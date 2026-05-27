"""Pytest-Helper — synthetische OHLCV-Bars für deterministische Tests."""
from __future__ import annotations

import pandas as pd


_FREQ_MAP = {
    "1m": "1min", "5m": "5min", "15m": "15min",
    "30m": "30min", "1h": "1h", "1d": "1D",
}


def make_bars(
    rows: list[tuple[float, float, float, float]],
    tf: str = "1h",
    start: str = "2026-05-01 09:00",
) -> pd.DataFrame:
    """`rows = [(open, high, low, close), …]` → OHLCV-DataFrame mit UTC-DatetimeIndex."""
    idx = pd.date_range(start=start, periods=len(rows), freq=_FREQ_MAP[tf], tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 0.0
    return df
