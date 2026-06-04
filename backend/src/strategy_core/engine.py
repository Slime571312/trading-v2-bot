"""Engine-Orchestrator — bündelt die 6 Playbook-Schritte zu einer Funktion.

Spec-Quelle: ~/Desktop/tradingbot/Trading/Bot/Playbook.md

Eingabe: ein Dictionary `bars: dict[tf -> DataFrame]` mit OHLCV pro TF (mindestens
1d, einer der HTFs aus 1h/30m/15m, einer der LTFs aus 5m/1m).

Ausgabe: ein `Signal` oder `None` — Backtest und Live-Bot teilen exakt diese
Funktion. Stateless. Determinstisch.
"""
from __future__ import annotations

import logging

import pandas as pd

from . import bias as bias_mod
from . import liquidity as liquidity_mod
from . import news as news_mod
from . import rr as rr_mod
from . import sessions as sessions_mod
from . import structure as structure_mod
from . import sweep as sweep_mod
from . import zones as zones_mod
from ._types import Signal, SignalVariant
from .pivots import find_pivots

log = logging.getLogger(__name__)


def evaluate(
    instrument: str,
    bars: dict[str, pd.DataFrame],
    rr_threshold: float = 2.0,
    sweep_lookback_bars: int = 10,
) -> Signal | None:
    """Vollständige Playbook-Auswertung. Liefert ein Signal oder None.

    `bars`-Keys erwartet: '1d' + mindestens einer von {'1h','30m','15m'} +
    mindestens einer von {'5m','1m'}.
    """
    if "1d" not in bars or len(bars["1d"]) < 10:
        return None

    # ── Schritt 1: Daily-Bias ────────────────────────────────────────────
    bias = bias_mod.compute_bias(bars["1d"])
    if bias.direction == "neutral":
        log.debug("evaluate %s: neutral bias → no signal", instrument)
        return None

    # ── Schritt 2+3: HTF-LQ + Sweep — höchster TF gewinnt ────────────────
    htf_order = ["1h", "30m", "15m"]
    htf_used: str | None = None
    detected_sweep = None
    df_htf: pd.DataFrame | None = None

    # Session-Filter: aktuelle Uhrzeit prüfen, nicht den letzten gecachten Bar.
    _now_utc = pd.Timestamp.now(tz="UTC")
    if not sessions_mod.is_in_session(_now_utc, instrument):
        log.debug("evaluate %s: outside session at %s", instrument, _now_utc.isoformat())
        return None

    # News-Filter (Bot/News-Integration.md): kein Entry ±30/+15 Min um High-Impact-Events
    in_news, event_title = news_mod.is_news_window(instrument, now=_now_utc.to_pydatetime())
    if in_news:
        log.info("evaluate %s: News-Window aktiv (%s) — skip", instrument, event_title)
        return None

    # OPEX-Close-Window (Bot/OPEX-Calendar.md): letzte 30 Min am OPEX-Tag = Multi-Mrd Pin-Flows
    if instrument not in sessions_mod.CRYPTO_INSTRUMENTS and sessions_mod.is_opex_close_window(_now_utc):
        log.info("evaluate %s: OPEX-Close-Window — kein Entry", instrument)
        return None

    # Silver-Bullet (Bot/Silver-Bullet.md): strenger MIN_RR (2.0) außerhalb der NY-AM-Zone.
    # Innerhalb der SB-Fenster bleibt MIN_RR = 1.5 (Standard) → Window gibt Edge, niedrigere Hürde.
    # Für Indizes greift SB-NY-AM (15:00-16:00 UTC), für BTC alle drei Fenster (24/7).
    if instrument in sessions_mod.CRYPTO_INSTRUMENTS:
        _sb_active = sessions_mod.is_any_silver_bullet_window(_now_utc)
    else:
        _sb_active = sessions_mod.is_silver_bullet_window(_now_utc, "ny_am") or \
                     sessions_mod.is_silver_bullet_window(_now_utc, "london_open")
    _effective_min_rr = rr_mod.MIN_RR if _sb_active else 2.0

    for tf in htf_order:
        df = bars.get(tf)
        if df is None or len(df) < 20:
            continue
        levels = liquidity_mod.liquidity_levels(df, bias.direction)
        sweeps = sweep_mod.detect_sweeps(df, levels, tf=tf, lookback=sweep_lookback_bars)
        if sweeps:
            detected_sweep = sweeps[-1]  # jüngster
            htf_used = tf
            df_htf = df
            break

    if detected_sweep is None or htf_used is None or df_htf is None:
        return None

    # ── Schritt 4: LTF-BOS in Bias-Richtung ──────────────────────────────
    ltf_order = ["5m", "1m"]
    bos = None
    ltf_used: str | None = None
    df_ltf: pd.DataFrame | None = None
    sweep_ltf_idx: int = -1

    for tf in ltf_order:
        df = bars.get(tf)
        if df is None or len(df) < 10:
            continue
        # Finde LTF-Bar der den HTF-Sweep enthält bzw. direkt danach kommt
        ltf_after = df[df.index >= detected_sweep.time]
        if len(ltf_after) < 3:
            continue
        first_after = ltf_after.index[0]
        idx = df.index.get_loc(first_after)
        # idx kann Slice sein bei Duplikaten — wir nehmen den ersten Treffer
        if isinstance(idx, slice):
            idx = idx.start
        # Vault (CHoCH.md): CHoCH ist der frühere, präzisere LTF-Trigger nach HTF-Sweep.
        # BOS als Fallback wenn CHoCH nichts liefert (mehr Bestätigung, späteren Entry).
        candidate = structure_mod.find_choch_after(df, bias.direction, int(idx))
        if candidate is None:
            candidate = structure_mod.find_bos_after(df, bias.direction, int(idx))
        if candidate is not None:
            bos = candidate
            ltf_used = tf
            df_ltf = df
            sweep_ltf_idx = int(idx)
            break

    if bos is None or ltf_used is None or df_ltf is None:
        return None

    # ── Schritt 5: RR-Check ──────────────────────────────────────────────
    entry_primary = float(df_ltf["close"].iloc[bos.bar_idx])
    # SL mit ATR-Buffer (per-Instrument kalibriert — SL-Placement.md)
    sl = rr_mod.compute_sl(bias.direction, detected_sweep, df_htf, instrument=instrument)
    htf_pivots = find_pivots(df_htf)
    tp = rr_mod.find_tp_target(bias.direction, entry_primary, htf_pivots)
    if tp is None:
        return None

    rr_primary = rr_mod.rr_ratio(bias.direction, entry_primary, sl, tp)
    variant: SignalVariant | None = None
    entry = entry_primary
    rr = rr_primary
    ob = None
    fvg = None
    eq_data = None

    if rr_primary >= rr_threshold:
        variant = "primary"
    else:
        # ── Schritt 6: Retracement-Suche — OB > FVG, mit OTE-Confluence-Check ──
        # Dealing-Range für OTE: Impulse-Leg von Sweep-Extrem zu BOS-Extrem
        if bias.direction == "long":
            _dr_low = float(df_htf["low"].iloc[detected_sweep.bar_idx])
            _dr_high = bos.body_extreme
        else:
            _dr_low = bos.body_extreme
            _dr_high = float(df_htf["high"].iloc[detected_sweep.bar_idx])
        _ote_bias = 1 if bias.direction == "long" else -1
        _ote_zone = (
            rr_mod.calculate_ote_zone(_dr_low, _dr_high, _ote_bias)
            if _dr_low < _dr_high
            else None
        )

        # OB auf HTF suchen (wo der Sweep passierte, dort sitzt der echte OB)
        ob = zones_mod.find_order_block(df_htf, detected_sweep, bos)
        if ob is not None:
            entry = (ob.upper + ob.lower) / 2.0
            rr = rr_mod.rr_ratio(bias.direction, entry, sl, tp)
            eq_data = zones_mod.compute_equilibrium(detected_sweep, bos, df_htf)
            in_eq = zones_mod.in_eq_zone(entry, eq_data, bias.direction)
            # OTE ∩ OB = höchste Confluence (Rang 1 in Signal-Hierarchie)
            if _ote_zone is not None and rr_mod.ote_confluence_entry(
                entry, _ote_zone, ob.lower, ob.upper
            ):
                variant = "ote_ob_entry"
            elif in_eq:
                variant = "ultimate"
            else:
                variant = "ob_retest"
        else:
            # FVG auf LTF zwischen Sweep und BOS
            fvgs = zones_mod.find_fvgs(
                df_ltf, sweep_ltf_idx, bos.bar_idx, bias.direction,
            )
            if fvgs:
                fvg = fvgs[-1]  # neuester FVG zwischen Sweep und BOS
                entry = (fvg.upper + fvg.lower) / 2.0
                rr = rr_mod.rr_ratio(bias.direction, entry, sl, tp)
                eq_data = zones_mod.compute_equilibrium(detected_sweep, bos, df_htf)
                in_eq = zones_mod.in_eq_zone(entry, eq_data, bias.direction)
                variant = "ultimate" if in_eq else "fvg_retest"
            else:
                return None  # weder OB noch FVG → Setup verworfen

    # Hard-Floor MIN_RR (TP-Strategy.md + Silver-Bullet.md):
    # in SB-Fenster: 1.5 (Standard). Außerhalb: 2.0 (strenger, weil Edge fehlt).
    if variant is None or rr < _effective_min_rr:
        log.debug("evaluate %s: RR %.2f < effective_MIN_RR %.2f (SB_active=%s) — skip",
                  instrument, rr, _effective_min_rr, _sb_active)
        return None

    return Signal(
        time=df_ltf.index[-1],
        instrument=instrument,
        side=bias.direction,
        entry=float(entry),
        sl=float(sl),
        tp=float(tp),
        rr=float(rr),
        variant=variant,
        htf_used=htf_used,
        ltf_used=ltf_used,
        bias=bias,
        sweep=detected_sweep,
        structure_break=bos,
        ob=ob,
        fvg=fvg,
        equilibrium=eq_data,
    )
