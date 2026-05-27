"""Pivot-/Swing-Detection — Basis-Helper für alle Strukturmodule.

Klassischer Fractal-Pivot: Ein Pivot-High bei Bar i ist eine Kerze, deren `high`
**strikt** größer ist als die `n_left` Bars davor UND die `n_right` Bars danach.
Pivot-Low analog mit `low`.

Mit `n_right > 0` sieht das Modul **nur bestätigte Pivots** — der jüngste Pivot
wird erst `n_right` Bars später sichtbar. Das ist gewollt: kein Look-ahead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ._types import Swing


def find_pivots(df: pd.DataFrame, n_left: int = 3, n_right: int = 3) -> list[Swing]:
    """Liefert alle bestätigten Pivot-Highs und Pivot-Lows in chronologischer Reihenfolge.

    Args:
        df: OHLCV-DataFrame, DatetimeIndex.
        n_left: Wie viele Bars links niedriger / höher sein müssen.
        n_right: Analog rechts.

    Returns:
        Liste von `Swing`, sortiert nach `bar_idx`.
    """
    if len(df) < n_left + n_right + 1:
        return []

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    times = list(df.index)
    swings: list[Swing] = []

    for i in range(n_left, len(df) - n_right):
        center_high = highs[i]
        left = highs[i - n_left:i]
        right = highs[i + 1:i + n_right + 1]
        if (left < center_high).all() and (right < center_high).all():
            swings.append(Swing(
                time=times[i], price=float(center_high), kind="high", bar_idx=i,
            ))

        center_low = lows[i]
        left = lows[i - n_left:i]
        right = lows[i + 1:i + n_right + 1]
        if (left > center_low).all() and (right > center_low).all():
            swings.append(Swing(
                time=times[i], price=float(center_low), kind="low", bar_idx=i,
            ))

    swings.sort(key=lambda s: s.bar_idx)
    return swings


def latest_pivot(pivots: list[Swing], kind: str, before_idx: int | None = None) -> Swing | None:
    """Jüngster Pivot eines Typs, optional vor einem bestimmten Bar-Index."""
    candidates = [p for p in pivots if p.kind == kind]
    if before_idx is not None:
        candidates = [p for p in candidates if p.bar_idx <= before_idx]
    return max(candidates, key=lambda p: p.bar_idx) if candidates else None
