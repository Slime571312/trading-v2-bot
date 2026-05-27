"""Live-Paper-Bot — asyncio Tick-Loop ruft Strategy-Core, persistiert State.

Spec: Trading/Bot/Architektur-v2.md (Etappe 5) + Trading/Bot/Execution.md.

**Pragmatische Architektur-Wahl:** REST-Polling alle 60s statt Capital-WebSocket.
Begründung:
- 5m-Strategie braucht keine Sub-Sekunden-Latenz
- Cache-TTLs (60s für 1m, 300s für 5m) sind natürlicher Tick-Rhythmus
- WS-Reconnect-Logik (Spec aus Execution.md) bleibt für Etappe 7-Refinement

Der **Frontend-Push** (FastAPI-WebSocket → Browser) ist Pflicht und gebaut.
"""
from .state import BotState, OpenTrade, ClosedTrade, load_state, save_state
from .orchestrator import BotOrchestrator, get_orchestrator

__all__ = [
    "BotState", "OpenTrade", "ClosedTrade",
    "load_state", "save_state",
    "BotOrchestrator", "get_orchestrator",
]
