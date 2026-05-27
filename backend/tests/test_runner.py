"""Tests für runner — synthetische Multi-TF-Bars + Engine-Replay."""
import pandas as pd
import pytest

from src.backtest.runner import run_backtest
from tests.helpers import make_bars


@pytest.mark.asyncio
async def test_runner_empty_signals_no_trades():
    # Flat Markt → keine Signale, keine Trades, Equity bleibt unverändert
    bars_full = {
        "1d": make_bars([(100, 101, 99, 100)] * 30, tf="1d"),
        "1h": make_bars([(100, 101, 99, 100)] * 200, tf="1h"),
        "5m": make_bars([(100, 101, 99, 100)] * 300, tf="5m"),
    }
    result = await run_backtest(
        "BTC", bars_full,
        initial_capital=10_000.0,
        iter_tf="5m",
        min_warmup_bars=50,
    )
    assert result.metrics.total_trades == 0
    assert result.final_equity == 10_000.0
    assert result.metrics.total_return_pct == 0.0
    assert result.metrics.max_drawdown_pct == 0.0


@pytest.mark.asyncio
async def test_runner_returns_valid_structure():
    # Strukturen-Check: Result hat die richtigen Felder
    bars_full = {
        "1d": make_bars([(100, 101, 99, 100)] * 15, tf="1d"),
        "1h": make_bars([(100, 101, 99, 100)] * 100, tf="1h"),
        "5m": make_bars([(100, 101, 99, 100)] * 150, tf="5m"),
    }
    result = await run_backtest(
        "BTC", bars_full,
        initial_capital=5_000.0,
        iter_tf="5m",
        min_warmup_bars=30,
    )
    assert result.instrument == "BTC"
    assert result.iter_tf == "5m"
    assert result.initial_capital == 5_000.0
    assert len(result.equity_curve) >= 2
    assert isinstance(result.metrics.total_trades, int)
