"""Unit-Tests für Chat-State + Tool-Wrapper (ohne Claude-Code-CLI-Call).

Nach Agent-SDK-Migration:
- Tools sind keine flachen dict-Schemas mehr, sondern @tool-decorated Funktionen
  in einem MCP-Server
- Wir testen direkt die _impl_*-Funktionen + die Closure-Factory für propose
- ConversationManager-Schema änderte sich (turns statt messages, session_id)
"""
from __future__ import annotations

import json
import pytest

from src.chat.state import ConversationManager, ChatTurn, ToolCallRecord
from src.chat.tools import (
    ALLOWED_TOOLS, MCP_SERVER_NAME, TOOL_NAMES,
    _impl_propose_config_diff_factory,
    build_bot_mcp_server,
)


# ── Tool-Setup ────────────────────────────────────────────────────────


def test_tool_names_match_allowed_tools():
    """allowed_tools-Schema muss zu TOOL_NAMES passen (mcp__<server>__<name>)."""
    expected = [f"mcp__{MCP_SERVER_NAME}__{n}" for n in TOOL_NAMES]
    assert ALLOWED_TOOLS == expected


def test_build_mcp_server_returns_config():
    """build_bot_mcp_server returnt eine McpSdkServerConfig (struktur-check)."""
    server = build_bot_mcp_server(conversation_id="c_test")
    # Es ist ein dict mit type='sdk', name, instance
    assert isinstance(server, dict)
    assert server.get("type") == "sdk"
    assert server.get("name") == MCP_SERVER_NAME


def test_build_mcp_server_without_conversation_id():
    """conversation_id ist optional — propose_config_diff bekommt dann None."""
    server = build_bot_mcp_server()
    assert server is not None


# ── propose_config_diff Whitelist-Enforcement ─────────────────────────


@pytest.mark.asyncio
async def test_propose_config_diff_rejects_unknown_keys():
    impl = _impl_propose_config_diff_factory("c_test")
    out = await impl({
        "diff": {"capital_api_key": "ich klau dich"},
        "rationale": "haxxor",
    })
    text = out["content"][0]["text"]
    parsed = json.loads(text)
    assert "error" in parsed
    assert "capital_api_key" in parsed["error"]


@pytest.mark.asyncio
async def test_propose_config_diff_accepts_whitelisted(tmp_path, monkeypatch):
    monkeypatch.setattr("src.chat.state.CHAT_FILE", tmp_path / "chat.json")
    monkeypatch.setattr("src.chat.state._singleton", None)

    impl = _impl_propose_config_diff_factory("c_abc")
    out = await impl({
        "diff": {"rr_threshold": 2.5},
        "rationale": "OOS Sharpe besser bei höherem RR",
    })
    parsed = json.loads(out["content"][0]["text"])
    assert parsed.get("status") == "pending"
    assert parsed["diff"] == {"rr_threshold": 2.5}
    assert parsed["proposal_id"].startswith("p_")


@pytest.mark.asyncio
async def test_propose_config_diff_rejects_empty():
    impl = _impl_propose_config_diff_factory("c_test")
    out = await impl({"diff": {}, "rationale": "nichts"})
    parsed = json.loads(out["content"][0]["text"])
    assert "error" in parsed


# ── ConversationManager (neues turns-Schema) ──────────────────────────


def _fresh_manager(tmp_path, monkeypatch) -> ConversationManager:
    monkeypatch.setattr("src.chat.state.CHAT_FILE", tmp_path / "chat.json")
    monkeypatch.setattr("src.chat.state._singleton", None)
    return ConversationManager()


def test_new_conversation_gets_unique_id(tmp_path, monkeypatch):
    mgr = _fresh_manager(tmp_path, monkeypatch)
    c1 = mgr.new_conversation()
    c2 = mgr.new_conversation()
    assert c1.id != c2.id
    assert c1.id.startswith("c_")
    assert c1.session_id is None


def test_conversation_append_turn_persists(tmp_path, monkeypatch):
    mgr = _fresh_manager(tmp_path, monkeypatch)
    c = mgr.new_conversation()
    mgr.append_turn(c.id, ChatTurn(role="user", text="hi"))
    mgr.append_turn(c.id, ChatTurn(
        role="assistant", text="hello",
        tool_calls=[ToolCallRecord(name="read_status", input={}, output='{"running": false}')],
    ))

    # Manager neu laden
    mgr2 = ConversationManager()
    loaded = mgr2.get(c.id)
    assert loaded is not None
    assert len(loaded.turns) == 2
    assert loaded.turns[0].role == "user"
    assert loaded.turns[0].text == "hi"
    assert loaded.turns[1].role == "assistant"
    assert len(loaded.turns[1].tool_calls) == 1
    assert loaded.turns[1].tool_calls[0].name == "read_status"


def test_conversation_session_id_persists(tmp_path, monkeypatch):
    mgr = _fresh_manager(tmp_path, monkeypatch)
    c = mgr.new_conversation()
    assert c.session_id is None
    mgr.update_session_id(c.id, "claude-session-abc-123")

    mgr2 = ConversationManager()
    loaded = mgr2.get(c.id)
    assert loaded is not None
    assert loaded.session_id == "claude-session-abc-123"


def test_conversation_delete(tmp_path, monkeypatch):
    mgr = _fresh_manager(tmp_path, monkeypatch)
    c = mgr.new_conversation()
    assert mgr.delete(c.id) is True
    assert mgr.delete(c.id) is False
    assert mgr.get(c.id) is None


# ── Proposals (unverändert) ───────────────────────────────────────────


def test_add_and_mark_proposal(tmp_path, monkeypatch):
    mgr = _fresh_manager(tmp_path, monkeypatch)
    p = mgr.add_proposal({"rr_threshold": 2.5}, "rationale here")
    assert p.status == "pending"
    assert p.id.startswith("p_")

    p2 = mgr.mark_proposal(p.id, "applied")
    assert p2 is not None
    assert p2.status == "applied"
    assert p2.applied_at is not None

    p3 = mgr.mark_proposal(p.id, "rejected")
    assert p3 is None  # bereits applied → keine 2. Markierung


def test_list_proposals_with_filter(tmp_path, monkeypatch):
    mgr = _fresh_manager(tmp_path, monkeypatch)
    p1 = mgr.add_proposal({"rr_threshold": 2.0}, "r1")
    p2 = mgr.add_proposal({"rr_threshold": 2.5}, "r2")
    mgr.mark_proposal(p1.id, "applied")

    pending = mgr.list_proposals(status="pending")
    applied = mgr.list_proposals(status="applied")
    all_ = mgr.list_proposals()
    assert len(pending) == 1 and pending[0].id == p2.id
    assert len(applied) == 1 and applied[0].id == p1.id
    assert len(all_) == 2
