"""Kennzahlen-Berechnung — Win-Rate, Profit-Factor, MaxDD, Sharpe, Expectancy."""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd

from ._types import BacktestMetrics, BacktestTrade


def compute_metrics(
    trades: Sequence[BacktestTrade],
    equity_curve: Sequence[tuple[pd.Timestamp, float]],
    initial_capital: float,
    iter_tf: str,
) -> BacktestMetrics:
    if not trades:
        return BacktestMetrics(
            total_trades=0, wins=0, losses=0, win_rate=0.0,
            total_return_pct=0.0, avg_win_r=0.0, avg_loss_r=0.0,
            profit_factor=0.0, max_drawdown_pct=0.0, expectancy_r=0.0,
            sharpe=0.0, exposure_pct=0.0, longs=0, shorts=0,
        )

    rs = np.array([t.r_multiple for t in trades], dtype=float)
    wins = int((rs > 0).sum())
    losses = int((rs <= 0).sum())
    total = len(trades)
    win_rate = wins / total if total else 0.0
    avg_win_r = float(rs[rs > 0].mean()) if wins else 0.0
    avg_loss_r = float(rs[rs <= 0].mean()) if losses else 0.0

    sum_wins = float(rs[rs > 0].sum())
    sum_losses = float(abs(rs[rs <= 0].sum()))
    profit_factor = sum_wins / sum_losses if sum_losses > 0 else float("inf") if sum_wins > 0 else 0.0
    expectancy_r = float(rs.mean())

    final_equity = equity_curve[-1][1] if equity_curve else initial_capital
    total_return_pct = (final_equity - initial_capital) / initial_capital * 100.0

    # Drawdown auf Equity-Curve
    eq_values = np.array([e for _, e in equity_curve], dtype=float)
    if len(eq_values) > 1:
        running_max = np.maximum.accumulate(eq_values)
        dd = (eq_values - running_max) / running_max
        max_dd_pct = float(dd.min() * 100.0)
    else:
        max_dd_pct = 0.0

    # Sharpe vereinfacht: bar-returns auf der Equity-Curve, annualisiert nach TF
    if len(eq_values) > 2:
        bar_returns = np.diff(eq_values) / eq_values[:-1]
        std = float(bar_returns.std())
        mean = float(bar_returns.mean())
        # Bars pro Jahr je TF (24/7 für Crypto, Capital-Open-Hours sonst grob)
        bars_per_year = {
            "1m": 525_600, "5m": 105_120, "15m": 35_040, "30m": 17_520,
            "1h": 8_760, "1d": 365,
        }.get(iter_tf, 525_600)
        sharpe = (mean / std) * math.sqrt(bars_per_year) if std > 0 else 0.0
    else:
        sharpe = 0.0

    # Exposure: Anteil der Zeit im Trade
    total_bars = sum(t.bars_held for t in trades)
    exposure_pct = (total_bars / max(1, len(equity_curve) - 1)) * 100.0

    longs = sum(1 for t in trades if t.side == "long")
    shorts = total - longs

    return BacktestMetrics(
        total_trades=total, wins=wins, losses=losses, win_rate=win_rate,
        total_return_pct=total_return_pct, avg_win_r=avg_win_r, avg_loss_r=avg_loss_r,
        profit_factor=profit_factor, max_drawdown_pct=max_dd_pct,
        expectancy_r=expectancy_r, sharpe=sharpe, exposure_pct=exposure_pct,
        longs=longs, shorts=shorts,
    )
