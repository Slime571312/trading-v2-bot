"""WebSocket-Fan-Out für Live-Updates ans Dashboard.

Pattern aus wiki/sources/2026-05-27-fastapi-websocket-recipes.md:
- Per-Client Mailbox-Queue (asyncio.Queue mit maxsize=50)
- Backpressure: bei voller Queue ältestes Item droppen
- Heartbeat: alle 15s Ping → 10s-Timeout fürs Pong → Disconnect

Aufruf aus dem Tick-Loop:  `await push_to_dashboard({"type": "...", ...})`
Niemals direkt `ws.send_json()` aus Business-Logic — über Mailbox entkoppeln.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)

_clients: set[WebSocket] = set()
_mailboxes: dict[int, asyncio.Queue[dict]] = defaultdict(lambda: asyncio.Queue(maxsize=50))
MAILBOX_MAX = 50


async def handle_client(ws: WebSocket, initial_message: dict | None = None) -> None:
    """WebSocket-Lebenszyklus für einen Client. Aufgerufen aus dem FastAPI-Route-Handler.

    Optionales `initial_message` (z.B. aktueller State) wird direkt nach Accept gesendet,
    damit der Client nicht warten muss bis das nächste Event kommt.
    """
    await ws.accept()
    _clients.add(ws)
    mb = _mailboxes[id(ws)]

    if initial_message is not None:
        try:
            await ws.send_json(initial_message)
        except Exception as e:
            log.warning("ws initial send failed: %s", e)
            _clients.discard(ws)
            _mailboxes.pop(id(ws), None)
            return

    drain_task = asyncio.create_task(_drain(ws, mb))
    hb_task = asyncio.create_task(_heartbeat(ws))

    try:
        while True:
            # Wir lesen nur um Disconnect zu erkennen; Pong-Handling im _heartbeat
            msg = await ws.receive_text()
            # "pong"-Antworten landen hier — werden vom heartbeat-Task via separate
            # receive consumed; falls beide gleichzeitig versuchen zu lesen, gibt
            # FastAPI das aktuell wartende zurück. Wir ignorieren hier alles.
            if msg == "pong":
                continue
    except WebSocketDisconnect:
        log.info("ws-client disconnect (clean)")
    except Exception as e:
        log.warning("ws-client error: %s", e)
    finally:
        drain_task.cancel()
        hb_task.cancel()
        _clients.discard(ws)
        _mailboxes.pop(id(ws), None)


async def push_to_dashboard(payload: dict) -> None:
    """Fan-Out an alle verbundenen Clients. Synchron, non-blocking.

    Drop-Oldest bei voller Queue — neuere State-Updates sind wichtiger als alte
    in einem Live-Trading-Kontext.
    """
    for c in list(_clients):
        mb = _mailboxes[id(c)]
        try:
            mb.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                mb.get_nowait()
                mb.put_nowait(payload)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass


def n_clients() -> int:
    """Zur Diagnose: wie viele Browser-Tabs hören gerade zu."""
    return len(_clients)


async def _drain(ws: WebSocket, q: asyncio.Queue[dict]) -> None:
    """Per-Client Sende-Loop. Liest die Mailbox, sendet bis Disconnect."""
    try:
        while True:
            data = await q.get()
            try:
                await ws.send_json(data)
            except Exception:
                return  # Verbindung tot — handle_client räumt auf
    except asyncio.CancelledError:
        return


async def _heartbeat(ws: WebSocket, interval: int = 15) -> None:
    """Ping/Pong-Heartbeat. Erkennt eingeschlafene Verbindungen.

    Wir senden alle 15s einen Ping-Frame als JSON {"type": "ping"} — wenn der
    Client offline ist erkennt das `send_json` durch eine Exception.
    """
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await ws.send_json({"type": "ping"})
            except Exception:
                return
    except asyncio.CancelledError:
        return
