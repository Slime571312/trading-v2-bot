"""Bar-für-Bar-Replay des Strategy-Cores gegen historische OHLCV.

Spec-Quellen:
  - Trading/Bot/Playbook.md          (Setup-Logik)
  - Trading/Bot/Backtest-Methodik.md (Close-to-Close + Slippage, kein Look-ahead)

**Modell:**
- Iteriere über `iter_tf` (Default 5m) Bar-für-Bar.
- Pro Bar: prüfe ob offener Trade intrabar SL/TP getroffen hat (SL > TP bei
  Ambiguität, konservativ).
- Wenn kein offener Trade: rufe `evaluate()` mit historischem Slice — der
  Strategy-Core sieht nur Bars ≤ `ts`, kein Look-ahead.
- Bei Signal: Entry mit `apply_slippage_entry`, Position-Size aus
  `risk_pct_per_trade / sl_distance`.

**Risk-Modell** (gemäß Playbook + Risk.md):
- Fixes Risk-Pct pro Trade (Default 1 %).
- Size = (Equity × risk_pct) / SL-Distanz.
- Hitting SL = exakt −1R Equity-Verlust (per Konstruktion).
- 1R = `risk_amount = equity × risk_pct` bei Trade-Open.
"""
from __future__ import annotations

import logging
from typing import Literal

import pandas as pd

from src.strategy_core import evaluate

from ._types import BacktestResult, BacktestTrade, ExitReason
from .metrics import compute_metrics
from .slippage import apply_slippage_entry, apply_slippage_exit

log = logging.getLogger(__name__)


