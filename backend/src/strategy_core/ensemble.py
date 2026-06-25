"""Ensemble Entry Signals — Multi-Pattern Voting System.

3 independent patterns vote on entry quality:
1. Order-Block Retest (70% confidence)
2. FVG-Mitigation (65% confidence)
3. Equilibrium + RSI (60% confidence)

Only enter if ≥2 patterns agree (STRONG signal).
Single pattern = WEAK signal (reduced position size).
No patterns = SKIP.

Spec: ~/Desktop/tradingbot/Trading/Bot/Ensemble-Entry-Signals.md
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from .volatility import compute_rsi

log = logging.getLogger(__name__)


@dataclass
class EnsembleSignal:
    """Result of ensemble voting across 3 patterns."""
    has_signal: bool
    confidence: float  # 0.60–1.0 (weighted average of voters)
    voters: list[str]  # ["OB"], ["OB", "FVG"], etc.
    ensemble_grade: Literal["STRONG", "WEAK", "SKIP"]
    primary_voter: str | None  # First voter in list


def signal_order_block_retest(
    bars: dict[str, pd.DataFrame],
    side: Literal["long", "short"] = "long",
) -> bool:
    """Pattern 1: Order-Block Retest.

    Order-Block = Liquidity Sweep + Break-of-Structure.
    Sequence:
    1. Sweep: Local High (long) or Low (short) created
    2. Pullback: Price retraces from sweep
    3. BOS: Price breaks pullback and retests sweep area

    Returns True if current setup is OB retest setup.
    """
    if "1h" not in bars or "5m" not in bars:
        return False

    htf = bars["1h"]
    ltf = bars["5m"]

    if len(htf) < 5 or len(ltf) < 5:
        return False

    # ── Sweep Detection (last 5 HTF bars) ──
    if side == "long":
        # Find local high (sweep level)
        sweep_high = htf["high"].tail(5).max()
        sweep_high_idx = htf["high"].tail(5).idxmax()

        # Pullback check: bar after sweep should have lower high
        post_sweep = htf[htf.index > sweep_high_idx]
        if len(post_sweep) < 2:
            return False

        pullback_high = post_sweep["high"].iloc[-2]  # 2nd-last after sweep

        # Pullback must be lower than sweep (no higher high)
        if pullback_high >= sweep_high * 0.99:
            return False

        # BOS: current candle breaks pullback
        current_close = htf["close"].iloc[-1]
        if current_close <= pullback_high:
            return False

        # LTF confirmation: 5m-low should be near sweep-low (retest area)
        current_low_5m = ltf["low"].iloc[-1]
        sweep_low = htf["low"].tail(5).min()

        # Within 0.5% of sweep low = in retest zone
        if current_low_5m >= sweep_low * 0.995:
            return True

    else:  # short
        # Find local low (sweep level)
        sweep_low = htf["low"].tail(5).min()
        sweep_low_idx = htf["low"].tail(5).idxmin()

        # Pullback check
        post_sweep = htf[htf.index > sweep_low_idx]
        if len(post_sweep) < 2:
            return False

        pullback_low = post_sweep["low"].iloc[-2]

        # Pullback must be higher than sweep
        if pullback_low <= sweep_low * 1.01:
            return False

        # BOS: current candle breaks pullback
        current_close = htf["close"].iloc[-1]
        if current_close >= pullback_low:
            return False

        # LTF confirmation
        current_high_5m = ltf["high"].iloc[-1]
        sweep_high = htf["high"].tail(5).max()

        if current_high_5m <= sweep_high * 1.005:
            return True

    return False


def signal_fvg_mitigation(
    bars: dict[str, pd.DataFrame],
    side: Literal["long", "short"] = "long",
) -> bool:
    """Pattern 2: FVG-Mitigation.

    Fair-Value-Gap = imbalance (gap without wick) between candles.
    Mitigation = current candle closes the gap.

    Bullish FVG (for long): candle[n].close < candle[n+1].open
    and candle[n+1].close < candle[n+2].open (down-trend gaps).
    Mitigation: current bar penetrates into gap.
    """
    if "1h" not in bars:
        return False

    df = bars["1h"]
    if len(df) < 5:
        return False

    if side == "long":
        # Bullish FVG detection (last 3 candles)
        n3_close = df["close"].iloc[-3]
        n2_open = df["open"].iloc[-2]
        n2_close = df["close"].iloc[-2]
        n1_open = df["open"].iloc[-1]
        n1_low = df["low"].iloc[-1]

        # Gap checks
        bullish_fvg_exists = (n3_close < n2_open) and (n2_close < n1_open)

        if not bullish_fvg_exists:
            return False

        # FVG zone bounds
        fvg_gap_low = max(n3_close, n2_close)  # Top of gap zone
        fvg_gap_high = min(n2_open, n1_open)   # Bottom of gap zone

        # Mitigation: current candle fills gap (low penetrates into gap)
        if n1_low <= fvg_gap_low:
            return True

    else:  # short
        # Bearish FVG detection
        n3_close = df["close"].iloc[-3]
        n2_open = df["open"].iloc[-2]
        n2_close = df["close"].iloc[-2]
        n1_open = df["open"].iloc[-1]
        n1_high = df["high"].iloc[-1]

        bearish_fvg_exists = (n3_close > n2_open) and (n2_close > n1_open)

        if not bearish_fvg_exists:
            return False

        fvg_gap_high = min(n3_close, n2_close)
        fvg_gap_low = max(n2_open, n1_open)

        if n1_high >= fvg_gap_high:
            return True

    return False


def signal_equilibrium_rsi(
    bars: dict[str, pd.DataFrame],
    side: Literal["long", "short"] = "long",
    rsi_threshold: float = 30.0,
) -> bool:
    """Pattern 3: Equilibrium + RSI.

    Equilibrium = midpoint of highest high + lowest low (last 20 HTF bars).
    Entry condition: price at or below equilibrium AND RSI oversold/overbought.

    For long: price <= equilibrium AND RSI < 30
    For short: price >= equilibrium AND RSI > 70
    """
    if "1h" not in bars:
        return False

    htf = bars["1h"]

    if len(htf) < 20:
        return False

    # Compute equilibrium over last 20 bars
    eq_level = (htf["high"].tail(20).max() + htf["low"].tail(20).min()) / 2

    current_close = htf["close"].iloc[-1]

    # Compute RSI from HTF (assume it's already in bars, else compute)
    if "rsi" in htf.columns:
        current_rsi = htf["rsi"].iloc[-1]
    else:
        # Compute RSI(14) on-the-fly
        rsi_series = compute_rsi(htf["close"], length=14)
        current_rsi = rsi_series.iloc[-1]

    if side == "long":
        # Long entry: price <= equilibrium AND RSI < 30
        if current_close <= eq_level and current_rsi < rsi_threshold:
            return True

    else:  # short
        # Short entry: price >= equilibrium AND RSI > 70
        rsi_upper = 100 - rsi_threshold
        if current_close >= eq_level and current_rsi > rsi_upper:
            return True

    return False


def ensemble_evaluate(
    bars: dict[str, pd.DataFrame],
    side: Literal["long", "short"] = "long",
) -> EnsembleSignal:
    """Combine 3 patterns with voting logic.

    ≥2 voters → STRONG signal (full position size)
    1 voter → WEAK signal (0.5× position size)
    0 voters → SKIP (no entry)

    Returns EnsembleSignal with voting details.
    """
    votes = []

    # Pattern 1: Order-Block Retest
    try:
        if signal_order_block_retest(bars, side=side):
            votes.append("OB")
    except Exception as e:
        log.warning("ensemble: OB pattern error: %s", str(e)[:100])

    # Pattern 2: FVG-Mitigation
    try:
        if signal_fvg_mitigation(bars, side=side):
            votes.append("FVG")
    except Exception as e:
        log.warning("ensemble: FVG pattern error: %s", str(e)[:100])

    # Pattern 3: Equilibrium + RSI
    try:
        if signal_equilibrium_rsi(bars, side=side):
            votes.append("EQ-RSI")
    except Exception as e:
        log.warning("ensemble: EQ-RSI pattern error: %s", str(e)[:100])

    # ── Voting Result ──
    if len(votes) >= 2:
        # STRONG: ≥2 voters agree
        pattern_confidence = {
            "OB": 0.70,
            "FVG": 0.65,
            "EQ-RSI": 0.60,
        }
        avg_confidence = sum(pattern_confidence[v] for v in votes) / len(votes)

        result = EnsembleSignal(
            has_signal=True,
            confidence=avg_confidence,  # Weighted average
            voters=votes,
            ensemble_grade="STRONG",
            primary_voter=votes[0],
        )
        log.debug("ensemble STRONG: voters=%s confidence=%.2f", votes, avg_confidence)
        return result

    elif len(votes) == 1:
        # WEAK: single voter (reduced confidence by 30%)
        pattern_confidence = {
            "OB": 0.70,
            "FVG": 0.65,
            "EQ-RSI": 0.60,
        }
        base_conf = pattern_confidence[votes[0]]
        reduced_conf = base_conf * 0.7  # 30% penalty

        result = EnsembleSignal(
            has_signal=True,
            confidence=reduced_conf,
            voters=votes,
            ensemble_grade="WEAK",
            primary_voter=votes[0],
        )
        log.debug("ensemble WEAK: voter=%s confidence=%.2f (base %.2f)",
                 votes[0], reduced_conf, base_conf)
        return result

    else:
        # SKIP: no voters
        result = EnsembleSignal(
            has_signal=False,
            confidence=0.0,
            voters=[],
            ensemble_grade="SKIP",
            primary_voter=None,
        )
        log.debug("ensemble SKIP: no voter agreement")
        return result
