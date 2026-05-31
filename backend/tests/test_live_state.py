"""Unit-Tests für Multi-Bot State (BotInstance + BotState) + Persistenz."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.live.state import (
    BotInstance, BotState, ClosedTrade, DEFAULT_INSTRUMENTS,
    OpenTrade, TickLogEntry, TICK_LOG_MAX,
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


# ── Trade-Roundtrips (unverändert) ────────────────────────────────────


def test_open_trade_roundtrip():
    t = _open()
    restored = OpenTrade.from_json(t.to_json())
    assert restored == t


def test_closed_trade_roundtrip():
    t = _closed()
    restored = ClosedTrade.from_json(t.to_json())
    assert restored == t


# ── TickLog ───────────────────────────────────────────────────────────


def test_tick_log_entry_roundtrip():
    e = TickLogEntry(
        timestamp=datetime(2026, 5, 30, tzinfo=timezone.utc),
        action="open", decision="opened_primary",
        bias="long", htf_used="1h", ltf_used="5m",
        rr_computed=3.0, variant="primary",
        detail="long @ 50000", related_trade_id="abc",
    )
    restored = TickLogEntry.from_json(e.to_json())
    assert restored == e


def test_bot_instance_tick_log_circular():
    inst = BotInstance(instrument="BTC")
    for i in range(TICK_LOG_MAX + 20):
        inst.append_tick_log(TickLogEntry(
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            action="eval", decision=f"step-{i}",
        ))
    assert len(inst.tick_log) == TICK_LOG_MAX
    # ältester Eintrag wurde gedroppt
    assert inst.tick_log[0].decision == f"step-20"


# ── BotInstance + BotState Roundtrips ─────────────────────────────────


def test_bot_instance_roundtrip():
    inst = BotInstance(
        instrument="BTC",
        running=True,
        equity=12_500.0,
        rr_threshold=2.5,
        sweep_lookback=15,
        open_trades=[_open("o1")],
        closed_trades=[_closed("c1")],
    )
    restored = BotInstance.from_json(inst.to_json())
    assert restored.instrument == "BTC"
    assert restored.equity == 12_500.0
    assert restored.rr_threshold == 2.5
    assert len(restored.open_trades) == 1
    assert len(restored.closed_trades) == 1


def test_bot_state_roundtrip_default():
    state = BotState()
    state.ensure("BTC")
    state.ensure("DE40")
    restored = BotState.from_json(state.to_json())
    assert sorted(restored.instances.keys()) == ["BTC", "DE40"]


def test_bot_state_total_equity():
    state = BotState()
    btc = state.ensure("BTC")
    btc.equity = 10_500.0
    de = state.ensure("DE40")
    de.equity = 9_800.0
    assert state.total_equity() == 20_300.0


def test_bot_state_any_running():
    state = BotState()
    state.ensure("BTC")
    de = state.ensure("DE40")
    de.running = True
    assert state.any_running() is True


def test_ensure_creates_instance_with_defaults():
    state = BotState()
    inst = state.ensure("BTC")
    assert inst.instrument == "BTC"
    assert inst.equity == 10_000.0
    assert inst.rr_threshold == 2.0
    assert inst.running is False


# ── Persistenz + Migration ────────────────────────────────────────────


def test_save_and_load_state_multi_instance(tmp_path, monkeypatch):
    monkeypatch.setattr("src.live.state.STATE_FILE", tmp_path / "test_state.json")

    state = BotState()
    btc = state.ensure("BTC")
    btc.running = True
    btc.equity = 11_000.0
    btc.open_trades.append(_open())
    btc.closed_trades.append(_closed())

    de = state.ensure("DE40")
    de.rr_threshold = 2.8

    save_state(state)
    assert (tmp_path / "test_state.json").exists()

    loaded = load_state()
    # running wird beim Load auf False resettet
    assert loaded.instances["BTC"].running is False
    assert loaded.instances["BTC"].equity == 11_000.0
    assert len(loaded.instances["BTC"].open_trades) == 1
    assert loaded.instances["DE40"].rr_threshold == 2.8


def test_load_state_missing_file_returns_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr("src.live.state.STATE_FILE", tmp_path / "nonexistent.json")
    state = load_state()
    # 4 leere Default-Instanzen
    assert sorted(state.instances.keys()) == sorted(DEFAULT_INSTRUMENTS)
    for inst in state.instances.values():
        assert inst.running is False
        assert inst.equity == inst.initial_capital


def test_load_state_corrupt_returns_fresh(tmp_path, monkeypatch):
    fake_file = tmp_path / "corrupt.json"
    fake_file.write_text("{nicht-valid-json")
    monkeypatch.setattr("src.live.state.STATE_FILE", fake_file)
    state = load_state()
    assert sorted(state.instances.keys()) == sorted(DEFAULT_INSTRUMENTS)


def test_load_state_old_single_bot_schema_migrates_to_fresh(tmp_path, monkeypatch):
    """Alte single-bot State (kein 'instances'-Key) → verworfen, 4 frische."""
    import json
    fake_file = tmp_path / "old_state.json"
    fake_file.write_text(json.dumps({
        "running": False, "equity": 9999.0,
        "open_trades": [], "closed_trades": [],
        # alte Felder ohne 'instances'
    }))
    monkeypatch.setattr("src.live.state.STATE_FILE", fake_file)
    state = load_state()
    assert sorted(state.instances.keys()) == sorted(DEFAULT_INSTRUMENTS)
    # alte Werte sollen NICHT durchgesickert sein
    for inst in state.instances.values():
        assert inst.equity == inst.initial_capital


def test_save_state_ensures_default_instruments_present(tmp_path, monkeypatch):
    """Wenn user nur BTC angelegt hat, ergänzt load_state automatisch die 3 anderen Defaults."""
    monkeypatch.setattr("src.live.state.STATE_FILE", tmp_path / "partial.json")
    state = BotState()
    state.ensure("BTC")
    save_state(state)

    loaded = load_state()
    assert "BTC" in loaded.instances
    # load_state füllt fehlende Defaults nach
    for inst_name in DEFAULT_INSTRUMENTS:
        assert inst_name in loaded.instances
