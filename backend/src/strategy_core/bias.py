"""Daily-Bias + Multi-TF-Confluence (Bot/Bias-Detection.md + Dayli Bias.md).

Daily-Bias = Richtung des jüngsten Daily-BoS.
Multi-TF-Confluence: H4-Bias aus dem 4h-Chart als Bestätigung oder Retracement-Marker.

H4-Aligned mit Daily → höchste Conviction (volle Range-TPs erlaubt).
H4 dreht gegen Daily → "Retracement-Setup" (immer noch tradebar in Daily-Richtung
auf Lower-TF-Trigger, aber kürzere TPs und reduzierte Conviction).

Algorithmus (gleich für Daily und H4):
1. Finde alle Pivots im Frame.
2. Für jeden Pivot, finde den ersten nachfolgenden Bar mit Body-Close jenseits.
3. Der jüngste BreakerBar bestimmt den aktiven BoS und damit den Bias.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ._types import Bias, BiasDir
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


def resample_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Aggregiert 1h-Bars zu 4h. Sessions-aligned auf 00/04/08/12/16/20 UTC."""
    if df_1h.empty:
        return df_1h
    agg = df_1h.resample("4h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum" if "volume" in df_1h.columns else "max",
    }).dropna()
    return agg


def compute_h4_bias(df_1h: pd.DataFrame) -> BiasDir:
    """Vereinfachter H4-Bias: BoS auf resamplten 4h-Bars."""
    df_4h = resample_to_4h(df_1h)
    if len(df_4h) < 10:
        return "neutral"
    return compute_bias(df_4h, n_left=2, n_right=2).direction


def multi_tf_confluence(daily_bias: BiasDir, h4_bias: BiasDir) -> dict:
    """Confluence-Klassifikation aus Daily + H4 (Dayli Bias.md Tabelle).

    aligned   → beide gleich, voller Conviction
    retrace   → daily klar, h4 entgegen oder neutral → Retracement-Setup (kürzere TPs)
    no_daily  → kein klarer Daily-Bias → kein Trade
    """
    if daily_bias == "neutral":
        return {"label": "no_daily", "aligned": False, "size_factor": 0.0}
    if h4_bias == daily_bias:
        return {"label": "aligned", "aligned": True, "size_factor": 1.0}
    if h4_bias == "neutral":
        # Daily klar, H4 unklar → moderate Conviction
        return {"label": "h4_neutral", "aligned": False, "size_factor": 0.75}
    # H4 dreht aktiv gegen Daily → Retracement-Setup (laut Vault gültig auf LTF-Trigger)
    return {"label": "retrace", "aligned": False, "size_factor": 0.5}