def _slice_view(
    bars_full: dict[str, pd.DataFrame],
    ts: pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    """Liefert pro TF nur Bars ≤ ts. Per searchsorted in O(log N)."""
    view: dict[str, pd.DataFrame] = {}
    for tf, df in bars_full.items():
        if df.empty:
            continue
        idx = df.index.searchsorted(ts, side="right")
        if idx > 0:
            view[tf] = df.iloc[:idx]
    return view


def _open_trade(
    ts: pd.Timestamp,
    bar_idx: int,
    current_bar: pd.Series,
    signal,  # Signal (strategy_core)
    equity: float,
    risk_pct: float,
) -> BacktestTrade | None:
    """Trade-Open mit Slippage + Risk-Sizing. None wenn SL ungültig."""
    entry = apply_slippage_entry(signal.entry, signal.side,
                                  float(current_bar.high), float(current_bar.low))
    sl_distance = abs(entry - signal.sl)
    if sl_distance <= 0:
        return None
    risk_amount = equity * risk_pct
    size = risk_amount / sl_distance
    return BacktestTrade(
        open_time=ts, close_time=ts,  # close_time wird beim Schließen überschrieben
        side=signal.side, entry=entry, exit=entry,
        sl=signal.sl, tp=signal.tp, size=size,
        variant=signal.variant, htf_used=signal.htf_used, ltf_used=signal.ltf_used,
        bars_held=0, pnl_abs=0.0, pnl_pct=0.0, r_multiple=0.0,
        exit_reason="end_of_data",
    )


def _check_intrabar_exit(
    trade: BacktestTrade,
    current_bar: pd.Series,
) -> tuple[float, ExitReason] | None:
    """Prüft SL/TP intrabar. SL hat Vorrang bei Ambiguität (konservativ).

    Returns (exit_price_raw, reason) oder None wenn weder SL noch TP getroffen.
    """
    high = float(current_bar.high)
    low = float(current_bar.low)
    if trade.side == "long":
        sl_hit = low <= trade.sl
        tp_hit = high >= trade.tp
        if sl_hit:
            return trade.sl, "sl"
        if tp_hit:
            return trade.tp, "tp"
    else:  # short
        sl_hit = high >= trade.sl
        tp_hit = low <= trade.tp
        if sl_hit:
            return trade.sl, "sl"
        if tp_hit:
            return trade.tp, "tp"
    return None


def _close_trade(
    trade: BacktestTrade,
    ts: pd.Timestamp,
    bar_idx_open: int,
    bar_idx_close: int,
    current_bar: pd.Series,
    exit_price_raw: float,
    reason: ExitReason,
) -> BacktestTrade:
    """Trade schließen — Slippage auf Exit, PnL + R-Multiple berechnen."""
    exit_slipped = apply_slippage_exit(exit_price_raw, trade.side,
                                        float(current_bar.high), float(current_bar.low))
    direction = 1 if trade.side == "long" else -1
    pnl_abs = (exit_slipped - trade.entry) * direction * trade.size
    pnl_pct = (exit_slipped - trade.entry) / trade.entry * 100.0 * direction
    # 1R = risk_amount = sl_distance × size
    risk_amount = abs(trade.entry - trade.sl) * trade.size
    r_multiple = pnl_abs / risk_amount if risk_amount > 0 else 0.0

    trade.close_time = ts
    trade.exit = exit_slipped
    trade.exit_reason = reason
    trade.bars_held = bar_idx_close - bar_idx_open
    trade.pnl_abs = pnl_abs
    trade.pnl_pct = pnl_pct
    trade.r_multiple = r_multiple
    return trade


async def run_backtest(
    instrument: str,
    bars_full: dict[str, pd.DataFrame],
    initial_capital: float = 10_000.0,
    rr_threshold: float = 2.0,
    sweep_lookback: int = 10,
    risk_pct_per_trade: float = 0.01,
    iter_tf: str = "5m",
    min_warmup_bars: int = 100,
) -> BacktestResult:
    """Führt den Bar-für-Bar-Replay aus.

    `bars_full` muss bereits alle benötigten TFs für den gewünschten Range
    enthalten — der Caller (`/backtest`-Endpoint oder Test) ist für das Fetchen
    zuständig. So bleibt `runner.py` rein algorithmisch und gut testbar.
    """
    if iter_tf not in bars_full or bars_full[iter_tf].empty:
        raise ValueError(f"iter_tf={iter_tf} fehlt oder leer in bars_full")

    iter_bars = bars_full[iter_tf]
    start = iter_bars.index[0]
    end = iter_bars.index[-1]
    equity = initial_capital
    equity_curve: list[tuple[pd.Timestamp, float]] = [(start, equity)]
    trades: list[BacktestTrade] = []
    open_trade: BacktestTrade | None = None
    open_bar_idx = -1

    log.info("Backtest start: %s %s %s → %s (%d bars)", instrument, iter_tf, start, end, len(iter_bars))

    for i, ts in enumerate(iter_bars.index):
        current_bar = iter_bars.iloc[i]

        # 1. Offene Trades — intrabar SL/TP?
        if open_trade is not None:
            exit_hit = _check_intrabar_exit(open_trade, current_bar)
            if exit_hit is not None:
                exit_raw, reason = exit_hit
                closed = _close_trade(
                    open_trade, ts, open_bar_idx, i, current_bar, exit_raw, reason,
                )
                equity += closed.pnl_abs
                trades.append(closed)
                open_trade = None

        # 2. Kein offener Trade + Warmup vorbei → Signal-Check
        if open_trade is None and i >= min_warmup_bars:
            view = _slice_view(bars_full, ts)
            try:
                signal = evaluate(
                    instrument, view,
                    rr_threshold=rr_threshold,
                    sweep_lookback_bars=sweep_lookback,
                )
            except Exception as e:
                log.warning("evaluate crash @ %s: %s", ts, e)
                signal = None

            if signal is not None:
                trade = _open_trade(ts, i, current_bar, signal, equity, risk_pct_per_trade)
                if trade is not None:
                    open_trade = trade
                    open_bar_idx = i

        # 3. Equity-Curve-Punkt
        mark_to_market = equity
        if open_trade is not None:
            direction = 1 if open_trade.side == "long" else -1
            unrealized = (float(current_bar.close) - open_trade.entry) * direction * open_trade.size
            mark_to_market = equity + unrealized
        equity_curve.append((ts, mark_to_market))

    # End: offenen Trade am letzten Bar schließen
    if open_trade is not None:
        last_bar = iter_bars.iloc[-1]
        closed = _close_trade(
            open_trade, end, open_bar_idx, len(iter_bars) - 1,
            last_bar, float(last_bar.close), "end_of_data",
        )
        equity += closed.pnl_abs
        trades.append(closed)
        equity_curve[-1] = (end, equity)

    metrics = compute_metrics(trades, equity_curve, initial_capital, iter_tf)
    log.info("Backtest done: %d trades, %.2f%% return, %.2f Sharpe",
             metrics.total_trades, metrics.total_return_pct, metrics.sharpe)

    return BacktestResult(
        instrument=instrument, iter_tf=iter_tf,
        start=start, end=end,
        initial_capital=initial_capital, final_equity=equity,
        metrics=metrics, trades=trades, equity_curve=equity_curve,
    )
