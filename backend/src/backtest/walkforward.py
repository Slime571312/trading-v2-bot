"""Walk-Forward-Skeleton — OOS-Robustness-Check ohne In-Sample-Tuning (Etappe 3).

Etappe 7 ergänzt das In-Sample-Hyperopt. Jetzt: feste Params, rollende
OOS-Fenster, aggregierte Metriken — zeigt ob die Strategie stabil ist oder
nur auf einem Datensegment funktioniert.

Spec: Trading/Bot/Backtest-Methodik.md (WFO-Abschnitt)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from .runner import run_backtest
from ._types import BacktestMetrics, BacktestResult

log = logging.getLogger(__name__)


@dataclass(slots=True)
class WFWindow:
    window_idx: int
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp
    result: BacktestResult


@dataclass(slots=True)
class WalkForwardResult:
    instrument: str
    iter_tf: str
    n_windows: int
    windows: list[WFWindow] = field(default_factory=list)
    # Aggregiert über alle OOS-Fenster
    total_trades: int = 0
    wins: int = 0
    win_rate: float = 0.0
    total_return_pct: float = 0.0
    avg_expectancy_r: float = 0.0
    avg_profit_factor: float | None = None
    avg_max_drawdown_pct: float = 0.0
    avg_sharpe: float | None = None
    # Robustness: Anteil Fenster mit Expectancy > 0
    pct_windows_positive: float = 0.0


async def run_walk_forward(
    instrument: str,
    bars_full: dict[str, pd.DataFrame],
    iter_tf: str = "5m",
    oos_bars: int = 200,
    in_sample_bars: int = 500,
    initial_capital: float = 10_000.0,
    rr_threshold: float = 2.0,
    sweep_lookback: int = 10,
    risk_pct_per_trade: float = 0.01,
    min_warmup_bars: int = 100,
) -> WalkForwardResult:
    """Rollendes OOS-Test-Fenster über alle verfügbaren iter_tf-Bars.

    Fenster-Logik:
    - Jedes Fenster: [window_start … oos_start) = in_sample, [oos_start … oos_end] = OOS
    - Slide forward by oos_bars
    - Backtest läuft auf [window_start … oos_end] mit Strategy-Core (kein Tuning)
    - Etappe 7 wird in_sample für Hyperopt nutzen; hier ist es nur ein Kontext-Puffer

    Mindest-Anforderung: in_sample_bars + oos_bars + min_warmup_bars ≤ len(iter_tf-Bars)
    """
    if iter_tf not in bars_full or bars_full[iter_tf].empty:
        raise ValueError(f"iter_tf={iter_tf} fehlt in bars_full")

    iter_bars = bars_full[iter_tf]
    n = len(iter_bars)
    window_size = in_sample_bars + oos_bars

    if n < window_size + min_warmup_bars:
        raise ValueError(
            f"Zu wenige {iter_tf}-Bars für WFO: {n} < {window_size + min_warmup_bars} nötig "
            f"(in_sample={in_sample_bars} + oos={oos_bars} + warmup={min_warmup_bars})"
        )

    windows: list[WFWindow] = []
    window_idx = 0
    start_pos = 0

    while start_pos + window_size <= n:
        oos_start_pos = start_pos + in_sample_bars
        oos_end_pos = start_pos + window_size

        oos_start_ts = iter_bars.index[oos_start_pos]
        oos_end_ts = iter_bars.index[oos_end_pos - 1]

        # OOS-Slice aus allen TFs herausschneiden
        oos_bars_slice: dict[str, pd.DataFrame] = {}
        for tf, df in bars_full.items():
            if df.empty:
                continue
            # Komplettes Fenster von Anfang bis OOS-Ende (damit Warmup greift)
            end_mask = df.index <= oos_end_ts
            sliced = df[end_mask]
            # Nur so viele Bars wie in diesem Fenster verfügbar, mind. warmup
            if not sliced.empty:
                oos_bars_slice[tf] = sliced

        if iter_tf not in oos_bars_slice or len(oos_bars_slice[iter_tf]) < min_warmup_bars:
            start_pos += oos_bars
            continue

        log.info("WFO window %d: OOS %s → %s", window_idx, oos_start_ts.date(), oos_end_ts.date())

        result = await run_backtest(
            instrument=instrument,
            bars_full=oos_bars_slice,
            initial_capital=initial_capital,
            rr_threshold=rr_threshold,
            sweep_lookback=sweep_lookback,
            risk_pct_per_trade=risk_pct_per_trade,
            iter_tf=iter_tf,
            min_warmup_bars=min_warmup_bars,
        )

        windows.append(WFWindow(
            window_idx=window_idx,
            oos_start=oos_start_ts,
            oos_end=oos_end_ts,
            result=result,
        ))
        window_idx += 1
        start_pos += oos_bars

    if not windows:
        return WalkForwardResult(
            instrument=instrument, iter_tf=iter_tf, n_windows=0
        )

    # Aggregation
    total_trades = sum(w.result.metrics.total_trades for w in windows)
    wins = sum(w.result.metrics.wins for w in windows)
    win_rate = wins / total_trades if total_trades > 0 else 0.0

    n_w = len(windows)
    avg_expectancy_r = sum(w.result.metrics.expectancy_r for w in windows) / n_w
    avg_max_drawdown_pct = sum(w.result.metrics.max_drawdown_pct for w in windows) / n_w
    # profit_factor / sharpe können None sein (keine Losses / std=0) — None-Werte rausfiltern
    pf_vals = [w.result.metrics.profit_factor for w in windows
               if w.result.metrics.profit_factor is not None]
    avg_profit_factor: float | None = sum(pf_vals) / len(pf_vals) if pf_vals else None
    sh_vals = [w.result.metrics.sharpe for w in windows
               if w.result.metrics.sharpe is not None]
    avg_sharpe: float | None = sum(sh_vals) / len(sh_vals) if sh_vals else None

    final_equities = [w.result.final_equity for w in windows]
    total_return_pct = (
        (final_equities[-1] - initial_capital) / initial_capital * 100.0
        if final_equities else 0.0
    )
    pct_windows_positive = sum(
        1 for w in windows if w.result.metrics.expectancy_r > 0
    ) / n_w

    log.info(
        "WFO done: %d windows, %d trades, %.1f%% win-rate, %.2fR avg-expectancy, %.1f%% windows positive",
        n_w, total_trades, win_rate * 100, avg_expectancy_r, pct_windows_positive * 100,
    )

    return WalkForwardResult(
        instrument=instrument,
        iter_tf=iter_tf,
        n_windows=n_w,
        windows=windows,
        total_trades=total_trades,
        wins=wins,
        win_rate=win_rate,
        total_return_pct=total_return_pct,
        avg_expectancy_r=avg_expectancy_r,
        avg_profit_factor=avg_profit_factor,
        avg_max_drawdown_pct=avg_max_drawdown_pct,
        avg_sharpe=avg_sharpe,
        pct_windows_positive=pct_windows_positive,
    )
