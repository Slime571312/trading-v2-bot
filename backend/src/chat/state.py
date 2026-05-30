"""Conversation-History + Proposal-Queue, persistent als JSON.

Schema-Vereinfachung mit Agent-SDK: statt roher Anthropic-Content-Blocks
speichern wir nur eine UI-freundliche Liste aus ChatTurn-Objekten + die
Claude-Code-`session_id` für Session-Resumption. Der eigentliche Conversation-
Context lebt in Claude Codes Session-Store.

Proposals: `propose_config_diff` legt einen Eintrag an. Der User entscheidet
über `/chat/proposals/{id}/apply|reject`.
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
class ToolCallRecord:
    """Eine Tool-Invocation in einem Turn — Display-only."""
    name: str  # ohne mcp__-Präfix (kompakter im UI)
    input: dict[str, Any]
    output: str  # JSON-String oder Plaintext


@dataclass(slots=True)
class ChatTurn:
    role: Literal["user", "assistant"]
    text: str  # finale Antwort (assistant) bzw. User-Eingabe
    timestamp: str = field(default_factory=_now_iso)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "role": self.role,
            "text": self.text,
            "timestamp": self.timestamp,
            "tool_calls": [asdict(tc) for tc in self.tool_calls],
        }

    @classmethod
    def from_json(cls, d: dict) -> "ChatTurn":
        return cls(
            role=d["role"],
            text=d.get("text", ""),
            timestamp=d.get("timestamp", _now_iso()),
            tool_calls=[ToolCallRecord(**tc) for tc in d.get("tool_calls", [])],
        )


@dataclass(slots=True)
class Conversation:
    id: str
    created_at: str
    title: str
    turns: list[ChatTurn] = field(default_factory=list)
    model: str | None = None  # None = Claude Code default
    session_id: str | None = None  # Claude-Code-Session zum resumen

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "title": self.title,
            "turns": [t.to_json() for t in self.turns],
            "model": self.model,
            "session_id": self.session_id,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Conversation":
        # Backward-compat: alte Konversationen hatten `messages` (Anthropic-Format).
        # Die werfen wir weg — sie sind mit dem neuen Schema inkompatibel.
        turns_json = d.get("turns", [])
        return cls(
            id=d["id"], created_at=d["created_at"],
            title=d.get("title", "Neue Konversation"),
            turns=[ChatTurn.from_json(t) for t in turns_json],
            model=d.get("model"),
            session_id=d.get("session_id"),
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
        out = cls()
        for k, v in d.get("conversations", {}).items():
            try:
                out.conversations[k] = Conversation.from_json(v)
            except (KeyError, TypeError) as e:
                log.warning("Conversation %s übersprungen (alt/inkompatibel): %s", k, e)
        out.proposals = [Proposal.from_json(p) for p in d.get("proposals", [])]
        return out


# ── Persistenz (atomar) ─────────────────────────────────────────────────


def _load() -> ChatState:
    if not CHAT_FILE.exists():
        return ChatState()
    try:
        with CHAT_FILE.open("r", encoding="utf-8") as f:
            return ChatState.from_json(json.load(f))
    except (json.JSONDecodeError, ValueError) as e:
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


# ── Manager-Singleton ───────────────────────────────────────────────────


class ConversationManager:
    def __init__(self) -> None:
        self._state: ChatState = _load()

    # ── Conversations ───────────────────────────────────────────────
    def list_conversations(self) -> list[Conversation]:
        return sorted(self._state.conversations.values(),
                      key=lambda c: c.created_at, reverse=True)

    def get(self, conv_id: str) -> Conversation | None:
        return self._state.conversations.get(conv_id)

    def new_conversation(self, model: str | None = None, title: str | None = None) -> Conversation:
        conv = Conversation(
            id=_new_id("c_"),
            created_at=_now_iso(),
            title=title or f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            model=model,
        )
        self._state.conversations[conv.id] = conv
        _save(self._state)
        return conv

    def append_turn(self, conv_id: str, turn: ChatTurn) -> None:
        conv = self._state.conversations.get(conv_id)
        if not conv:
            raise KeyError(f"Conversation {conv_id} nicht gefunden")
        conv.turns.append(turn)
        _save(self._state)

    def update_session_id(self, conv_id: str, session_id: str) -> None:
        conv = self._state.conversations.get(conv_id)
        if conv and session_id and conv.session_id != session_id:
            conv.session_id = session_id
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
    global _singleton
    if _singleton is None:
        _singleton = ConversationManager()
    return _singleton
