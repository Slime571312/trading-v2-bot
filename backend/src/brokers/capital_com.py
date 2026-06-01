"""Capital.com REST-Client — Python-Port der v1-`capital-session.ts`-Logik.

Identische Endpoints, identischer Auth-Flow, identische Headers — Capital sieht
keinen Unterschied zwischen einem v1- und einem v2-Aufruf.

Flow:
  1. POST /session   {identifier, password}   Header X-CAP-API-KEY
     → Response liefert CST + X-SECURITY-TOKEN in den Response-Headers
  2. Diese drei Header gehen an alle weiteren Calls (/prices/, /positions, …)
  3. Session-TTL ~10 min — wir cachen 9 min, holen dann neu
  4. Bei 401 → Session invalidieren + retry

Singleton-Session per Modul-Level Lock + Future, damit parallele Tick-Calls
keine parallelen Logins erzeugen (Capital erlaubt nur EINE aktive Session).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from src.config import config

log = logging.getLogger(__name__)

SESSION_TTL_S = 9 * 60  # 9 Minuten, identisch zu v1


class CapitalAuthError(Exception):
    """Login fehlgeschlagen oder Credentials unvollständig."""


class CapitalAPIError(Exception):
    """Capital antwortet mit Fehler (nicht-2xx, nicht 401)."""

    def __init__(self, status: int, body: str):
        super().__init__(f"Capital-API {status}: {body[:200]}")
        self.status = status
        self.body = body


class _Session:
    """Gecachte Capital-Session (CST + X-SECURITY-TOKEN)."""

    __slots__ = ("cst", "token", "expires_at")

    def __init__(self, cst: str, token: str, expires_at: float):
        self.cst = cst
        self.token = token
        self.expires_at = expires_at

    def is_fresh(self) -> bool:
        return time.time() < self.expires_at


_session: _Session | None = None
_session_lock = asyncio.Lock()


async def _create_session(client: httpx.AsyncClient) -> _Session:
    if not config.broker_configured:
        raise CapitalAuthError(
            "Capital.com-Credentials fehlen — setze CAPITAL_API_KEY, "
            "CAPITAL_EMAIL und CAPITAL_API_PASSWORD in der .env."
        )
    res = await client.post(
        f"{config.capital_base_url}/session",
        headers={"X-CAP-API-KEY": config.capital_api_key, "Content-Type": "application/json"},
        json={
            "identifier": config.capital_email,
            "password": config.capital_api_password,
            "encryptedPassword": False,
        },
    )
    if res.status_code != 200:
        raise CapitalAuthError(f"POST /session ergab {res.status_code}: {res.text[:200]}")
    cst = res.headers.get("CST", "")
    token = res.headers.get("X-SECURITY-TOKEN", "")
    if not cst or not token:
        raise CapitalAuthError("Session-Response ohne CST / X-SECURITY-TOKEN")
    log.info("Capital-Session erneuert (demo=%s)", config.is_demo)
    return _Session(cst=cst, token=token, expires_at=time.time() + SESSION_TTL_S)


async def get_session(client: httpx.AsyncClient) -> _Session:
    """Liefert eine frische Session — cached, refresht bei Ablauf, thread-safe."""
    global _session
    if _session and _session.is_fresh():
        return _session
    async with _session_lock:
        if _session and _session.is_fresh():  # erneut prüfen, ein anderer Coroutine hat evtl. schon erneuert
            return _session
        _session = await _create_session(client)
        return _session


def invalidate_session() -> None:
    """Bei 401 vom Capital aufrufen → nächster Call holt neue Session."""
    global _session
    _session = None


def _auth_headers(session: _Session) -> dict[str, str]:
    return {
        "X-CAP-API-KEY": config.capital_api_key,
        "CST": session.cst,
        "X-SECURITY-TOKEN": session.token,
    }


async def request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    retry_on_401: bool = True,
    max_429_retries: int = 4,
    max_network_retries: int = 3,
) -> httpx.Response:
    """Authentifizierter Capital-Call mit automatischem 401-, 429- und Netz-Retry.

    `path` ist relativ zur Base-URL (z.B. `/prices/BTCUSD`).
    Capital rate-limitiert auf ~10 req/s — bei 429 wird mit exponentiellem
    Backoff (0.5/1/2/4 s) bis zu `max_429_retries`-mal wiederholt.

    Transiente Netz-Fehler (DNS-Aussetzer, Connection-Reset, Read-Timeout)
    werden bis `max_network_retries`-mal mit Backoff 1/2/4 s wiederholt —
    der Bot bleibt dadurch über kurze WiFi-/DNS-Hiccups stabil.
    """
    session = await get_session(client)
    url = f"{config.capital_base_url}{path}"

    res: httpx.Response | None = None
    network_attempt = 0
    for attempt in range(max_429_retries + 1):
        try:
            res = await client.request(method, url, headers=_auth_headers(session),
                                       params=params, json=json_body)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError,
                httpx.ConnectTimeout, httpx.WriteTimeout) as e:
            if network_attempt >= max_network_retries:
                raise CapitalAPIError(599, f"Netzwerk-Fehler nach {network_attempt+1} Versuchen: {e}")
            wait = 1.0 * (2 ** network_attempt)
            log.warning("Netz-Fehler bei %s %s (%s) — retry in %.1fs (%d/%d)",
                        method, path, e, wait, network_attempt + 1, max_network_retries)
            await asyncio.sleep(wait)
            network_attempt += 1
            continue
        if res.status_code == 401 and retry_on_401:
            log.warning("401 von Capital → Session refresh + retry %s %s", method, path)
            invalidate_session()
            session = await get_session(client)
            continue
        if res.status_code == 429 and attempt < max_429_retries:
            wait = 0.5 * (2 ** attempt)
            log.info("429 von Capital — backoff %.1fs (Versuch %d/%d) %s",
                     wait, attempt + 1, max_429_retries, path)
            await asyncio.sleep(wait)
            continue
        break

    if res is None:
        raise CapitalAPIError(599, "Kein Response — alle Netz-Retries fehlgeschlagen")
    if res.status_code >= 400:
        raise CapitalAPIError(res.status_code, res.text)
    return res


# ─── Convenience-Wrapper für die Routen, die wir in Etappe 1 brauchen ─────


async def fetch_prices(
    client: httpx.AsyncClient,
    epic: str,
    resolution: str,
    max_bars: int = 200,
) -> list[dict[str, Any]]:
    """GET /prices/{epic}?resolution=...&max=...

    Liefert Capital's `prices`-Array (jede Kerze hat snapshotTime + bid + ask
    + lastTradedVolume — siehe Capital-API-Docs).
    """
    res = await request(
        client,
        "GET",
        f"/prices/{epic}",
        params={"resolution": resolution, "max": max_bars},
    )
    data = res.json()
    return data.get("prices", [])
