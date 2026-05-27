"""LTF-BOS-Detection in Bias-Richtung nach einem Sweep.

Quelle: Trading/BOS.md

**Bullish-BOS:** Body-Close auf LTF > letztes Pivot-High (auf LTF) **nach** dem
HTF-Sweep. Bearish-BOS analog mit Pivot-Low.

LTF nutzt sensitivere Pivot-Stärke (2/2 statt 3/3) — auf 1m/5m sind 3er-Pivots
fast immer ungesehen, 2er sind ein guter Kompromiss zwischen Noise und Latenz.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ._types import Side, StructureBreak
from .pivots import find_pivots, latest_pivot


def find_bos_after(
    df: pd.DataFrame,
    direction: Side,
    from_bar_idx: int,
    n_left: int = 2,
    n_right: int = 2,
) -> StructureBreak | None:
    """Sucht den **ersten** BoS in `direction` ab Bar `from_bar_idx + 1`.

    Args:
        df: LTF-OHLCV (5m oder 1m).
        direction: 'long' (sucht bullish BOS) | 'short' (bearish).
        from_bar_idx: ab welchem Bar gesucht wird (typisch: Sweep-Bar).
        n_left, n_right: Pivot-Stärke für die Suche der zu brechenden Swings.
    """
    if from_bar_idx >= len(df) - 1:
        return None

    pivots = find_pivots(df, n_left, n_right)
    pivots_before = [p for p in pivots if p.bar_idx <= from_bar_idx]
    if not pivots_before:
        return None

    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    times = list(df.index)

    if direction == "long":
        target = latest_pivot(pivots_before, "high")
        if target is None:
            return None
        body_high = np.maximum(opens, closes)
        for i in range(from_bar_idx + 1, len(df)):
            if body_high[i] > target.price:
                return StructureBreak(
                    time=times[i], bar_idx=i,
                    body_extreme=float(body_high[i]),
                    broken_swing=target, direction="long",
                )
    else:
        target = latest_pivot(pivots_before, "low")
        if target is None:
            return None
        body_low = np.minimum(opens, closes)
        for i in range(from_bar_idx + 1, len(df)):
            if body_low[i] < target.price:
                return StructureBreak(
                    time=times[i], bar_idx=i,
                    body_extreme=float(body_low[i]),
                    broken_swing=target, direction="short",
                )
    return None
