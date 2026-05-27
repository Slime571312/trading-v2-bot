"""OHLCV-Fetcher — verbindet Capital-Client + Cache zu einer einfachen API.

Eine Funktion: `await load_candles(instrument, tf, bars=200, refresh=False)`.
- Mapped Playbook-Name → Capital-Epic, Playbook-TF → Capital-Resolution
- Prüft Cache; bei Hit + Frische → DataFrame zurück, kein API-Call
- Sonst → Capital-Call, normalisiert die Antwort, schreibt Cache, gibt DF zurück
"""
from __future__ import annotations

import logging

import httpx
import pandas as pd

from src.brokers import capital_com
from src.config import to_epic, to_resolution
from src.data import cache

log = logging.getLogger(__name__)


def _normalize_prices(prices: list[dict]) -> pd.DataFrame:
    """Capital-Antwort → pandas-DataFrame in unserem Standard-Schema.

    Capital liefert pro Kerze separate bid/ask-Vier-Tupel. Wir nehmen den
    **Mid-Preis** ((bid + ask) / 2) — das ist im Backtest fair, in Etappe 3
    wird der Spread separat als realistic-slippage-Modell drauf gerechnet.
    """
    rows = []
    for p in prices:
        bid = p.get("openPrice", {}).get("bid")
        ask = p.get("openPrice", {}).get("ask")
        if bid is None or ask is None:
            continue
        ts = p.get("snapshotTime") or p.get("snapshotTimeUTC")
        rows.append({
            "time": pd.to_datetime(ts, utc=True),
            "open": (p["openPrice"]["bid"] + p["openPrice"]["ask"]) / 2,
            "high": (p["highPrice"]["bid"] + p["highPrice"]["ask"]) / 2,
            "low": (p["lowPrice"]["bid"] + p["lowPrice"]["ask"]) / 2,
            "close": (p["closePrice"]["bid"] + p["closePrice"]["ask"]) / 2,
            "volume": p.get("lastTradedVolume", 0),
        })
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows).set_index("time").sort_index()
    return df


async def load_candles(
    instrument: str,
    tf: str,
    bars: int = 200,
    refresh: bool = False,
) -> pd.DataFrame:
    """Liefert die letzten `bars` Kerzen für (instrument, tf).

    - `instrument`: Playbook-Name (`DE40`, `NASDAQ`, `SP500`, `BTC`).
    - `tf`: Playbook-TF (`1m`, `5m`, `15m`, `30m`, `1h`, `1d`).
    - `bars`: gewünschte Anzahl (Capital limitiert pro Call, für Etappe 1 ≤ 1000).
    - `refresh=True`: ignoriert Cache, holt frisch.

    Returns DataFrame indexed by UTC-Timestamp, Spalten open/high/low/close/volume.
    """
    epic = to_epic(instrument)
    resolution = to_resolution(tf)

    if not refresh and cache.is_fresh(instrument, tf):
        cached = cache.read(instrument, tf)
        if cached is not None and len(cached) >= bars:
            log.debug("Cache-Hit %s/%s — %d Kerzen", instrument, tf, len(cached))
            return cached.tail(bars)

    async with httpx.AsyncClient(timeout=20.0) as client:
        prices = await capital_com.fetch_prices(client, epic, resolution, max_bars=bars)

    df = _normalize_prices(prices)
    if df.empty:
        log.warning("Capital lieferte 0 Kerzen für %s/%s (epic=%s, res=%s)",
                    instrument, tf, epic, resolution)
        return df

    cache.write(instrument, tf, df)
    log.info("Fetched %d %s-Kerzen für %s (epic=%s, res=%s)",
             len(df), tf, instrument, epic, resolution)
    return df
