"""Conviction-Score — 10-Punkte ICT-Confluence-Score nach Bot/Conviction-Score.md.

Soft-Filter NACH dem Engine-Signal: ein Setup kann alle Hard-Gates passieren
(Bias + Sweep + BOS + RR ≥ 1.5) und trotzdem zu wenig Confluence haben.

Drei Ausgaben pro Setup:
  - "A" (Score ≥ 7 trending / 8 uncertain / 9 ranging) → 1.0× Risk
  - "B" (Score 5-6 trending / 6-7 uncertain) → 0.5× Risk
  - "skip" (zu wenig Score, oder ≥ 2 Kontradiktionen) → kein Trade

ADX(14) auf 1h bestimmt das Regime:
  - ADX > 25  → trending (Standard-Schwellen)
  - 20 ≤ ADX ≤ 25 → uncertain (+1 strikter)
  - ADX < 20 → ranging (nur A-Setups erlaubt)

Faktoren die der Bot v2 derzeit liefert (aus engine.py):
  - bias (klar bullish/bearish)
  - sweep HTF (1h/30m/15m) oder LTF (5m/1m)
  - BOS/CHoCH bestätigt
  - OB oder FVG vorhanden
  - in Kill-Zone (Silver-Bullet-Fenster)
  - in OTE-Zone (variant == 'ote_ob_entry')
  - weekday_weight (Power-of-3 Approx)

Noch nicht verfügbar (Score-Punkt = 0):
  - SMT Divergence (smt.py noch nicht gebaut)
  - Funding-Rate-Penalty (sentiment.py noch nicht gebaut)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd


# Weekday-Gewichte aus Power-of-3.md (approximiert):
# Tu/Wed/Thu = Prime ICT-Setup-Tage. Mo = Accumulation. Fri = OPEX-Drift.
_WEEKDAY_WEIGHTS = {
    0: 0.7,   # Mo
    1: 1.0,   # Tue
    2: 1.0,   # Wed
    3: 1.0,   # Thu
    4: 0.5,   # Fri
    5: 0.0,   # Sat (no trade)
    6: 0.0,   # Sun (no trade)
}


def weekday_entry_weight(ts: datetime | pd.Timestamp) -> float:
    """Power-of-3 Weekday-Gewicht (Bot/Power-of-3.md, Approximation)."""
    return _WEEKDAY_WEIGHTS.get(ts.weekday(), 1.0)


@dataclass
class ConvictionContext:
    """Alle Faktoren die der Score braucht. Wird in engine.py befüllt."""
    instrument: str
    bias_clear: bool             # True wenn Daily-Bias != neutral
    sweep_is_htf: bool           # True wenn HTF-Sweep (1h/30m/15m), False wenn nur LTF
    bos_confirmed: bool          # True wenn LTF-CHoCH oder BOS bestätigt
    active_zone: bool            # True wenn OB oder FVG als Entry-Zone gefunden
    is_kill_zone: bool           # True wenn Silver-Bullet-Fenster aktiv
    in_ote_zone: bool            # True wenn variant == 'ote_ob_entry' oder ähnlich
    weekday_weight: float        # aus weekday_entry_weight()
    # Erweiterungen (default False bis SMT/Funding implementiert sind)
    smt_confirms: bool = False
    smt_diverges: bool = False
    funding_contra: bool = False


def compute_conviction_score(ctx: ConvictionContext) -> tuple[int, dict]:
    """10-Punkte Score + Breakdown pro Faktor (für TickLog)."""
    pts: dict[str, int] = {}

    pts["htf_bias"] = 2 if ctx.bias_clear else 0
    pts["sweep"] = 2 if ctx.sweep_is_htf else (1 if ctx.bos_confirmed else 0)
    pts["entry_zone"] = 1 if ctx.active_zone else 0
    pts["kill_zone"] = 1 if ctx.is_kill_zone else 0
    pts["bos"] = 1 if ctx.bos_confirmed else 0
    pts["ote_zone"] = 1 if ctx.in_ote_zone else 0
    pts["smt"] = 1 if ctx.smt_confirms else 0
    pts["weekday"] = 1 if ctx.weekday_weight >= 0.8 else 0

    base = sum(pts.values())
    contradictions = int(ctx.smt_diverges) + int(ctx.funding_contra)
    pts["contradiction_penalty"] = -contradictions
    score = base - contradictions
    return score, pts


def get_conviction_thresholds(adx: float) -> dict:
    """ADX(14)-Regime bestimmt A/B-Setup-Schwellen."""
    if adx > 25:
        return {"a_setup": 7, "b_setup": 5, "no_b": False, "regime": "trending"}
    elif adx >= 20:
        return {"a_setup": 8, "b_setup": 6, "no_b": False, "regime": "uncertain"}
    else:
        return {"a_setup": 9, "b_setup": 99, "no_b": True, "regime": "ranging"}


def classify_setup(
    score: int, adx: float, contradictions: int = 0,
) -> tuple[str, float, str]:
    """Returns (grade, size_multiplier, regime).

    grade ∈ {"A", "B", "skip"}.
    size_multiplier ∈ {1.0, 0.5, 0.0}.
    regime ∈ {"trending", "uncertain", "ranging"}.
    """
    if contradictions >= 2:
        return "skip", 0.0, "trending"
    th = get_conviction_thresholds(adx)
    if score >= th["a_setup"]:
        return "A", 1.0, th["regime"]
    if score >= th["b_setup"] and not th["no_b"]:
        return "B", 0.5, th["regime"]
    return "skip", 0.0, th["regime"]


def compute_adx(ohlcv: pd.DataFrame, period: int = 14) -> float:
    """ADX(14) — Wilder's Smoothing. Returns 0.0 bei zu wenig Bars."""
    if len(ohlcv) < period * 2 + 1:
        return 0.0
    high = ohlcv["high"].astype(float)
    low = ohlcv["low"].astype(float)
    close = ohlcv["close"].astype(float)

    # True Range
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Directional Movement
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    # Wilder's smoothing via EMA-ähnlich (alpha=1/period)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    val = adx.iloc[-1]
    return float(val) if pd.notna(val) else 0.0
