"""Unit-Tests für Chat-State + Tool-Dispatch (ohne Anthropic-API)."""
from __future__ import annotations

import json
import pytest

from src.chat.state import ConversationManager
from src.chat.tools import cached_tools, dispatch_tool, TOOLS


# ── Tools-Definitionen ────────────────────────────────────────────────


def test_tools_well_formed():
    """Alle Tools haben name, description, input_schema."""
    for t in TOOLS:
        assert "name" in t and isinstance(t["name"], str)
        assert "description" in t and len(t["description"]) > 20
        assert "input_schema" in t
        assert t["input_schema"]["type"] == "object"


def test_cached_tools_marks_last_only():
    """cache_control nur auf dem letzten Tool — Anthropic cacht dann alle bis dahin."""
    tools = cached_tools()
    for i, t in enumerate(tools):
        if i == len(tools) - 1:
            assert "cache_control" in t
            assert t["cache_control"] == {"type": "ephemeral"}
        else:
            assert "cache_control" not in t


def test_cached_tools_does_not_mutate_originals():
    """TOOLS soll auch nach cached_tools() unverändert sein."""
    snapshot = json.dumps(TOOLS)
    _ = cached_tools()
    assert json.dumps(TOOLS) == snapshot


# ── propose_config_diff Whitelist-Enforcement ─────────────────────────


@pytest.mark.asyncio
async def test_propose_config_diff_rejects_unknown_keys():
    out = await dispatch_tool("propose_config_diff", {
        "diff": {"capital_api_key": "ich klau dich"},
        "rationale": "haxxor",
    })
    parsed = json.loads(out)
    assert "error" in parsed
    assert "capital_api_key" in parsed["error"]


@pytest.mark.asyncio
async def test_propose_config_diff_accepts_whitelisted_keys(tmp_path, monkeypatch):
    # Chat-State auf temp-File umlenken
    monkeypatch.setattr("src.chat.state.CHAT_FILE", tmp_path / "chat.json")
    monkeypatch.setattr("src.chat.state._singleton", None)

    out = await dispatch_tool("propose_config_diff", {
        "diff": {"rr_threshold": 2.5},
        "rationale": "OOS Sharpe besser bei höherem RR",
    }, conversation_id="c_test")
    parsed = json.loads(out)
    assert parsed.get("status") == "pending"
    assert parsed["diff"] == {"rr_threshold": 2.5}
    assert parsed["proposal_id"].startswith("p_")


@pytest.mark.asyncio
async def test_propose_config_diff_rejects_empty():
    out = await dispatch_tool("propose_config_diff", {
        "diff": {},
        "rationale": "nichts",
    })
    parsed = json.loads(out)
    assert "error" in parsed


@pytest.mark.asyncio
async def test_dispatch_unknown_tool():
    out = await dispatch_tool("hack_the_planet", {})
    parsed = json.loads(out)
    assert "error" in parsed
    assert "unknown tool" in parsed["error"]


# ── ConversationManager ────────────────────────────────────────────────


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


def test_conversation_append_persists(tmp_path, monkeypatch):
    mgr = _fresh_manager(tmp_path, monkeypatch)
    c = mgr.new_conversation()
    mgr.append_messages(c.id, [{"role": "user", "content": "hi"}])
    # Manager neu laden = State von Disk
    mgr2 = ConversationManager()
    loaded = mgr2.get(c.id)
    assert loaded is not None
    assert len(loaded.messages) == 1
    assert loaded.messages[0]["content"] == "hi"


def test_conversation_delete(tmp_path, monkeypatch):
    mgr = _fresh_manager(tmp_path, monkeypatch)
    c = mgr.new_conversation()
    assert mgr.delete(c.id) is True
    assert mgr.delete(c.id) is False  # 2× ist False
    assert mgr.get(c.id) is None


# ── Proposals ──────────────────────────────────────────────────────────


def test_add_and_mark_proposal(tmp_path, monkeypatch):
    mgr = _fresh_manager(tmp_path, monkeypatch)
    p = mgr.add_proposal({"rr_threshold": 2.5}, "rationale here")
    assert p.status == "pending"
    assert p.id.startswith("p_")

    p2 = mgr.mark_proposal(p.id, "applied")
    assert p2 is not None
    assert p2.status == "applied"
    assert p2.applied_at is not None

    # Erneut markieren → None (war nicht pending)
    p3 = mgr.mark_proposal(p.id, "rejected")
    assert p3 is None


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
