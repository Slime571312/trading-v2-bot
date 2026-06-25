"""Monte Carlo Backtesting Framework — Shuffle & Resample Methods.

Spec: Trading/Bot/Monte-Carlo-Backtesting.md

Methode 1: Shuffle — Trade-Sequenzen randomisieren
  Input: Trades aus historischem Backtest
  Process: Shuffle 1000×, Equity-Curve pro Szenario
  Output: Worst-Case DD, Prob-Profit, Konfidenzintervalle

Methode 2: Resample — Bar Bootstrap
  Input: Historische OHLCV-Bars (Pool von letzten 100 Bars)
  Process: Sample zufällig 100 neue Bars, run backtest 100×
  Output: Win-Rate/Return/DD Konfidenzintervalle, Robustheit
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from ._types import BacktestMetrics, BacktestTrade, ExitReason

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ScenarioResult:
    """Resultat eines Monte Carlo Szenarios."""
    scenario_id: int
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    win_rate: float  # dieser Szenario
    trades_count: int
    equity_curve: list[float] = field(default_factory=list)


def monte_carlo_shuffle(
    trades: list[BacktestTrade],
    n_simulations: int = 1000,
    initial_capital: float = 10_000.0,
    risk_pct: float = 0.01,
    seed: int | None = None,
) -> dict:
    """
    Shuffle-Methode: Trades in zufälliger Reihenfolge kombinieren.

    Annahme: Trades sind unabhängig, P&L bleibt gleich, nur Sequenz ändert sich.
    Misst: Robustheit gegen Drawdown-Timing, Szenario-Variabilität.

    Args:
        trades: List aus Backtest
        n_simulations: Anzahl MC-Szenarien (default 1000)
        initial_capital: Startkapital
        risk_pct: Risk pro Trade (z.B. 0.01 = 1%)
        seed: Random seed für Reproduzierbarkeit

    Returns:
        {
            "method": "shuffle",
            "n_simulations": 1000,
            "total_trades": 55,
            "scenarios": [ScenarioResult, ...],
            "worst_case_drawdown_pct": -15.3,
            "prob_profit": 0.72,
            "ci_return": (5.0, 28.0),  # 5th, 95th percentile
            "ci_dd": (-2.0, -18.0),
            "statistics": {
                "avg_return": 15.3,
                "std_return": 7.2,
                "avg_dd": -8.5,
                "std_dd": 3.2,
            }
        }
    """
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)

    if not trades:
        log.warning("Monte Carlo Shuffle: no trades provided")
        return {
            "method": "shuffle",
            "n_simulations": n_simulations,
            "total_trades": 0,
            "scenarios": [],
            "worst_case_drawdown_pct": 0.0,
            "prob_profit": 0.0,
            "ci_return": (0.0, 0.0),
            "ci_dd": (0.0, 0.0),
            "statistics": {},
            "message": "No trades to simulate",
        }

    scenarios: list[ScenarioResult] = []

    for scenario_id in range(n_simulations):
        # Shuffle trades
        shuffled = trades.copy()
        random.shuffle(shuffled)

        # Equity curve
        equity = initial_capital
        equity_curve = [equity]
        wins = 0

        for trade in shuffled:
            # Risk-based sizing (consistent mit Backtest)
            risk_amount = equity * risk_pct
            # r_multiple bereits in trade (z.B. +2.0 = +2R)
            pnl = risk_amount * trade.r_multiple
            equity += pnl
            equity_curve.append(equity)

            if trade.r_multiple > 0:
                wins += 1

        # Berechne Drawdown
        eq_arr = np.array(equity_curve)
        if len(eq_arr) > 1:
            running_max = np.maximum.accumulate(eq_arr)
            dd = (eq_arr - running_max) / running_max
            max_dd = float(dd.min() * 100.0)
        else:
            max_dd = 0.0

        total_return_pct = (equity - initial_capital) / initial_capital * 100.0
        win_rate = wins / len(shuffled) if shuffled else 0.0

        scenarios.append(ScenarioResult(
            scenario_id=scenario_id,
            final_equity=equity,
            total_return_pct=total_return_pct,
            max_drawdown_pct=max_dd,
            win_rate=win_rate,
            trades_count=len(shuffled),
            equity_curve=equity_curve,
        ))

    # Statistik
    returns = np.array([s.total_return_pct for s in scenarios])
    drawdowns = np.array([s.max_drawdown_pct for s in scenarios])
    profitable = sum(1 for s in scenarios if s.total_return_pct > 0)

    worst_case_dd = float(np.percentile(drawdowns, 95))  # 95th = worst 5%
    prob_profit = profitable / n_simulations
    ci_return_lower = float(np.percentile(returns, 5))
    ci_return_upper = float(np.percentile(returns, 95))
    ci_dd_lower = float(np.percentile(drawdowns, 5))
    ci_dd_upper = float(np.percentile(drawdowns, 95))

    return {
        "method": "shuffle",
        "n_simulations": n_simulations,
        "total_trades": len(trades),
        "scenarios": scenarios,
        "worst_case_drawdown_pct": worst_case_dd,
        "prob_profit": prob_profit,
        "ci_return": (ci_return_lower, ci_return_upper),
        "ci_dd": (ci_dd_lower, ci_dd_upper),
        "statistics": {
            "avg_return": float(returns.mean()),
            "std_return": float(returns.std()),
            "avg_dd": float(drawdowns.mean()),
            "std_dd": float(drawdowns.std()),
        },
        "message": f"Monte Carlo Shuffle: {prob_profit:.0%} scenarios profitable, "
                   f"worst-case DD: {worst_case_dd:.1f}%, "
                   f"return CI: [{ci_return_lower:.1f}%, {ci_return_upper:.1f}%]",
    }


def monte_carlo_resample_bars(
    bars_dict: dict[str, pd.DataFrame],
    strategy_core_eval_fn: Callable,
    n_simulations: int = 100,
    iter_tf: str = "5m",
    initial_capital: float = 10_000.0,
    risk_pct: float = 0.01,
    lookback_bars: int = 100,
    seed: int | None = None,
) -> dict:
    """
    Resample-Methode: Bootstrap OHLCV-Bars aus historischem Pool.

    Annahme: Letzte `lookback_bars` Bars repräsentieren typische Marktbedingungen.
    Wir sampeln `lookback_bars` neue Bars aus diesem Pool (mit Zurücklegen).
    Dieser Pool wird dann durchlaufen: full Backtest pro Szenario.

    Teuer (O(N²)) — nur 100 Szenarien statt 1000.

    Args:
        bars_dict: {"1h": df_1h, "4h": df_4h, ...}
        strategy_core_eval_fn: evaluate() Funktion aus strategy_core
        n_simulations: Anzahl Szenarien (default 100)
        iter_tf: Iterativ-Timeframe (z.B. "5m")
        initial_capital: Startkapital
        risk_pct: Risk pro Trade
        lookback_bars: Pool-Größe für Resampling
        seed: Random seed

    Returns:
        {
            "method": "resample",
            "n_simulations": 100,
            "scenarios": [ScenarioResult, ...],
            "win_rate_ci": (0.52, 0.65),
            "return_ci": (8.0, 22.0),
            "dd_ci": (-6.0, -12.0),
            "avg_trades": 55,
            "statistics": {...},
            "message": "Strategie robust über alle Szenarien"
        }
    """
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)

    # Extrahiere Pool (letzte lookback_bars aus iter_tf)
    if iter_tf not in bars_dict or bars_dict[iter_tf].empty:
        log.warning(f"Monte Carlo Resample: iter_tf '{iter_tf}' not found or empty")
        return {
            "method": "resample",
            "n_simulations": n_simulations,
            "scenarios": [],
            "win_rate_ci": (0.0, 0.0),
            "return_ci": (0.0, 0.0),
            "dd_ci": (0.0, 0.0),
            "avg_trades": 0,
            "statistics": {},
            "message": "Insufficient data for resampling",
        }

    bars_pool = bars_dict[iter_tf].iloc[-lookback_bars:].copy()
    if len(bars_pool) < lookback_bars:
        log.warning(f"Resample pool has only {len(bars_pool)} bars, need {lookback_bars}")

    scenarios: list[ScenarioResult] = []

    for scenario_id in range(n_simulations):
        # Resample bars aus Pool
        indices = np.random.choice(len(bars_pool), size=len(bars_pool), replace=True)
        resampled_bars = bars_pool.iloc[indices].copy()

        # Wichtig: Index (Zeitstempel) müssen aufsteigend sein, um Backtest korrekt ablaufen
        # aber wir behalten die Bar-Struktur, nur Reihenfolge ändert sich
        # → Neuer Index (0, 1, 2, ...)
        resampled_bars = resampled_bars.reset_index(drop=True)

        # Erstelle bars_dict_resampled für Strategy-Core
        bars_dict_resampled = bars_dict.copy()
        bars_dict_resampled[iter_tf] = resampled_bars

        # Hilfsfunktion: Führe einen mini-backtest aus
        # (würde normalerweise `runner.py` aufrufen, vereinfacht hier)
        try:
            # Annahme: strategy_core_eval_fn gibt [Signal, ...] zurück
            # Dies ist ein Stub — Real implementation würde volle backtest ausführen
            result = _run_mini_backtest(
                bars_dict_resampled,
                strategy_core_eval_fn,
                iter_tf,
                initial_capital,
                risk_pct,
            )
            scenarios.append(result)
        except Exception as e:
            log.warning(f"Szenario {scenario_id} backtest failed: {e}")
            continue

    if not scenarios:
        return {
            "method": "resample",
            "n_simulations": n_simulations,
            "scenarios": [],
            "win_rate_ci": (0.0, 0.0),
            "return_ci": (0.0, 0.0),
            "dd_ci": (0.0, 0.0),
            "avg_trades": 0,
            "statistics": {},
            "message": "No successful scenarios",
        }

    # Statistik
    win_rates = np.array([s.win_rate for s in scenarios])
    returns = np.array([s.total_return_pct for s in scenarios])
    drawdowns = np.array([s.max_drawdown_pct for s in scenarios])
    trades_counts = np.array([s.trades_count for s in scenarios])

    win_rate_ci = (float(np.percentile(win_rates, 5)),
                   float(np.percentile(win_rates, 95)))
    return_ci = (float(np.percentile(returns, 5)),
                 float(np.percentile(returns, 95)))
    dd_ci = (float(np.percentile(drawdowns, 5)),
             float(np.percentile(drawdowns, 95)))

    return {
        "method": "resample",
        "n_simulations": len(scenarios),
        "scenarios": scenarios,
        "win_rate_ci": win_rate_ci,
        "return_ci": return_ci,
        "dd_ci": dd_ci,
        "avg_trades": float(trades_counts.mean()),
        "statistics": {
            "avg_win_rate": float(win_rates.mean()),
            "std_win_rate": float(win_rates.std()),
            "avg_return": float(returns.mean()),
            "std_return": float(returns.std()),
            "avg_dd": float(drawdowns.mean()),
            "std_dd": float(drawdowns.std()),
        },
        "message": f"Monte Carlo Resample: {len(scenarios)} scenarios, "
                   f"win-rate CI: [{win_rate_ci[0]:.0%}, {win_rate_ci[1]:.0%}], "
                   f"return CI: [{return_ci[0]:.1f}%, {return_ci[1]:.1f}%]",
    }


def _run_mini_backtest(
    bars_dict: dict[str, pd.DataFrame],
    strategy_core_eval_fn: Callable,
    iter_tf: str,
    initial_capital: float,
    risk_pct: float,
) -> ScenarioResult:
    """
    Hilfsfunktion: Run einen vereinfachten Backtest auf resampled Bars.

    Diese Funktion ist ein Stub; in echter Implementierung würde sie
    den vollen runner.py-Backtest-Loop aufrufen.

    Returns:
        ScenarioResult mit finalen Metriken
    """
    # Stub: Return empty scenario
    # Real: würde bar-by-bar loop machen, Signals evaluieren, Trades abwickeln
    return ScenarioResult(
        scenario_id=0,
        final_equity=initial_capital,
        total_return_pct=0.0,
        max_drawdown_pct=0.0,
        win_rate=0.0,
        trades_count=0,
        equity_curve=[initial_capital],
    )
