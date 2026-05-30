"""Chat-Bot via Claude Agent SDK — nutzt Claude-Code-Subscription.

Spec: Trading/Bot/Chat-Engine.md
Agent-SDK statt direkter Anthropic-API → kein ANTHROPIC_API_KEY nötig,
läuft über die `claude`-CLI des Users.

Tools werden als MCP-Server gebündelt (chat/tools.py), Multi-Turn-Loop
+ Session-Resumption übernimmt die SDK selbst (chat/client.py).
"""
from .client import (
    ChatClient,
    ChatTurnResult,
    get_chat_client,
    is_available,
    run_turn,
)
from .state import (
    ChatTurn,
    Conversation,
    ConversationManager,
    Proposal,
    ProposalStatus,
    ToolCallRecord,
    get_chat_state,
)

__all__ = [
    "ChatClient", "ChatTurnResult", "get_chat_client", "is_available", "run_turn",
    "ChatTurn", "Conversation", "ConversationManager",
    "Proposal", "ProposalStatus", "ToolCallRecord",
    "get_chat_state",
]
