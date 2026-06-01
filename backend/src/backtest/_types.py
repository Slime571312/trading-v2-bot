"""Backtest-Dataclasses — Trade, Metrics, Result."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from src.strategy_core._types import Side, SignalVariant

ExitReason = Literal["sl", "tp", "end_of_data"]


@dataclass(slots=True)
class BacktestTrade:
    open_time: pd.Timestamp
    close_time: pd.Timestamp
    side: Side
    entry: float            # nach Slippage
    exit: float             # nach Slippage
    sl: float               # Stop-Loss-Level (ohne Slippage; intrabar getriggert)
    tp: float               # Take-Profit-Level
    size: float             # Units (risk_per_trade / sl_distance)
    variant: SignalVariant
    htf_used: str
    ltf_used: str
    bars_held: int
    pnl_abs: float          # Risiko-normalisierte PnL in Equity-Einheiten
    pnl_pct: float          # PnL als Prozent des Notional (entry * size)
    r_multiple: float       # PnL in Vielfachen des Risikos (1.0 = +1R, -1.0 = SL hit)
    exit_reason: ExitReason


@dataclass(slots=True)
class BacktestMetrics:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_return_pct: float
    avg_win_r: float
    avg_loss_r: float
    profit_factor: float | None   # sum(wins) / abs(sum(losses)); None wenn unbestimmt (keine Losses)
    max_drawdown_pct: float
    expectancy_r: float           # avg R-multiple per trade
    sharpe: float | None          # vereinfacht: avg/std × √(bars_per_year); None wenn std==0
    exposure_pct: float           # % der Zeit im Trade
    longs: int
    shorts: int


@dataclass(slots=True)
class BacktestResult:
    instrument: str
    iter_tf: str
    start: pd.Timestamp
    end: pd.Timestamp
    initial_capital: float
    final_equity: float
    metrics: BacktestMetrics
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple[pd.Timestamp, float]] = field(default_factory=list)
