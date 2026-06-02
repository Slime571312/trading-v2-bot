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
    # Gecachte Bars können Stunden alt sein — df.index[-1] wäre dann falsch.
    _now_utc = pd.Timestamp.now(tz="UTC")
    if not sessions_mod.is_in_session(_now_utc, instrument):
        log.debug("evaluate %s: outside session at %s", instrument, _now_utc.isoformat())
        return None

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
    sl = rr_mod.compute_sl(bias.direction, detected_sweep, df_htf)
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
        # ── Schritt 6: Retracement-Suche — OB > FVG, mit optionalem EQ-Boost ──
        # OB auf HTF suchen (wo der Sweep passierte, dort sitzt der echte OB)
        ob = zones_mod.find_order_block(df_htf, detected_sweep, bos)
        if ob is not None:
            entry = (ob.upper + ob.lower) / 2.0
            rr = rr_mod.rr_ratio(bias.direction, entry, sl, tp)
            eq_data = zones_mod.compute_equilibrium(detected_sweep, bos, df_htf)
            in_eq = zones_mod.in_eq_zone(entry, eq_data, bias.direction)
            variant = "ultimate" if in_eq else "ob_retest"
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

    # Mindest-RR auch nach Retracement-Verbesserung — wenn auch das nicht passt, skip
    if variant is None or rr < rr_threshold * 0.75:
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
