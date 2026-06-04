"""Crypto-Sentiment für BTC — Funding-Rate-Check (Bot/Crypto-Sentiment.md).

Funding-Rate aus Binance USDT-M Perpetual Futures als externer Stimmungsindikator
für BTC. Capital.com BTC/USD ist ein CFD ohne Funding, aber Binance-Perp bewegt
denselben Preis und liefert die Crowd-Positionierung kostenlos via REST.

Public Endpoint, kein API-Key nötig:
  GET https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT

Cache: 30min (Funding updated alle 8h, also reichlich Headroom).

Bias-Modifier-Regeln (aus Vault):
  Rate > +0.10%  (8h)  → BTC Long ist überhitzt — Long-Entries als Kontradiktion markieren
  Rate < -0.05%  (8h)  → BTC Short überhitzt (Squeeze-Risiko) — Short-Entries als Kontradiktion
  Sonst                  → kein Modifier
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

BINANCE_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
SYMBOL = "BTCUSDT"

# Schwellwerte aus Vault (per 8h-Periode)
LONG_OVERHEAT = 0.0010   # +0.10% = überhitzt Long
SHORT_OVERHEAT = -0.0005  # -0.05% = überhitzt Short (Squeeze)

CACHE_TTL_MIN = 30
_ERROR_BACKOFF_MIN = 30

_cache: dict = {"rate": None, "fetched_at": None, "last_error_at": None}


def _fetch_funding_rate() -> float | None:
    """Holt aktuelle Funding-Rate von Binance, cached 30 Min."""
    now = datetime.now(timezone.utc)

    if (_cache["fetched_at"] and
            now - _cache["fetched_at"] < timedelta(minutes=CACHE_TTL_MIN)):
        return _cache["rate"]

    if (_cache["last_error_at"] and
            now - _cache["last_error_at"] < timedelta(minutes=_ERROR_BACKOFF_MIN)):
        return _cache["rate"]   # alte Werte (oder None) zurückgeben

    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(BINANCE_PREMIUM_URL, params={"symbol": SYMBOL})
            resp.raise_for_status()
            data = resp.json()
            rate = float(data.get("lastFundingRate", 0))
            _cache["rate"] = rate
            _cache["fetched_at"] = now
            _cache["last_error_at"] = None
            log.info("BTC Funding-Rate: %.5f%% (8h-Periode)", rate * 100)
            return rate
    except Exception as e:
        _cache["last_error_at"] = now
        log.warning("Binance Funding-Fetch fehlgeschlagen (%dmin Backoff): %s",
                    _ERROR_BACKOFF_MIN, e)
        return _cache["rate"]


def is_funding_contra(side: str) -> bool:
    """True wenn die aktuelle BTC Funding-Rate gegen die geplante Richtung läuft.

    Long bei Funding > +0.10% → True (Crowd überladen Long, Reversal-Risiko)
    Short bei Funding < -0.05% → True (Crowd überladen Short, Squeeze-Risiko)
    """
    rate = _fetch_funding_rate()
    if rate is None:
        return False  # kein Datum → keine Kontradiktion (fail-open für seltenen Fall)

    if side == "long" and rate > LONG_OVERHEAT:
        return True
    if side == "short" and rate < SHORT_OVERHEAT:
        return True
    return False


def get_funding_rate() -> float | None:
    """Aktueller Wert für Dashboard/Logging."""
    return _fetch_funding_rate()
