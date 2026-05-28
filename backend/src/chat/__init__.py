"""Chat-Bot — Anthropic Claude API mit Tool-Use, Multi-Turn-Loop.

Spec: Trading/Bot/Chat-Engine.md + wiki/sources/2026-05-27-anthropic-advanced-tool-use.md
Scope (aus Decisions.md): Read ✓ · Trigger ✓ · Vorschlag ✓ · Direkt-Schreiben ✗

Tools werden in tools.py definiert + dispatched. ConversationManager (in state.py)
hält Multi-Turn-History pro conversation_id. ChatClient (in client.py) macht den
eigentlichen Anthropic-API-Call inkl. tool-loop und prompt-caching.
"""
from .state import (
    ConversationManager,
    Proposal,
    ProposalStatus,
    get_chat_state,
)
from .client import ChatClient, get_chat_client

__all__ = [
    "ConversationManager", "Proposal", "ProposalStatus",
    "get_chat_state", "ChatClient", "get_chat_client",
]
