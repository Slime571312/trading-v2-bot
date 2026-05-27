"""Daily-Bias = Richtung des jüngsten Daily-BoS.

Quelle: Trading/Dayli Bias.md + Trading/BOS.md

**Algorithmus** (deterministisch, ohne State-Machine):

1. Finde alle Pivots im Daily.
2. Für jeden Pivot, finde den ERSTEN nachfolgenden Bar dessen Body-Close den
   Pivot durchbricht (Body = max/min von open & close, nicht Wick).
3. Sammle alle solchen (Pivot, BreakerBar)-Paare; der JÜNGSTE BreakerBar ist
   der aktive BoS.
4. Bias-Richtung = Richtung dieses BoS.

Edge-Case: Keine Pivots oder kein Pivot wurde je durchbrochen → Bias `neutral`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ._types import Bias
from .pivots import find_pivots


def compute_bias(
    daily_df: pd.DataFrame,
    n_left: int = 3,
    n_right: int = 3,
) -> Bias:
    """Liefert Daily-Bias basierend auf dem jüngsten Daily-BoS."""
    pivots = find_pivots(daily_df, n_left, n_right)
    if not pivots:
        return Bias(direction="neutral")

    opens = daily_df["open"].to_numpy(dtype=float)
    closes = daily_df["close"].to_numpy(dtype=float)
    body_high = np.maximum(opens, closes)
    body_low = np.minimum(opens, closes)
    times = list(daily_df.index)

    best_break_idx = -1
    best_bias: Bias | None = None

    for pivot in pivots:
        if pivot.bar_idx + 1 >= len(daily_df):
            continue
        if pivot.kind == "high":
            future = body_high[pivot.bar_idx + 1:]
            mask = future > pivot.price
        else:
            future = body_low[pivot.bar_idx + 1:]
            mask = future < pivot.price
        if not mask.any():
            continue

        first_break = int(np.argmax(mask)) + pivot.bar_idx + 1
        if first_break > best_break_idx:
            best_break_idx = first_break
            best_bias = Bias(
                direction="long" if pivot.kind == "high" else "short",
                last_bos_time=times[first_break],
                last_bos_level=pivot.price,
                last_bos_index=first_break,
            )

    return best_bias if best_bias is not None else Bias(direction="neutral")
