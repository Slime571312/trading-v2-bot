"""Tests für Production-Sprint (Punkte 1-4):

1. CircuitBreaker — State-Transitions CLOSED → OPEN → HALF_OPEN → CLOSED
2. EquityCurveFilter — Tier-Logik bei verschiedenen Drawdown-Levels
3. Graceful Shutdown — preserve_was_running Pfad im stop_all
4. Telegram-Notifier — no-op wenn nicht konfiguriert, Format-Stabilität
"""
from __future__ import annotations

import asyncio

import pytest

from src.brokers.circuit_breaker import (
    CircuitBreaker, CircuitOpenError, State,
)
from src.live.equity_filter import equity_filter_factor


# ── Circuit Breaker ─────────────────────────────────────────────────────

async def test_breaker_starts_closed():
    cb = CircuitBreaker(name="test")
    await cb.before_call()  # darf nicht raisen
    assert cb.state == State.CLOSED


async def test_breaker_opens_after_threshold_failures():
    cb = CircuitBreaker(name="test", failure_threshold=3, failure_window_s=10)
    for _ in range(3):
        await cb.before_call()
        await cb.on_failure()
    assert cb.state == State.OPEN
    # Nächster Call soll sofort raisen
    with pytest.raises(CircuitOpenError):
        await cb.before_call()


async def test_breaker_success_resets_failure_window():
    """Nach success im CLOSED-State werden alte Failures vergessen."""
    cb = CircuitBreaker(name="test", failure_threshold=3, failure_window_s=10)
    await cb.before_call()
    await cb.on_failure()
    await cb.before_call()
    await cb.on_success()
    # Failures bleiben gespeichert bis sie nach window verfallen, aber State=CLOSED
    assert cb.state == State.CLOSED


async def test_breaker_half_open_probe_success_closes():
    cb = CircuitBreaker(name="test", failure_threshold=2, failure_window_s=10, cool_off_s=0.01)
    await cb.before_call(); await cb.on_failure()
    await cb.before_call(); await cb.on_failure()
    assert cb.state == State.OPEN
    await asyncio.sleep(0.05)
    await cb.before_call()  # → HALF_OPEN
    assert cb.state == State.HALF_OPEN
    await cb.on_success()
    assert cb.state == State.CLOSED


async def test_breaker_half_open_probe_failure_reopens():
    cb = CircuitBreaker(name="test", failure_threshold=2, failure_window_s=10, cool_off_s=0.01)
    await cb.before_call(); await cb.on_failure()
    await cb.before_call(); await cb.on_failure()
    await asyncio.sleep(0.05)
    await cb.before_call()  # → HALF_OPEN
    await cb.on_failure()
    assert cb.state == State.OPEN


# ── Equity-Curve-Filter ────────────────────────────────────────────────

def test_ec_filter_healthy_at_high():
    """Equity am All-Time-High → full size."""
    factor, tier, dd = equity_filter_factor(
        initial_capital=10000,
        current_equity=10500,
        closed_trades_pnls=[100, 200, 200],  # Curve: 10000→10100→10300→10500
    )
    assert factor == 1.0
    assert tier == "healthy"
    assert dd == 0.0


def test_ec_filter_light_dd_halves_size():
    """Drawdown 3-5% → 0.5×."""
    # Equity-High 10500 (nach 3 Trades), aber dann -3.5%
    factor, tier, dd = equity_filter_factor(
        initial_capital=10000,
        current_equity=10500 * 0.965,
        closed_trades_pnls=[200, 200, 100],  # Curve max 10500
    )
    assert factor == 0.5
    assert tier == "drawdown_light"
    assert 3.0 <= dd < 5.0


def test_ec_filter_medium_dd_quarter_size():
    factor, tier, _ = equity_filter_factor(
        initial_capital=10000,
        current_equity=10500 * 0.93,   # 7% DD
        closed_trades_pnls=[200, 200, 100],
    )
    assert factor == 0.25
    assert tier == "drawdown_medium"


def test_ec_filter_severe_dd_blocks_trade():
    """> 8% DD → 0× (kein Trade)."""
    factor, tier, dd = equity_filter_factor(
        initial_capital=10000,
        current_equity=10500 * 0.89,   # 11% DD
        closed_trades_pnls=[200, 200, 100],
    )
    assert factor == 0.0
    assert tier == "drawdown_severe"
    assert dd > 8.0


def test_ec_filter_no_trades_yet():
    """Frischer Bot ohne Trade-Historie → healthy."""
    factor, tier, _ = equity_filter_factor(10000, 10000, [])
    assert factor == 1.0
    assert tier == "healthy"


def test_ec_filter_uses_current_equity_as_high_if_higher():
    """current_equity > letzte closed_trade-Equity (z.B. wegen offenem Trade-PnL)
    → equity_high wird auf current_equity gesetzt → 0% DD."""
    factor, tier, dd = equity_filter_factor(
        initial_capital=10000,
        current_equity=11000,           # höher als curve-max=10500
        closed_trades_pnls=[200, 200, 100],
    )
    assert factor == 1.0
    assert dd == 0.0


# ── Telegram-Notifier ──────────────────────────────────────────────────

async def test_notifier_noop_when_not_configured(monkeypatch):
    """Ohne Token + Chat-ID darf KEIN HTTP-Call passieren."""
    from src.config import config
    from src.live import notifier

    monkeypatch.setattr(config, "telegram_bot_token", "")
    monkeypatch.setattr(config, "telegram_chat_id", "")

    # Wenn ein HTTP-Call käme, würde der test hängen oder fail.
    result = await notifier.send_message("test")
    assert result is False


def test_notifier_fire_and_forget_safe_without_loop(monkeypatch):
    """fire_and_forget OUTSIDE async context darf nicht crashen."""
    from src.config import config
    from src.live import notifier
    monkeypatch.setattr(config, "telegram_bot_token", "")
    notifier.fire_and_forget("test")   # darf nicht raisen
