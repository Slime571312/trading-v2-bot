"""Liquiditäts-Marker auf HTF — bias-gefiltert.

Quelle: Trading/LQS.md + Trading/Bot/Playbook.md (Schritt 2)

- **Long-Bias**  → suche **SSL** (prominente Pivot-Lows = Sell-Side-Liquidität)
- **Short-Bias** → suche **BSL** (prominente Pivot-Highs = Buy-Side-Liquidität)

Banken sweepen **gegen** die Bias-Richtung, um dort ihre Orders zu füllen.

In Etappe 2 sind ALLE Pivots der relevanten Seite "prominent". Der echte
prominent-Filter (nur BoS-getriggerte Highs) bleibt als TODO für später.
"""
from __future__ import annotations

import pandas as pd

from ._types import BiasDir, Swing
from .pivots import find_pivots


def liquidity_levels(
    df: pd.DataFrame,
    bias_direction: BiasDir,
    n_left: int = 3,
    n_right: int = 3,
    only_unswept: bool = False,
) -> list[Swing]:
    """Liefert relevante Liquiditäts-Levels für die gegebene Bias-Richtung.

    Args:
        df: HTF-OHLCV (z.B. 1h/30m/15m).
        bias_direction: long → SSL (Lows), short → BSL (Highs), neutral → [].
        only_unswept: wenn True, nur Levels deren Wick noch nicht durchbrochen wurde.
    """
    if bias_direction == "neutral":
        return []

    pivots = find_pivots(df, n_left, n_right)
    target_kind = "low" if bias_direction == "long" else "high"

    relevant = [p for p in pivots if p.kind == target_kind]

    if not only_unswept:
        return relevant

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    unswept: list[Swing] = []
    for p in relevant:
        after = slice(p.bar_idx + 1, len(df))
        if target_kind == "low":
            swept = (lows[after] < p.price).any()
        else:
            swept = (highs[after] > p.price).any()
        if not swept:
            unswept.append(p)
    return unswept
