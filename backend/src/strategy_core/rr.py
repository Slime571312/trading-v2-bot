"""RR-Berechnung + Stop-Loss-/Take-Profit-Ableitung.

Quellen:
- Trading/SL und TP.md
- Trading/Bot/SL-Placement.md (ATR-Buffer, Multi-Method)
- Trading/Bot/TP-Strategy.md (Min-RR-Filter)
- Trading/Bot/Playbook.md (Schritt 5)
"""
from __future__ import annotations

import pandas as pd

from ._types import Side, Sweep, Swing


# Aus SL-Placement.md: per-Instrument kalibrierte ATR-Multiplier.
# ATR×Multiplier statt Pip-Buffer skaliert mit der Volatilität.
ATR_MULT: dict[str, float] = {
    "BTC":    0.15,
    "DE40":   0.10,
    "NASDAQ": 0.10,
    "SP500":  0.12,
}

# Aus TP-Strategy.md: Setup wird verworfen wenn RR1 < MIN_RR
MIN_RR = 1.5


def _atr(htf_df: pd.DataFrame, period: int = 14) -> float:
    """True Range Average — robuster als reine close-Differenz."""
    if len(htf_df) < period + 1:
        return 0.0
    high = htf_df["high"].astype(float)
    low = htf_df["low"].astype(float)
    close = htf_df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_val = tr.tail(period).mean()
    return float(atr_val) if pd.notna(atr_val) else 0.0


def compute_sl(
    side: Side,
    sweep: Sweep,
    htf_df: pd.DataFrame,
    instrument: str = "DE40",
) -> float:
    """SL jenseits der Sweep-Wick + ATR-basierter Buffer.

    Aus SL-Placement.md: ATR×0.10–0.15 statt prozentual.
    Pip-Buffer (10–20 Pips) ist cross-Instrument unsinnig — ATR skaliert mit Volatilität.
    """
    atr = _atr(htf_df, period=14)
    mult = ATR_MULT.get(instrument, 0.12)
    buffer = atr * mult

    if side == "long":
        wick = float(htf_df["low"].iloc[sweep.bar_idx])
        # Wenn ATR=0 (zu wenig Bars), Fallback auf 0.1% proportional
        return wick - buffer if buffer > 0 else wick * (1 - 0.001)
    else:
        wick = float(htf_df["high"].iloc[sweep.bar_idx])
        return wick + buffer if buffer > 0 else wick * (1 + 0.001)


def find_tp_target(side: Side, entry: float, htf_pivots: list[Swing]) -> float | None:
    """TP = **entferntester** gegenüberliegender Pivot (= größtes verfügbares RR).

    Aus dem Magnet-Modell in LQS.md: der größte Liquidity-Pool zieht den Preis,
    nicht jeder Mini-Wackler dazwischen. Die nächstgelegenen Pivots sind oft nur
    Range-Noise — der **prominente** Counter-Pool sitzt weiter weg. In v2 wählen
    wir konsistent den entferntesten Pivot in Trade-Richtung; Partial-TPs (TP1/
    TP2/TP3 mit Cuts dazwischen) sind eine Etappe-3-Erweiterung.

    - Long  → höchster Pivot-High über Entry.
    - Short → tiefster Pivot-Low unter Entry.
    """
    if side == "long":
        candidates = [p for p in htf_pivots if p.kind == "high" and p.price > entry]
        return max(candidates, key=lambda p: p.price).price if candidates else None
    else:
        candidates = [p for p in htf_pivots if p.kind == "low" and p.price < entry]
        return min(candidates, key=lambda p: p.price).price if candidates else None


def rr_ratio(side: Side, entry: float, sl: float, tp: float) -> float:
    """(TP - Entry) / (Entry - SL) für Long, gespiegelt für Short.

    Returns 0.0 bei ungültiger Konstellation (SL falsche Seite, TP falsche Seite).
    """
    if side == "long":
        if entry <= sl or tp <= entry:
            return 0.0
        return (tp - entry) / (entry - sl)
    else:
        if entry >= sl or tp >= entry:
            return 0.0
        return (entry - tp) / (sl - entry)
