"""Sweep-Detection — Wick durchsticht Level, Body schließt zurück.

Quelle: Trading/LQS.md

**Definition:**
- **BSL-Sweep** (Buy-Side Liquidity über einem Pivot-High):
  `high[i] > level`  AND  `max(open[i], close[i]) < level`
- **SSL-Sweep** (Sell-Side Liquidity unter einem Pivot-Low):
  `low[i] < level`  AND  `min(open[i], close[i]) > level`

Wir liefern nur den **ersten** Sweep pro Level (alle weiteren am selben Level
sind nur Wiederholungen). Sweeps innerhalb der letzten `lookback` Bars werden
zurückgegeben.
"""
from __future__ import annotations

import pandas as pd

from ._types import Sweep, Swing


def detect_sweeps(
    df: pd.DataFrame,
    levels: list[Swing],
    tf: str,
    lookback: int = 10,
) -> list[Sweep]:
    """Findet alle Sweeps der gegebenen Levels innerhalb der letzten `lookback` Bars.

    Returns: chronologische Liste, ältester zuerst, neuester zuletzt.
    """
    if not levels:
        return []

    n = len(df)
    start = max(0, n - lookback)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    times = list(df.index)

    sweeps: list[Sweep] = []
    for level in levels:
        # Nur Bars nach dem Pivot des Levels betrachten
        scan_start = max(start, level.bar_idx + 1)
        for i in range(scan_start, n):
            if level.kind == "high":
                # BSL: Wick darüber, Body darunter
                if highs[i] > level.price and max(opens[i], closes[i]) < level.price:
                    sweeps.append(Sweep(
                        time=times[i], bar_idx=i, level=level.price,
                        swing=level, direction="bsl", tf=tf,
                    ))
                    break  # nur erster Sweep pro Level
            else:
                # SSL: Wick darunter, Body darüber
                if lows[i] < level.price and min(opens[i], closes[i]) > level.price:
                    sweeps.append(Sweep(
                        time=times[i], bar_idx=i, level=level.price,
                        swing=level, direction="ssl", tf=tf,
                    ))
                    break

    sweeps.sort(key=lambda s: s.bar_idx)
    return sweeps
