"""
CoinGlass Liquidation Heatmap für BTC TP/SL-Platzierung.
Kostenlose API: https://www.coinglass.com/api/coin/BTC/exchange_liquidation_orders?timeType=0
"""
import asyncio
import httpx
from datetime import datetime, timedelta
from typing import Optional


class LiquidityCluster:
    """Liquiditätscluster um bestimmte Preislevel."""
    def __init__(self, level: float, volume: float, direction: str):
        self.level = level
        self.volume = volume  # in USD
        self.direction = direction  # "long" oder "short"


class CoinGlassClient:
    """CoinGlass API Client für BTC Liquidationsdaten."""

    BASE_URL = "https://open-api.coinglass.com/api"
    TIMEOUT = 10

    def __init__(self):
        self.last_fetch: dict = {}
        self.cache_ttl_sec = 300  # 5 min Cache

    async def fetch_liquidation_heatmap(self) -> Optional[dict]:
        """
        Rufe aktuellen Liquidationsheatmap ab.
        Returns: {"long_clusters": [...], "short_clusters": [...]}
        """
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                # CoinGlass Endpoint für BTC Liquidationen
                resp = await client.get(
                    f"{self.BASE_URL}/coin/BTC/exchange_liquidation_orders?timeType=0"
                )
                resp.raise_for_status()
                data = resp.json()

                # Parse Liquidation Heatmap aus der Response
                # Structure: data.get("data", {}) enthält Austausch->Liquidationen mapping
                return self._parse_heatmap(data)
        except (httpx.TimeoutException, httpx.RequestError, ValueError) as e:
            # Silent fail — TP/SL fallback auf Defaults
            print(f"[CoinGlass] Fehler: {e}")
            return None

    def _parse_heatmap(self, raw_data: dict) -> Optional[dict]:
        """Parse CoinGlass-Response zu {long_clusters, short_clusters}."""
        try:
            # Beispiel-Struktur (muss gegen echte API validiert werden):
            # {"data": {"Binance": [...], "Bybit": [...], ...}}
            data = raw_data.get("data", {})

            long_clusters = []
            short_clusters = []

            for exchange, liq_orders in data.items():
                if not isinstance(liq_orders, list):
                    continue

                for order in liq_orders:
                    price = float(order.get("price", 0))
                    amount = float(order.get("amount", 0))
                    direction = order.get("direction", "").lower()

                    if direction == "long":
                        long_clusters.append(LiquidityCluster(price, amount, "long"))
                    elif direction == "short":
                        short_clusters.append(LiquidityCluster(price, amount, "short"))

            # Sortiere nach Volumen (Top-Cluster zuerst)
            long_clusters.sort(key=lambda c: c.volume, reverse=True)
            short_clusters.sort(key=lambda c: c.volume, reverse=True)

            return {
                "long_clusters": long_clusters[:5],  # Top 5
                "short_clusters": short_clusters[:5],
                "timestamp": datetime.utcnow().isoformat()
            }
        except Exception as e:
            print(f"[CoinGlass] Parse-Fehler: {e}")
            return None


def find_tp_near_liquidation(
    entry: float,
    direction: str,
    clusters: list[LiquidityCluster],
    target_r: float
) -> Optional[float]:
    """
    Finde TP-Level nahe bei Liquiditätsclustern.
    - Longs: TP über Entry, bevorzugt Short-Liquidationen (dort treffen Longs hin)
    - Shorts: TP unter Entry, bevorzugt Long-Liquidationen
    """
    if not clusters:
        return None

    if direction == "long":
        # Longs streben nach oben zu Short-Liquidationen
        relevant = [c for c in clusters if c.level > entry]
        if relevant:
            # Nimm das größte Cluster über Entry
            best = max(relevant, key=lambda c: c.volume)
            return best.level
    else:  # short
        # Shorts streben nach unten zu Long-Liquidationen
        relevant = [c for c in clusters if c.level < entry]
        if relevant:
            best = max(relevant, key=lambda c: c.volume)
            return best.level

    return None


def find_sl_away_from_liquidation(
    entry: float,
    direction: str,
    clusters: list[LiquidityCluster],
    default_sl: float,
    buffer_pct: float = 0.005  # 0.5% Buffer über SL-Level
) -> float:
    """
    Finde SL weg von großen Liquiditätsclustern.
    - Longs: SL unter Entry, vermeiden Long-Liquidationen
    - Shorts: SL über Entry, vermeiden Short-Liquidationen
    """
    if direction == "long":
        # SL unter Entry — vermeide die nächsten Long-Liquidationen
        dangerous = [c for c in clusters if c.level < entry and c.direction == "long"]
        if dangerous:
            # Platziere SL unter dem nächsten gefährlichen Cluster
            highest_dangerous = max(dangerous, key=lambda c: c.level)
            sl = highest_dangerous.level * (1 - buffer_pct)
            return min(sl, default_sl)  # Nicht enger als default_sl
    else:  # short
        # SL über Entry — vermeide Short-Liquidationen
        dangerous = [c for c in clusters if c.level > entry and c.direction == "short"]
        if dangerous:
            lowest_dangerous = min(dangerous, key=lambda c: c.level)
            sl = lowest_dangerous.level * (1 + buffer_pct)
            return max(sl, default_sl)  # Nicht größer als default_sl

    return default_sl
