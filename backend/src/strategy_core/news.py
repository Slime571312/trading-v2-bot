"""News-Filter — Forex Factory JSON-Endpoint.

Spec aus Trading/Bot/News-Integration.md:
  GET https://nfs.faireconomy.media/ff_calendar_thisweek.json
  Format: [{title, country, date, impact, forecast, previous}]
  Impact: "High" | "Medium" | "Low" | "Holiday"

Hard-Gate vor Entry: ±30/+15 Min um High-Impact-Events für die zugeordneten
Währungen des Instruments. Cache 6h (Wochenwechsel-sicher).

Async-Architektur (Bug-Fix Juni 2026):
- `refresh_calendar_async()` macht den HTTP-Fetch via httpx.AsyncClient — wird
  vor jedem Engine-Eval im async Tick-Loop awaited (idempotent durch Cache).
- `is_news_window()` bleibt sync und liest NUR den Cache — kein HTTP, kein
  Blocking. Wenn der Cache leer ist (frischer Boot ohne Refresh), wird
  fail-open (False) zurückgegeben.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

# Welche Währungen bewegen welches Instrument? (aus News.md, INSTRUMENT_CURRENCIES)
INSTRUMENT_CURRENCIES: dict[str, list[str]] = {
    "DE40":   ["EUR", "USD"],   # DAX reagiert auf beide
    "NASDAQ": ["USD"],
    "SP500":  ["USD"],
    "BTC":    ["USD"],          # Krypto reagiert stark auf USD-Events (FOMC/CPI)
}

# 4 absolute No-Trade-Events (auch bei verändertem Impact-Tag)
NO_TRADE_TITLES = (
    "CPI", "PPI", "FOMC", "NFP", "Non-Farm", "Federal Funds Rate",
    "Interest Rate Decision", "ECB Press Conference",
)

CACHE_TTL_HOURS = 6
ERROR_BACKOFF_MIN = 60   # bei Fehler 1h nicht erneut versuchen
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

_cache: dict = {"data": [], "fetched_at": None, "last_error_at": None}
_refresh_lock = asyncio.Lock()


def _cache_fresh(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return bool(
        _cache["fetched_at"]
        and now - _cache["fetched_at"] < timedelta(hours=CACHE_TTL_HOURS)
    )


def _in_error_backoff(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return bool(
        _cache["last_error_at"]
        and now - _cache["last_error_at"] < timedelta(minutes=ERROR_BACKOFF_MIN)
    )


async def refresh_calendar_async() -> None:
    """Holt den FF-Calendar async; idempotent durch Cache-Check + Lock.

    Wird vor jedem Tick-Eval aus dem async Live-Loop awaited. Bei frischem
    Cache (~6h TTL) ist die Funktion in ~µs durch — kein HTTP-Call. Bei
    Cache-Miss läuft ein einziger httpx.AsyncClient-Request (kein Blocking
    des Event-Loops für andere Bot-Instances).
    """
    async with _refresh_lock:
        now = datetime.now(timezone.utc)
        if _cache_fresh(now) or _in_error_backoff(now):
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(FF_URL, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                _cache["data"] = resp.json()
                _cache["fetched_at"] = now
                _cache["last_error_at"] = None
                log.info("News-Calendar refreshed async: %d Events", len(_cache["data"]))
        except Exception as e:
            _cache["last_error_at"] = now
            log.warning("Forex-Factory async-Fetch fehlgeschlagen (%dmin Backoff): %s",
                        ERROR_BACKOFF_MIN, e)


def _is_high_impact(event: dict) -> bool:
    """High-Impact = Impact:'High' ODER Titel matcht No-Trade-Liste."""
    if event.get("impact") == "High":
        return True
    title = (event.get("title") or "").lower()
    return any(t.lower() in title for t in NO_TRADE_TITLES)


def is_news_window(
    instrument: str,
    now: datetime | None = None,
    before_min: int = 30,
    after_min: int = 15,
) -> tuple[bool, str | None]:
    """True wenn innerhalb ±[before/after] Min ein High-Impact-Event für das
    Instrument liegt. Gibt (bool, event_title oder None) zurück.

    before_min: Sperre VOR dem Event (30 Min Default — Markt baut Volatility auf)
    after_min:  Sperre NACH dem Event (15 Min Default — Initial-Spike vorbei lassen)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    currencies = INSTRUMENT_CURRENCIES.get(instrument, [])
    if not currencies:
        return False, None

    window_before = timedelta(minutes=before_min)
    window_after = timedelta(minutes=after_min)

    # Cache-only lookup (kein HTTP, kein Blocking). Bei leerem Cache fail-open.
    for event in _cache["data"]:
        if not _is_high_impact(event):
            continue
        if event.get("country") not in currencies:
            continue
        try:
            event_time = datetime.fromisoformat(event["date"])
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue

        if (event_time - window_before) <= now <= (event_time + window_after):
            return True, event.get("title", "unknown")
    return False, None


def upcoming_events(instrument: str, hours_ahead: int = 24) -> list[dict]:
    """Nächste High-Impact-Events für Dashboard-Anzeige (Cache-only)."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)
    currencies = INSTRUMENT_CURRENCIES.get(instrument, [])
    out: list[dict] = []
    for ev in _cache["data"]:
        if not _is_high_impact(ev) or ev.get("country") not in currencies:
            continue
        try:
            et = datetime.fromisoformat(ev["date"])
            if et.tzinfo is None:
                et = et.replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        if now <= et <= cutoff:
            out.append({
                "title": ev.get("title", ""),
                "country": ev.get("country", ""),
                "time": et.isoformat(),
                "forecast": ev.get("forecast", ""),
            })
    return sorted(out, key=lambda x: x["time"])
