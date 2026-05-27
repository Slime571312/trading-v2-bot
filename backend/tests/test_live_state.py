"""Unit-Tests für State-Persistenz — Roundtrip JSON ↔ Dataclass + Atomic-Save."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.live.state import (
    BotState, ClosedTrade, OpenTrade,
    load_state, save_state, STATE_FILE,
)


def _open(id_: str = "abc123") -> OpenTrade:
    return OpenTrade(
        id=id_, instrument="BTC", side="long",
        open_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        entry=50_000, sl=49_900, tp=50_300, size=1.0,
        variant="primary", htf_used="1h", ltf_used="5m", rr_at_open=3.0,
    )


def _closed(id_: str = "def456") -> ClosedTrade:
    return ClosedTrade(
        id=id_, instrument="ETH", side="short",
        open_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
        entry=3_000, exit=2_950, sl=3_050, tp=2_900, size=2.0,
        variant="primary", htf_used="1h", ltf_used="5m",
        rr_at_open=2.0, pnl_abs=100.0, pnl_pct=1.67, r_multiple=1.0,
        exit_reason="tp",
    )


def test_open_trade_roundtrip():
    t = _open()
    restored = OpenTrade.from_json(t.to_json())
    assert restored == t


def test_closed_trade_roundtrip():
    t = _closed()
    restored = ClosedTrade.from_json(t.to_json())
    assert restored == t


def test_bot_state_roundtrip_default():
    state = BotState()
    restored = BotState.from_json(state.to_json())
    assert restored.equity == state.equity
    assert restored.initial_capital == state.initial_capital
    assert restored.instruments == state.instruments


def test_bot_state_roundtrip_with_trades():
    state = BotState(
        running=True,
        started_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        equity=10_500.0,
        open_trades=[_open("o1"), _open("o2")],
        closed_trades=[_closed("c1")],
    )
    restored = BotState.from_json(state.to_json())
    assert len(restored.open_trades) == 2
    assert len(restored.closed_trades) == 1
    assert restored.equity == 10_500.0
    assert restored.started_at == state.started_at


def test_save_and_load_state(tmp_path, monkeypatch):
    """save_state → file → load_state ergibt Original (außer running=False reset)."""
    fake_file = tmp_path / "test_state.json"
    monkeypatch.setattr("src.live.state.STATE_FILE", fake_file)

    state = BotState(
        running=True,
        equity=11_000.0,
        open_trades=[_open()],
        closed_trades=[_closed()],
    )
    save_state(state)
    assert fake_file.exists()

    loaded = load_state()
    # running wird beim Load auf False resettet (kein Auto-Start nach Backend-Restart)
    assert loaded.running is False
    assert loaded.equity == 11_000.0
    assert len(loaded.open_trades) == 1
    assert len(loaded.closed_trades) == 1


def test_load_state_missing_file_returns_default(tmp_path, monkeypatch):
    fake_file = tmp_path / "nonexistent.json"
    monkeypatch.setattr("src.live.state.STATE_FILE", fake_file)
    state = load_state()
    assert state.running is False
    assert state.equity == state.initial_capital
    assert len(state.open_trades) == 0


def test_load_state_corrupt_returns_default(tmp_path, monkeypatch):
    fake_file = tmp_path / "corrupt.json"
    fake_file.write_text("{nicht-valid-json")
    monkeypatch.setattr("src.live.state.STATE_FILE", fake_file)
    state = load_state()
    assert state.running is False
    assert state.equity == state.initial_capital
