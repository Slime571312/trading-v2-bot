"""Param-Grid-Search — testet alle Kombinationen aus rr_threshold × sweep_lookback,
ranked nach expectancy_r mit profit_factor als Tie-Breaker.

Bewusst KEIN volles WFO hier — der Grid blast sonst auf 4 × 12 × N-windows = 100+
Backtests, was nächtlich >1h dauert. Stattdessen: pro Combo ein einziger Backtest
über die letzten 1000 Bars, Walk-Forward als optionaler zweiter Pass auf der
besten Combo (in runner.py).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, asdict
from typing import Sequence

import pandas as pd

from src.backtest import BacktestResult, run_backtest
from src.brokers.capital_com import CapitalAPIError, CapitalAuthError
from src.data.fetcher import load_candles

log = logging.getLogger(__name__)

# Default-Grid — konservativ klein, damit nightly-Run unter 20 min bleibt
DEFAULT_RR_THRESHOLDS = [1.5, 2.0, 2.5, 3.0]
DEFAULT_SWEEP_LOOKBACKS = [5, 10, 20]
# 4 × 3 = 12 Combos pro Instrument


@dataclass(slots=True)
class GridCombo:
    """Ein Kandidat im Grid + sein Backtest-Ergebnis."""

    rr_threshold: float
    sweep_lookback: int
    total_trades: int
    win_rate: float
    expectancy_r: float
    profit_factor: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe: float
    score: float  # zusammengesetzt: expectancy_r mit MinTrades-Filter

    def diff_dict(self) -> dict:
        return {"rr_threshold": self.rr_threshold, "sweep_lookback": self.sweep_lookback}


@dataclass(slots=True)
class GridResult:
    """Ergebnis einer Grid-Search für EIN Instrument."""

    instrument: str
    iter_tf: str
    bars_used: int
    combos: list[GridCombo] = field(default_factory=list)
    # Aktuelle Live-Params zum Vergleich
    current_rr_threshold: float = 0.0
    current_sweep_lookback: int = 0
    current_score: float | None = None

    @property
    def best(self) -> GridCombo | None:
        return self.combos[0] if self.combos else None

    @property
    def improvement_vs_current(self) -> float | None:
        """Verbesserung (expectancy_r-Delta) des besten Combos gegenüber aktuellem Setup.

        Nutzt expectancy_r direkt (NICHT score), damit Sentinel-Werte (-1e9 für
        statistisch unsignifikante Combos) den Delta nicht in den Müll ziehen.
        Wenn current_combo nicht gemessen werden konnte: None.
        """
        if not self.best or self.current_score is None:
            return None
        # Sentinel-Check: wenn current_score das Min-Trades-Sentinel ist, hat die
        # aktuelle Combo nicht mal genug Trades — Delta ist "aussagekräftig groß"
        # aber wir flaggen es separat damit das UI das anzeigen kann.
        if self.current_score <= -1e8:
            # Returne nur die positive Expectancy als Δ (kein Müll mehr)
            return self.best.expectancy_r
        # Normaler Fall: Score-basierter Delta (Score = expectancy + log(pf)-bonus)
        return self.best.score - self.current_score

    @property
    def current_too_few_trades(self) -> bool:
        """True wenn die aktuelle Live-Combo unter dem Min-Trades-Filter lag."""
        return self.current_score is not None and self.current_score <= -1e8

    def to_json(self) -> dict:
        return {
            "instrument": self.instrument,
            "iter_tf": self.iter_tf,
            "bars_used": self.bars_used,
            "current_rr_threshold": self.current_rr_threshold,
            "current_sweep_lookback": self.current_sweep_lookback,
            "current_score": self.current_score,
            "current_too_few_trades": self.current_too_few_trades,
            "improvement_vs_current": self.improvement_vs_current,
            "best": asdict(self.best) if self.best else None,
            "combos": [asdict(c) for c in self.combos],
        }


def _score(metrics_total_trades: int, expectancy_r: float, profit_factor: float) -> float:
    """Zusammengesetzter Score. Bestraft Combos mit zu wenig Samples.

    Min-Trades-Filter: <5 Trades → score = -inf (statistisch unsignifikant)
    Sonst: expectancy_r + 0.05 × log(profit_factor) als Tie-Breaker
    """
    if metrics_total_trades < 5:
        return -1e9
    import math
    pf_bonus = 0.05 * math.log(max(profit_factor, 0.01))
    return expectancy_r + pf_bonus


async def _fetch_bars(instrument: str, iter_tf: str, bars: int) -> dict[str, pd.DataFrame] | None:
    """Lädt alle TFs einmalig — Grid-Search nutzt denselben Datensatz für alle Combos."""
    tfs_needed = list(dict.fromkeys(["1d", "1h", "30m", "15m", "5m", "1m", iter_tf]))
    out: dict[str, pd.DataFrame] = {}
    for tf in tfs_needed:
        try:
            df = await load_candles(instrument, tf, bars=bars)
            if not df.empty:
                out[tf] = df
        except (CapitalAuthError, CapitalAPIError) as e:
            log.warning("Grid-Search: fetch %s/%s scheitert (%s) — überspringen", instrument, tf, e)
    if "1d" not in out or iter_tf not in out:
        return None
    return out


async def _run_single(
    instrument: str,
    bars: dict[str, pd.DataFrame],
    rr_threshold: float,
    sweep_lookback: int,
    iter_tf: str,
    risk_pct: float,
    initial_capital: float,
) -> GridCombo:
    """Ein Backtest pro Param-Combo."""
    result = await run_backtest(
        instrument=instrument,
        bars_full=bars,
        initial_capital=initial_capital,
        rr_threshold=rr_threshold,
        sweep_lookback=sweep_lookback,
        risk_pct_per_trade=risk_pct,
        iter_tf=iter_tf,
    )
    m = result.metrics
    pf = m.profit_factor if m.profit_factor != float("inf") else 999.0
    return GridCombo(
        rr_threshold=rr_threshold,
        sweep_lookback=sweep_lookback,
        total_trades=m.total_trades,
        win_rate=round(m.win_rate, 4),
        expectancy_r=round(m.expectancy_r, 4),
        profit_factor=round(pf, 3),
        total_return_pct=round(m.total_return_pct, 3),
        max_drawdown_pct=round(m.max_drawdown_pct, 3),
        sharpe=round(m.sharpe, 3),
        score=round(_score(m.total_trades, m.expectancy_r, pf), 4),
    )


async def run_grid_search(
    instrument: str,
    *,
    iter_tf: str = "5m",
    bars: int = 1000,
    rr_thresholds: Sequence[float] = DEFAULT_RR_THRESHOLDS,
    sweep_lookbacks: Sequence[int] = DEFAULT_SWEEP_LOOKBACKS,
    current_rr_threshold: float = 2.0,
    current_sweep_lookback: int = 10,
    risk_pct: float = 0.01,
    initial_capital: float = 10_000.0,
) -> GridResult | None:
    """Führt die Grid-Search durch. None wenn Daten nicht ladbar.

    Combos werden SEQUENZIELL gefahren — parallel wäre theoretisch schneller,
    aber Capital-Rate-Limit (10 req/s) macht's egal weil alle TFs gecacht sind
    nach dem ersten Fetch.
    """
    log.info("Grid-Search %s: %d Combos (rr=%s × sl=%s)",
             instrument, len(rr_thresholds) * len(sweep_lookbacks),
             list(rr_thresholds), list(sweep_lookbacks))

    bars_data = await _fetch_bars(instrument, iter_tf, bars)
    if bars_data is None:
        log.warning("Grid-Search %s: keine Bars ladbar", instrument)
        return None

    combos: list[GridCombo] = []
    for rr in rr_thresholds:
        for sl in sweep_lookbacks:
            try:
                combo = await _run_single(
                    instrument, bars_data, rr, sl, iter_tf, risk_pct, initial_capital,
                )
                combos.append(combo)
                log.debug("  rr=%.1f sl=%d → %d trades, %.3fR expectancy, score=%.4f",
                          rr, sl, combo.total_trades, combo.expectancy_r, combo.score)
            except Exception as e:
                log.warning("  rr=%.1f sl=%d gecrasht: %s", rr, sl, e)

    # Aktuelle Live-Combo separat ausrechnen (für faire Vergleichs-Basis)
    current_score: float | None = None
    cur_in_grid = next(
        (c for c in combos
         if c.rr_threshold == current_rr_threshold and c.sweep_lookback == current_sweep_lookback),
        None,
    )
    if cur_in_grid is not None:
        current_score = cur_in_grid.score
    else:
        try:
            cur = await _run_single(
                instrument, bars_data, current_rr_threshold, current_sweep_lookback,
                iter_tf, risk_pct, initial_capital,
            )
            current_score = cur.score
        except Exception as e:
            log.warning("Grid-Search %s: current-combo crashed: %s", instrument, e)

    # Ranking: score absteigend
    combos.sort(key=lambda c: c.score, reverse=True)

    return GridResult(
        instrument=instrument,
        iter_tf=iter_tf,
        bars_used=bars,
        combos=combos,
        current_rr_threshold=current_rr_threshold,
        current_sweep_lookback=current_sweep_lookback,
        current_score=current_score,
    )
