"""Backtest-Engine — historischer Replay des Strategy-Cores.

Spec: ~/Desktop/tradingbot/Trading/Bot/Backtest-Methodik.md
Slippage 1:1 aus dem Vault (5-Komponenten-Modell, OHLCV-only).
"""
from ._types import BacktestMetrics, BacktestResult, BacktestTrade, ExitReason
from .runner import run_backtest

__all__ = [
    "BacktestMetrics", "BacktestResult", "BacktestTrade", "ExitReason",
    "run_backtest",
]
