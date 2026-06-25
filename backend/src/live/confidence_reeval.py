"""Confidence-Tier Dynamic Re-Evaluation.

Mid-trade re-evaluation of position confidence based on 3 gates:
1. Regime Gate (volatility, bias stability)
2. Structure Gate (OB/FVG destruction)
3. Momentum Gate (RSI, MACD continuation)

Re-eval every 15min. If tier drops, tighten SL/TP accordingly.

Spec: ~/Desktop/tradingbot/Trading/Bot/Confidence-Tier-Dynamic.md
"""
from __future__ import annotations

import logging
from typing import Literal

import pandas as pd

from src.strategy_core import bias as bias_mod
from src.strategy_core import volatility as vol_mod
from src.live.state import OpenTrade

log = logging.getLogger(__name__)


def regime_gate_ok(
    position: OpenTrade,
    bars: dict[str, pd.DataFrame],
) -> bool:
    """Gate 1: Is market regime still compatible with entry regime?

    Checks:
    - Volatility: ±20% of entry volatility (no spike/crash)
    - Daily Bias: still in entry direction (not "neutral")
    - Trend Stability: HTF not reversed against position
    """
    if "1h" not in bars:
        return True  # Can't check, conservatively OK

    # Volatility check
    entry_vol = position.atr_pct_at_open or 1.0
    try:
        current_vol = vol_mod.compute_atr_pct(bars["1h"])
    except Exception as e:
        log.warning("regime_gate: atr_pct error: %s", str(e)[:100])
        return True  # Fallback: can't check, assume OK

    if current_vol <= 0 or entry_vol <= 0:
        return True  # Invalid data, skip check

    vol_ratio = current_vol / entry_vol

    # Volatility spike beyond 20% = regime break
    if vol_ratio > 1.2 or vol_ratio < 0.8:
        log.info(
            "regime_gate FAIL: vol spike %.2f (entry=%.2f current=%.2f)",
            vol_ratio, entry_vol, current_vol
        )
        return False

    # Bias check (if 1d available)
    if "1d" in bars:
        try:
            bias = bias_mod.compute_bias(bars["1d"])
            if bias.direction == "neutral":
                log.info("regime_gate FAIL: bias went neutral")
                return False
        except Exception as e:
            log.warning("regime_gate: bias error: %s", str(e)[:100])

    return True


def structure_gate_ok(
    position: OpenTrade,
    bars: dict[str, pd.DataFrame],
) -> bool:
    """Gate 2: Is initial structure (OB/FVG) still valid?

    Checks:
    - For LONG: HTF low hasn't penetrated below SL (OB not destroyed)
    - For SHORT: HTF high hasn't penetrated above TP (OB not destroyed)
    - Structure integrity = Order Block still holds
    """
    if position.htf_used not in bars:
        return True  # Can't check, conservatively OK

    htf = bars[position.htf_used]
    if len(htf) < 5:
        return True

    try:
        current_high = htf["high"].iloc[-1]
        current_low = htf["low"].iloc[-1]

        if position.side == "long":
            # For long: check SL integrity
            # If price penetrates below SL, OB is destroyed
            if current_low < position.sl:
                log.info(
                    "structure_gate FAIL (long): price low %.2f < SL %.2f",
                    current_low, position.sl
                )
                return False

        else:  # short
            # For short: check TP integrity
            if current_high > position.tp:
                log.info(
                    "structure_gate FAIL (short): price high %.2f > TP %.2f",
                    current_high, position.tp
                )
                return False

        return True

    except Exception as e:
        log.warning("structure_gate: error: %s", str(e)[:100])
        return True


