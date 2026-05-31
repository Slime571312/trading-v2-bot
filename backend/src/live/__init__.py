"""Live Multi-Paper-Bot — n Instanzen (1 pro Instrument), je eigene Task/State."""
from .orchestrator import BotOrchestrator, get_orchestrator
from .state import (
    BotInstance, BotState, ClosedTrade, DEFAULT_INSTRUMENTS,
    OpenTrade, TickLogEntry, load_state, save_state,
)

__all__ = [
    "BotInstance", "BotState", "DEFAULT_INSTRUMENTS",
    "OpenTrade", "ClosedTrade", "TickLogEntry",
    "load_state", "save_state",
    "BotOrchestrator", "get_orchestrator",
]
