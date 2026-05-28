"""Conversation-History + Proposal-Queue, persistent als JSON.

Eine Konversation: ID + Liste der raw-message-dicts (genau das Format das die
Anthropic-API frisst). Mehrere Konversationen parallel möglich (frontend ruft
auf, welche aktiv ist).

Proposals: `propose_config_diff` legt einen Eintrag an. Der User entscheidet
über `/chat/proposals/{id}/apply|reject` — nur dort wird BotState mutiert.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

STATE_DIR = Path(__file__).resolve().parent.parent.parent / "state"
CHAT_FILE = STATE_DIR / "chat_state.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{secrets.token_hex(6)}"


ProposalStatus = Literal["pending", "applied", "rejected"]


@dataclass(slots=True)
class Proposal:
    id: str
    created_at: str
    diff: dict[str, Any]
    rationale: str
    status: ProposalStatus = "pending"
    applied_at: str | None = None
    rejected_at: str | None = None
    conversation_id: str | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Proposal":
        return cls(**d)


@dataclass(slots=True)
class Conversation:
    id: str
    created_at: str
    title: str
    messages: list[dict] = field(default_factory=list)
    model: str = "claude-sonnet-4-6"

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "title": self.title,
            "messages": self.messages,
            "model": self.model,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Conversation":
        return cls(
            id=d["id"], created_at=d["created_at"],
            title=d.get("title", "Neue Konversation"),
            messages=d.get("messages", []),
            model=d.get("model", "claude-sonnet-4-6"),
        )


@dataclass(slots=True)
class ChatState:
    conversations: dict[str, Conversation] = field(default_factory=dict)
    proposals: list[Proposal] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "conversations": {k: v.to_json() for k, v in self.conversations.items()},
            "proposals": [p.to_json() for p in self.proposals],
        }

    @classmethod
    def from_json(cls, d: dict) -> "ChatState":
        return cls(
            conversations={
                k: Conversation.from_json(v) for k, v in d.get("conversations", {}).items()
            },
            proposals=[Proposal.from_json(p) for p in d.get("proposals", [])],
        )


# ── Persistenz (atomar, gleicher Pattern wie live/state.py) ─────────────


def _load() -> ChatState:
    if not CHAT_FILE.exists():
        return ChatState()
    try:
        with CHAT_FILE.open("r", encoding="utf-8") as f:
            return ChatState.from_json(json.load(f))
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.error("Chat-State korrupt (%s) — starte leer", e)
        return ChatState()


def _save(state: ChatState) -> None:
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".chat_state.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state.to_json(), f, indent=2, default=str)
        os.replace(tmp, CHAT_FILE)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ── Manager-Schicht ─────────────────────────────────────────────────────


class ConversationManager:
    """Singleton-Manager. Hält in-Memory State, persistiert bei jeder Mutation."""

    def __init__(self) -> None:
        self._state: ChatState = _load()

    # ── Conversations ───────────────────────────────────────────────
    def list_conversations(self) -> list[Conversation]:
        return sorted(self._state.conversations.values(),
                      key=lambda c: c.created_at, reverse=True)

    def get(self, conv_id: str) -> Conversation | None:
        return self._state.conversations.get(conv_id)

    def new_conversation(self, model: str = "claude-sonnet-4-6", title: str | None = None) -> Conversation:
        conv = Conversation(
            id=_new_id("c_"),
            created_at=_now_iso(),
            title=title or f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            model=model,
        )
        self._state.conversations[conv.id] = conv
        _save(self._state)
        return conv

    def append_messages(self, conv_id: str, new_messages: list[dict]) -> None:
        conv = self._state.conversations.get(conv_id)
        if not conv:
            raise KeyError(f"Conversation {conv_id} nicht gefunden")
        conv.messages.extend(new_messages)
        _save(self._state)

    def set_messages(self, conv_id: str, messages: list[dict]) -> None:
        conv = self._state.conversations.get(conv_id)
        if not conv:
            raise KeyError(f"Conversation {conv_id} nicht gefunden")
        conv.messages = messages
        _save(self._state)

    def update_title(self, conv_id: str, title: str) -> None:
        conv = self._state.conversations.get(conv_id)
        if conv:
            conv.title = title
            _save(self._state)

    def delete(self, conv_id: str) -> bool:
        if conv_id in self._state.conversations:
            del self._state.conversations[conv_id]
            _save(self._state)
            return True
        return False

    # ── Proposals ───────────────────────────────────────────────────
    def add_proposal(self, diff: dict, rationale: str,
                     conversation_id: str | None = None) -> Proposal:
        p = Proposal(
            id=_new_id("p_"),
            created_at=_now_iso(),
            diff=diff,
            rationale=rationale,
            conversation_id=conversation_id,
        )
        self._state.proposals.append(p)
        _save(self._state)
        return p

    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]:
        if status is None:
            return sorted(self._state.proposals, key=lambda p: p.created_at, reverse=True)
        return [p for p in self._state.proposals if p.status == status]

    def get_proposal(self, prop_id: str) -> Proposal | None:
        for p in self._state.proposals:
            if p.id == prop_id:
                return p
        return None

    def mark_proposal(self, prop_id: str, status: ProposalStatus) -> Proposal | None:
        p = self.get_proposal(prop_id)
        if not p or p.status != "pending":
            return None
        p.status = status
        if status == "applied":
            p.applied_at = _now_iso()
        elif status == "rejected":
            p.rejected_at = _now_iso()
        _save(self._state)
        return p


_singleton: ConversationManager | None = None


def get_chat_state() -> ConversationManager:
    """Lazy-Singleton. FastAPI-Lifespan instanziiert beim Startup."""
    global _singleton
    if _singleton is None:
        _singleton = ConversationManager()
    return _singleton
