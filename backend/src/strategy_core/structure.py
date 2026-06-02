"""LTF-Strukturbruch-Detection: BOS und CHoCH nach einem HTF-Sweep.

Quellen: Trading/BOS.md, Trading/CHoCH.md

**BOS (Break of Structure):**
  Bestätigter Strukturbruch — Body-Close auf LTF > letztes Pivot-High (bullish)
  bzw. < letztes Pivot-Low (bearish). Stärker bestätigt, etwas später.

**CHoCH (Change of Character):**
  Erster, früherer Strukturbruch — Body bricht den ERSTEN gegenläufigen Swing
  nach dem Sweep, noch bevor der übergeordnete Trend bestätigt ist.
  Vault: CHoCH auf 1m/5m = Entry-Trigger wenn HTF-Setup vorhanden.
  Früher → besseres RR, aber weniger Bestätigung.

LTF-Pivot-Stärke 2/2 statt 3/3 — auf 1m/5m sind 3er-Pivots zu selten.
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
    """Erster bestätigter BOS in `direction` ab Bar `from_bar_idx + 1`.

    BOS = Body-Close bricht das **letzte bestätigte Pivot** vor dem Sweep.
    Stärker als CHoCH, aber etwas später (mehr Bestätigung).
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


def find_choch_after(
    df: pd.DataFrame,
    direction: Side,
    from_bar_idx: int,
) -> StructureBreak | None:
    """Erster CHoCH (Change of Character) nach einem Sweep.

    CHoCH = Body bricht den **ersten** gegenläufigen Swing der NACH dem Sweep
    entstand — nicht den letzten vor dem Sweep (wie BOS).

    Vault (CHoCH.md): "erster Bruch-Signal auf dem Entry-TF nach dem HTF-Sweep"
    → früherer Entry, weniger Bestätigung als BOS.

    Wenn kein Swing nach dem Sweep geformt wurde → None (zu früh nach Sweep).
    """
    if from_bar_idx >= len(df) - 2:
        return None

    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    times = list(df.index)

    if direction == "long":
        # Suche das erste Pivot-High das NACH dem Sweep entstand (n=1 für Früherkennung)
        choch_target: float | None = None
        choch_bar: int = -1
        for i in range(from_bar_idx + 1, len(df) - 1):
            # Minimaler Pivot: höher als direkte Nachbarn
            if highs[i] > highs[i - 1] and highs[i] > highs[i + 1] if i + 1 < len(df) else True:
                choch_target = highs[i]
                choch_bar = i
                break
        if choch_target is None:
            return None
        body_high = np.maximum(opens, closes)
        for i in range(choch_bar + 1, len(df)):
            if body_high[i] > choch_target:
                from .pivots import Swing as _Swing
                _dummy = _Swing(time=times[choch_bar], price=choch_target,
                                kind="high", bar_idx=choch_bar)
                return StructureBreak(
                    time=times[i], bar_idx=i,
                    body_extreme=float(body_high[i]),
                    broken_swing=_dummy, direction="long",
                )
    else:
        choch_target_low: float | None = None
        choch_bar_low: int = -1
        for i in range(from_bar_idx + 1, len(df) - 1):
            if lows[i] < lows[i - 1] and lows[i] < lows[i + 1] if i + 1 < len(df) else True:
                choch_target_low = lows[i]
                choch_bar_low = i
                break
        if choch_target_low is None:
            return None
        body_low = np.minimum(opens, closes)
        for i in range(choch_bar_low + 1, len(df)):
            if body_low[i] < choch_target_low:
                from .pivots import Swing as _Swing
                _dummy = _Swing(time=times[choch_bar_low], price=choch_target_low,
                                kind="low", bar_idx=choch_bar_low)
                return StructureBreak(
                    time=times[i], bar_idx=i,
                    body_extreme=float(body_low[i]),
                    broken_swing=_dummy, direction="short",
                )
    return None
