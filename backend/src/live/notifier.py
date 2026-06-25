"""Telegram-Alerts für den 24/7-Bot (Vault/Bot/Observability.md).

Nutzt die Telegram Bot HTTP-API direkt (kein python-telegram-bot dep) — ein
einzelner POST pro Alert, Fire-and-Forget. Wenn `telegram_bot_token` oder
`telegram_chat_id` in der .env leer sind, sind alle send_*-Funktionen no-op.

Alert-Typen:
- Trade-Open
- Trade-Close (mit R-Multiple + Equity-Update)
- Daily-Loss-Limit erreicht
- Critical Errors (Capital-Outage, State-Corrupt, etc.)

Rate-Limit: Telegram erlaubt 30 msg/sec global. Wir bündeln nicht, aber im
Live-Bot kommen Alerts < 1/min — kein Problem.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from src.config import config

log = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _enabled() -> bool:
    return bool(config.telegram_bot_token and config.telegram_chat_id)


async def send_message(text: str, parse_mode: str = "Markdown") -> bool:
    """Sendet eine Message an die konfigurierte Chat-ID. Returns True bei Erfolg.

    Bei jedem Fehler (Network, 4xx, 5xx) wird der Fehler geloggt aber nicht
    geraised — Alerts sollen NIEMALS einen Trade blockieren.
    """
    if not _enabled():
        return False
    url = _TELEGRAM_API.format(token=config.telegram_bot_token)
    payload: dict[str, Any] = {
        "chat_id": config.telegram_chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(url, json=payload)
            if r.status_code != 200:
                log.warning("Telegram send fehlgeschlagen (%d): %s", r.status_code, r.text[:200])
                return False
            return True
    except Exception as e:
        log.warning("Telegram send Exception: %s", e)
        return False


def fire_and_forget(text: str) -> None:
    """Sync Variante — startet send_message als Background-Task.

    Für Aufrufer die nicht in einem async-Kontext sind oder das Result nicht
    brauchen. Bei keinem laufenden Event-Loop wird stillschweigend gedroppt.
    """
    if not _enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # kein Loop → drop
    loop.create_task(send_message(text))


# ── Hochlevel Alert-Helpers ────────────────────────────────────────────

async def alert_trade_opened(
    instrument: str, side: str, entry: float, sl: float, tp: float, rr: float,
    variant: str, conviction: str | None,
) -> None:
    msg = (
        f"🟢 *{instrument} {side.upper()} OPEN*\n"
        f"Entry `{entry:.2f}`  SL `{sl:.2f}`  TP `{tp:.2f}`\n"
        f"RR `{rr:.2f}`  variant `{variant}`"
        + (f"  conv `{conviction}`" if conviction else "")
    )
    await send_message(msg)


async def alert_trade_closed(
    instrument: str, side: str, entry: float, exit_price: float,
    pnl: float, r_multiple: float, reason: str, equity: float,
) -> None:
    emoji = "✅" if r_multiple > 0 else "🔴"
    msg = (
        f"{emoji} *{instrument} {side.upper()} CLOSED ({reason})*\n"
        f"Entry `{entry:.2f}` → Exit `{exit_price:.2f}`\n"
        f"PnL `{pnl:+.2f}` (`{r_multiple:+.2f}R`)\n"
        f"Equity now: `{equity:.2f}`"
    )
    await send_message(msg)


async def alert_daily_loss_limit(instrument: str, equity: float, loss_pct: float) -> None:
    msg = (
        f"⚠️ *DAILY LOSS LIMIT — {instrument}*\n"
        f"Loss: `{loss_pct:.1f}%`  Equity: `{equity:.2f}`\n"
        f"Bot stoppt neue Trades bis morgen."
    )
    await send_message(msg)


async def alert_critical_error(instrument: str | None, message: str) -> None:
    prefix = f"💥 *CRITICAL — {instrument}*" if instrument else "💥 *CRITICAL*"
    msg = f"{prefix}\n```\n{message[:1000]}\n```"
    await send_message(msg)


async def alert_drawdown_severe(instrument: str, dd_pct: float, equity: float) -> None:
    msg = (
        f"📉 *SEVERE DRAWDOWN — {instrument}*\n"
        f"Drawdown: `{dd_pct:.1f}%`  Equity: `{equity:.2f}`\n"
        f"Equity-Curve-Filter blockt neue Trades bis Recovery."
    )
    await send_message(msg)
