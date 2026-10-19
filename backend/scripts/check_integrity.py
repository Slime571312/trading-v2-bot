#!/usr/bin/env python3
"""Integritäts-Check für den Live-State — fängt stille Rechenfehler.

Hintergrund: Am 19.10.2026 fiel auf, dass 96 % des gebuchten Gewinns aus
Trades stammte, deren Ausgang beim Öffnen schon in den Daten stand (Bar-Zeit
war um +2h falsch, und der 1m-Exit-Scan las Bars innerhalb der 5m-Signal-Bar).
Der Bug lief wochenlang, weil er nicht crashte — er produzierte plausible,
falsche Zahlen. Aggregat-Auswertungen (Win-Rate, PnL-Summe) sahen sogar gut aus.

Dieses Skript prüft deshalb keine Aggregate, sondern **Invarianten**: Aussagen,
die bei korrektem Betrieb ausnahmslos wahr sein müssen. Drei der Checks unten
hätten den Bug am ersten Tag gemeldet (I1, I2, I3).

Aufruf:
    backend/.venv/bin/python backend/scripts/check_integrity.py
    ... --state pfad/zur/live_state.json   # anderer State
    ... --quiet                            # nur Verstöße

Exit-Code 1 bei mindestens einem CRITICAL-Verstoß — damit als launchd-/cron-Job
oder CI-Schritt verwendbar.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CRITICAL = "CRITICAL"
WARN = "WARN"

REPO = Path(__file__).resolve().parents[2]
DEFAULT_STATE = REPO / "backend" / "state" / "live_state.json"
CACHE_DIR = REPO / "backend" / "data" / "cache"


# ── Helfer ──────────────────────────────────────────────────────────────


def _dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _pnl(t: dict) -> float:
    """Echter PnL. `pnl_abs` ist bei Multi-TP-Teilverkäufen 0.0 — wer darauf
    rechnet, zählt jeden Teilverkaufs-Gewinner als Verlierer."""
    v = t.get("total_pnl")
    return float(t.get("pnl_abs") or 0.0) if v is None else float(v)


def _risk(t: dict) -> float:
    """Ursprüngliches Risiko. Bewusst NUR aus `original_sl`.

    Der Rückfall auf `sl` wäre falsch: nach einem TP1-Treffer steht dort
    Break-Even, nach TP2 der TP1-Preis. Damit gerechnet sieht ein korrekter
    Trade wie eine invertierte Stop-Platzierung aus — dieser Check hat genau
    diesen Fehler zuerst selbst gemacht und 42 Fehlalarme produziert.
    Alt-Trades ohne `original_sl` werden deshalb übersprungen, nicht geraten.
    """
    e, sl = t.get("entry"), t.get("original_sl")
    return abs(e - sl) if e is not None and sl is not None else 0.0


def _trades(state: dict):
    """(instrument, trade, is_open) für alle Trades."""
    for inst, d in state.get("instances", {}).items():
        for t in d.get("closed_trades", []):
            yield inst, t, False
        for t in d.get("open_trades", []):
            yield inst, t, True


# ── Invarianten ─────────────────────────────────────────────────────────
# Jede Funktion gibt (schwere, titel, [verstoß-zeilen], geprüft_n) zurück.


def i1_bar_time_not_in_future(state):
    """Bar-Zeitstempel dürfen nie in der Zukunft liegen.

    Der tz-Bug (snapshotTime statt snapshotTimeUTC) datierte jede Kerze um den
    Börsen-Offset nach vorn. Ein einziger Blick hierauf hätte ihn entlarvt.
    """
    now = datetime.now(timezone.utc)
    bad, n = [], 0
    for f in sorted(CACHE_DIR.glob("*_1m.csv")):
        try:
            last = f.read_text().rstrip().rsplit("\n", 1)[-1].split(",")[0]
        except (OSError, IndexError):
            continue
        ts = _dt(last)
        if ts is None:
            continue
        n += 1
        drift = (ts - now).total_seconds() / 60
        if drift > 2:  # 2 min Toleranz für die laufende Kerze
            bad.append(f"{f.stem}: Bar {ts:%Y-%m-%d %H:%M} liegt {drift:+.0f} min "
                       f"vor der Wall-Clock ({now:%H:%M} UTC)")
    return CRITICAL, "Bar-Zeit nicht in der Zukunft", bad, n


def i2_no_resolution_from_preexisting_bars(state):
    """Ein Trade darf nicht von Bars geschlossen werden, die beim Öffnen vorlagen.

    Das ist der Lookahead-Kern. `scan_from` ist die neueste beim Öffnen
    verfügbare Bar; schließt ein Trade zu einem Zeitpunkt davor, war der
    Ausgang vorbekannt.
    """
    bad, n = [], 0
    for inst, t, is_open in _trades(state):
        sf, ct = _dt(t.get("scan_from")), _dt(t.get("close_time"))
        if sf is None or ct is None:
            continue  # Alt-Trade ohne scan_from
        n += 1
        if ct <= sf:
            bad.append(f"{inst} {str(t.get('id'))[:12]}: Close {ct:%m-%d %H:%M} liegt "
                       f"vor/auf scan_from {sf:%m-%d %H:%M} → Ausgang war vorbekannt")
    return CRITICAL, "Keine Auflösung durch vorbestehende Bars", bad, n


def i3_no_identical_entry_exit_pairs(state):
    """Zwei Trades dürfen nicht Ein- UND Ausstiegspreis exakt teilen.

    Echte Kurse wiederholen sich nicht auf vier Dezimalstellen. Tritt das auf,
    wurde dasselbe Setup mehrfach gebucht — der Fingerprint der 14 Fake-Trades.
    """
    groups = defaultdict(list)
    for inst, t, is_open in _trades(state):
        e, x = t.get("entry"), t.get("exit")
        if e is None or x is None:
            continue
        groups[(inst, round(e, 4), round(x, 4))].append(t)
    bad = []
    for (inst, e, x), v in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(v) > 1:
            dupe_pnl = sum(_pnl(t) for t in v) - _pnl(v[0])
            bad.append(f"{inst} entry={e} exit={x}: {len(v)}× gebucht, "
                       f"{dupe_pnl:+.2f} EUR davon doppelt")
    return CRITICAL, "Keine identischen Entry-/Exit-Paare", bad, len(groups)


def i4_equity_matches_booked_pnl(state):
    """Σ PnL muss der Equity-Veränderung entsprechen — sonst ist die
    Buchhaltung kaputt (Doppelbuchung oder verlorene Teilverkäufe)."""
    bad, n = [], 0
    for inst, d in state.get("instances", {}).items():
        init = d.get("initial_capital")
        eq = d.get("equity")
        if init is None or eq is None:
            continue
        n += 1
        booked = sum(_pnl(t) for t in d.get("closed_trades", []))
        delta = (eq - init) - booked
        if abs(delta) > 0.51:  # 0.50 Toleranz für Rundung/offene Partials
            bad.append(f"{inst}: Equity−Start = {eq - init:+.2f}, "
                       f"Σ Trade-PnL = {booked:+.2f}, Differenz {delta:+.2f} EUR")
    return CRITICAL, "Equity deckt sich mit gebuchtem PnL", bad, n


def i5_stop_and_target_on_correct_side(state):
    """Short: SL über, TP unter dem Entry. Long umgekehrt. Immer."""
    bad, n = [], 0
    for inst, t, is_open in _trades(state):
        e, side = t.get("entry"), t.get("side")
        sl = t.get("original_sl")  # nie auf `sl` zurückfallen, s. _risk()
        if e is None or sl is None or side not in ("long", "short"):
            continue
        n += 1
        up = side == "long"
        if abs(sl - e) < 1e-9:
            bad.append(f"{inst} {str(t.get('id'))[:12]} {side}: SL == Entry ({e}) "
                       f"→ Risiko 0, degeneriertes Signal")
        elif (up and sl > e) or (not up and sl < e):
            bad.append(f"{inst} {str(t.get('id'))[:12]} {side}: SL {sl} auf der "
                       f"falschen Seite von Entry {e}")
        for name in ("tp", "tp1", "tp2"):
            tp = t.get(name)
            if tp is None:
                continue
            if (up and tp <= e) or (not up and tp >= e):
                bad.append(f"{inst} {str(t.get('id'))[:12]} {side}: {name} {tp} "
                           f"auf der falschen Seite von Entry {e}")
    return CRITICAL, "SL/TP auf der richtigen Seite des Entry", bad, n


def i6_sl_exit_cannot_profit(state):
    """Ein SL-Exit am unveränderten Original-SL kann nicht profitabel sein.

    Nach einem TP1-Treffer wandert der SL auf Break-Even, dann ist ein
    positiver Gesamt-PnL korrekt — dieser Fall wird ausgenommen.
    """
    bad, n = [], 0
    for inst, t, is_open in _trades(state):
        if t.get("exit_reason") != "sl" or is_open:
            continue
        sl, osl = t.get("sl"), t.get("original_sl")
        if sl is None or osl is None or abs(sl - osl) > 1e-9:
            continue  # SL wurde nachgezogen → Gewinn legitim
        n += 1
        if _pnl(t) > 0:
            bad.append(f"{inst} {str(t.get('id'))[:12]}: SL-Exit am Original-SL, "
                       f"aber PnL {_pnl(t):+.2f}")
    return CRITICAL, "SL-Exit am Original-SL ist nie profitabel", bad, n


def i7_tp1_respects_min_distance(state):
    """TP1 muss den in rr.py deklarierten Mindestabstand halten (_MIN_TP1_R).

    Sonst wird die halbe Position bei ~0R geschnitten: der Gewinner wird
    gedeckelt, der Verlierer bleibt voll. Das frisst die Expectancy leise auf.
    """
    try:
        from src.strategy_core.rr import _MIN_TP1_R as MIN_R
    except Exception:
        MIN_R = 0.5
    bad, n = [], 0
    for inst, t, is_open in _trades(state):
        e, tp1, side = t.get("entry"), t.get("tp1"), t.get("side")
        risk = _risk(t)
        if None in (e, tp1) or not risk or side not in ("long", "short"):
            continue
        n += 1
        r = (tp1 - e) / risk * (1 if side == "long" else -1)
        if r < MIN_R:
            bad.append(f"{inst} {str(t.get('id'))[:12]}: TP1 bei {r:+.3f}R "
                       f"(Minimum {MIN_R}R)")
    return WARN, f"TP1 hält Mindestabstand ({MIN_R}R)", bad, n


def i8_no_duplicate_trade_ids(state):
    """Trade-IDs müssen eindeutig sein."""
    seen, bad = defaultdict(list), []
    for inst, t, is_open in _trades(state):
        seen[t.get("id")].append(inst)
    for tid, insts in seen.items():
        if len(insts) > 1:
            bad.append(f"ID {tid} {len(insts)}× vorhanden ({', '.join(insts)})")
    return CRITICAL, "Trade-IDs eindeutig", bad, len(seen)


def i9_hold_time_plausible(state):
    """Haltedauer muss positiv und mindestens eine Scan-Bar lang sein.

    Eine Haltedauer von 0 heißt: in derselben Bar geöffnet und geschlossen —
    genau das Muster der Fake-Trades.
    """
    bad, n = [], 0
    for inst, t, is_open in _trades(state):
        ot, ct = _dt(t.get("open_time")), _dt(t.get("close_time"))
        if ot is None or ct is None:
            continue
        n += 1
        mins = (ct - ot).total_seconds() / 60
        if mins <= 0:
            bad.append(f"{inst} {str(t.get('id'))[:12]}: Haltedauer {mins:.1f} min "
                       f"(open {ot:%m-%d %H:%M}, close {ct:%m-%d %H:%M})")
    return CRITICAL, "Haltedauer positiv", bad, n


def i10_r_multiple_consistent(state):
    """Das gespeicherte r_multiple muss zur Preisbewegung passen.

    Bei Multi-TP ist r_multiple der gewichtete R über die Teilverkäufe. Ein
    positiver R trotz negativer Schluss-Bewegung ist deshalb korrekt, sobald
    ein Teilverkauf stattfand — erkennbar an `partial_pnl > 0`, NICHT an
    `tp1_hit` (auf Alt-Trades None, was hier zuerst 15 Fehlalarme ergab).
    """
    bad, n = [], 0
    for inst, t, is_open in _trades(state):
        r, e, x, side = t.get("r_multiple"), t.get("entry"), t.get("exit"), t.get("side")
        risk = _risk(t)
        if None in (r, e, x) or not risk or side not in ("long", "short"):
            continue
        n += 1
        had_partial = float(t.get("partial_pnl") or 0.0) != 0.0
        full = (x - e) / risk * (1 if side == "long" else -1)
        if full < -0.01 and r > 0.01 and not had_partial:
            bad.append(f"{inst} {str(t.get('id'))[:12]}: r_multiple {r:+.2f} positiv, "
                       f"Bewegung {full:+.2f}R, kein Teilverkauf gebucht")
        elif not had_partial and abs(r) > abs(full) + 0.05 and abs(full) > 0.01:
            bad.append(f"{inst} {str(t.get('id'))[:12]}: |r_multiple| {abs(r):.2f} "
                       f"größer als volle Bewegung {abs(full):.2f}R ohne Teilverkauf")
    return WARN, "r_multiple konsistent mit Preisbewegung", bad, n


INVARIANTS = [
    i1_bar_time_not_in_future,
    i2_no_resolution_from_preexisting_bars,
    i3_no_identical_entry_exit_pairs,
    i4_equity_matches_booked_pnl,
    i5_stop_and_target_on_correct_side,
    i6_sl_exit_cannot_profit,
    i7_tp1_respects_min_distance,
    i8_no_duplicate_trade_ids,
    i9_hold_time_plausible,
    i10_r_multiple_consistent,
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--quiet", action="store_true", help="nur Verstöße zeigen")
    ap.add_argument("--max-lines", type=int, default=8,
                    help="max. Beispielzeilen pro Verstoß (Default 8)")
    args = ap.parse_args()

    if not args.state.exists():
        print(f"State nicht gefunden: {args.state}", file=sys.stderr)
        return 2
    state = json.loads(args.state.read_text())

    n_crit = n_warn = 0
    print(f"Integritäts-Check — {args.state}")
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n")

    for fn in INVARIANTS:
        try:
            sev, title, bad, checked = fn(state)
        except Exception as e:  # ein kaputter Check darf die anderen nicht stoppen
            print(f"  ?? {fn.__name__}: Check selbst fehlgeschlagen — {e}")
            continue

        if bad:
            if sev == CRITICAL:
                n_crit += 1
            else:
                n_warn += 1
            mark = "XX" if sev == CRITICAL else "!!"
            print(f"  {mark} {title} — {len(bad)} Verstöße von {checked} geprüft")
            for line in bad[:args.max_lines]:
                print(f"       {line}")
            if len(bad) > args.max_lines:
                print(f"       … und {len(bad) - args.max_lines} weitere")
        elif not args.quiet:
            print(f"  ok {title} ({checked} geprüft)")

    print()
    if n_crit:
        print(f"ERGEBNIS: {n_crit} kritische Verstöße, {n_warn} Warnungen.")
        print("Performance-Zahlen aus diesem State sind nicht belastbar.")
        return 1
    print(f"ERGEBNIS: keine kritischen Verstöße, {n_warn} Warnungen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