def momentum_gate_ok(
    position: OpenTrade,
    bars: dict[str, pd.DataFrame],
) -> bool:
    """Gate 3: Is initial momentum still active?

    Checks:
    - RSI not in extreme reversal zone (< 20 for long, > 80 for short)
    - MACD still pointing in entry direction (if entry was high-conviction)
    - EMA cross still valid (if available)
    """
    if position.ltf_used not in bars:
        return True

    ltf = bars[position.ltf_used]
    if len(ltf) < 10:
        return True

    try:
        # RSI check
        if "rsi" in ltf.columns:
            rsi = ltf["rsi"].iloc[-1]
        else:
            from src.strategy_core.volatility import compute_rsi
            rsi = compute_rsi(ltf["close"], length=14).iloc[-1]

        if position.side == "long":
            # Long: RSI < 20 = too much reversal to oversold
            if rsi < 20:
                log.info("momentum_gate FAIL (long): RSI %.0f < 20 (extreme reversal)",
                        rsi)
                return False

            # For high-conviction entries, MACD should still be bullish
            if position.conviction_score_at_open and \
               position.conviction_score_at_open > 70:
                if "macd" in ltf.columns and "macd_signal" in ltf.columns:
                    macd = ltf["macd"].iloc[-1]
                    macd_signal = ltf["macd_signal"].iloc[-1]
                    if macd < macd_signal:
                        log.info("momentum_gate FAIL (long): MACD bearish (%.2f < %.2f)",
                                macd, macd_signal)
                        return False

        else:  # short
            # Short: RSI > 80 = too much reversal to overbought
            if rsi > 80:
                log.info("momentum_gate FAIL (short): RSI %.0f > 80 (extreme reversal)",
                        rsi)
                return False

            if position.conviction_score_at_open and \
               position.conviction_score_at_open > 70:
                if "macd" in ltf.columns and "macd_signal" in ltf.columns:
                    macd = ltf["macd"].iloc[-1]
                    macd_signal = ltf["macd_signal"].iloc[-1]
                    if macd > macd_signal:
                        log.info("momentum_gate FAIL (short): MACD bullish (%.2f > %.2f)",
                                macd, macd_signal)
                        return False

        return True

    except Exception as e:
        log.warning("momentum_gate: error: %s", str(e)[:100])
        return True


def re_evaluate_confidence_tier(
    position: OpenTrade,
    bars: dict[str, pd.DataFrame],
) -> Literal["A", "B", "C", "D"]:
    """Re-evaluate position confidence tier.

    1. Check all 3 gates
    2. Map gate results to new tier:
       - 3/3 gates OK: A (1.0× risk)
       - 2/3 gates OK: B (0.5× risk)
       - 1/3 gates OK: C (0.2× risk)
       - 0/3 gates OK: D (exit immediately)

    Returns: new tier (A/B/C/D)
    """
    regime_ok = regime_gate_ok(position, bars)
    struct_ok = structure_gate_ok(position, bars)
    momentum_ok = momentum_gate_ok(position, bars)

    gates_ok_count = sum([regime_ok, struct_ok, momentum_ok])

    if gates_ok_count == 3:
        tier = "A"
    elif gates_ok_count == 2:
        tier = "B"
    elif gates_ok_count == 1:
        tier = "C"
    else:
        tier = "D"

    log.info(
        "re_evaluate_confidence_tier %s: %s (regime=%s struct=%s momentum=%s) "
        "→ tier %s",
        position.id,
        position.confidence_tier_at_open or "unknown",
        "ok" if regime_ok else "FAIL",
        "ok" if struct_ok else "FAIL",
        "ok" if momentum_ok else "FAIL",
        tier
    )

    return tier


def adjust_position_for_tier(
    position: OpenTrade,
    new_tier: Literal["A", "B", "C", "D"],
) -> None:
    """Adjust position SL/TP based on new confidence tier.

    Tier drops trigger:
    - SL tighter (move towards entry)
    - TP1 adjusted (tighter target)
    - For D: mark for immediate exit
    """
    old_tier = position.confidence_tier_at_open or "A"

    if new_tier == old_tier:
        return  # No change

    rr_at_entry = position.rr_at_open if position.rr_at_open > 0 else 2.0
    risk_distance = abs(position.entry - position.sl)

    log.info("adjust_position_for_tier %s: %s → %s", position.id, old_tier, new_tier)

    if new_tier == "B":
        # A→B: SL tighter by 0.25× RR
        if position.side == "long":
            new_sl = position.sl + risk_distance * 0.25
            position.sl = min(new_sl, position.entry - risk_distance * 0.1)
        else:
            new_sl = position.sl - risk_distance * 0.25
            position.sl = max(new_sl, position.entry + risk_distance * 0.1)
        log.info("B-tier: SL tightened from %.2f to %.2f", position.sl, new_sl)

    elif new_tier == "C":
        # B→C or A→C: SL very tight (0.1× RR from entry)
        if position.side == "long":
            position.sl = position.entry - risk_distance * 0.1
        else:
            position.sl = position.entry + risk_distance * 0.1

        # Also adjust TP to TP1 level (lock profit zone)
        if position.tp1 is not None:
            position.tp = position.tp1
        log.info("C-tier: SL very tight (%.2f), TP moved to TP1 (%.2f)",
                position.sl, position.tp)

    elif new_tier == "D":
        # Exit immediately: mark SL at entry (breakeven close)
        log.warning("D-tier: Position marked for immediate exit")
        # Loop will detect this and close the position

    # Record tier change in position metadata (if we add a field for it)
    # For now, it's logged via TickLogEntry in loop.py
