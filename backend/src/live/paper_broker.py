"""Paper-Broker — öffnet/schließt virtuelle Positionen, identisches Risk-Modell
wie der Backtest-Runner.

- Slippage via `backtest/slippage.py` (1 Source of Truth)
- Position-Size: `equity × risk_pct / sl_distance` → SL-Hit = exakt −1R
- Intrabar SL/TP-Check: pessimistisch (SL hat Vorrang bei Ambiguität)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd

from src.backtest.slippage import apply_slippage_entry, apply_slippage_exit

from .state import ClosedTrade, ExitReason, OpenTrade, _new_trade_id, _now

if TYPE_CHECKING:
    from src.strategy_core._types import Signal

log = logging.getLogger(__name__)


def open_position(
    signal: "Signal",
    equity: float,
    risk_pct: float,
    current_bar: pd.Series,
) -> OpenTrade | None:
    """Öffne eine Paper-Position aus einem Signal. None wenn SL-Distanz ungültig.

    Side-Effect-frei — gibt nur die Trade-Struktur zurück. Aufrufer ist für
    State-Mutation + Persist verantwortlich.
    """
    # Signal-Level-Sanity: degenerierter SL (=Entry) wird sofort verworfen,
    # noch vor dem Slippage-Modell (sonst kann Slippage künstlich eine SL-Distanz
    # erzeugen und einen unsinnig gesizten Trade rechtfertigen).
    if abs(signal.entry - signal.sl) <= 0:
        log.warning("open_position: degeneriertes Signal (entry==sl) für %s — skip",
                    signal.instrument)
        return None

    entry = apply_slippage_entry(
        signal.entry, signal.side,
        float(current_bar.high), float(current_bar.low),
    )
    sl_distance = abs(entry - signal.sl)
    if sl_distance <= 0:
        log.warning("open_position: SL-Distanz 0 für %s — skip", signal.instrument)
        return None

    risk_amount = equity * risk_pct
    size = risk_amount / sl_distance

    return OpenTrade(
        id=_new_trade_id(),
        instrument=signal.instrument,
        side=signal.side,
        open_time=_now(),
        entry=entry,
        sl=signal.sl,
        tp=signal.tp,
        size=size,
        variant=signal.variant or "primary",
        htf_used=signal.htf_used,
        ltf_used=signal.ltf_used,
        rr_at_open=signal.rr,
    )


def check_intrabar_exit(
    trade: OpenTrade,
    current_bar: pd.Series,
) -> tuple[float, ExitReason] | None:
    """Returns (exit_price_raw, reason) oder None falls weder SL noch TP getroffen.

    Konservativ: bei Ambiguität (Bar streift SL UND TP) gewinnt SL.
    """
    high = float(current_bar.high)
    low = float(current_bar.low)

    if trade.side == "long":
        if low <= trade.sl:
            return trade.sl, "sl"
        if high >= trade.tp:
            return trade.tp, "tp"
    else:  # short
        if high >= trade.sl:
            return trade.sl, "sl"
        if low <= trade.tp:
            return trade.tp, "tp"
    return None


def close_position(
    trade: OpenTrade,
    exit_price_raw: float,
    current_bar: pd.Series,
    reason: ExitReason,
) -> ClosedTrade:
    """Closed-Trade aus Open-Trade + Exit-Bar. PnL + R-Multiple berechnen."""
    exit_slipped = apply_slippage_exit(
        exit_price_raw, trade.side,
        float(current_bar.high), float(current_bar.low),
    )
    direction = 1 if trade.side == "long" else -1
    pnl_abs = (exit_slipped - trade.entry) * direction * trade.size
    pnl_pct = (exit_slipped - trade.entry) / trade.entry * 100.0 * direction

    risk_amount = abs(trade.entry - trade.sl) * trade.size
    r_multiple = pnl_abs / risk_amount if risk_amount > 0 else 0.0

    return ClosedTrade(
        id=trade.id, instrument=trade.instrument, side=trade.side,
        open_time=trade.open_time, close_time=_now(),
        entry=trade.entry, exit=exit_slipped,
        sl=trade.sl, tp=trade.tp, size=trade.size,
        variant=trade.variant, htf_used=trade.htf_used, ltf_used=trade.ltf_used,
        rr_at_open=trade.rr_at_open,
        pnl_abs=pnl_abs, pnl_pct=pnl_pct, r_multiple=r_multiple,
        exit_reason=reason,
    )


def force_close(
    trade: OpenTrade,
    last_price: float,
    reason: ExitReason = "manual",
) -> ClosedTrade:
    """Synthetischer Exit zum letzten bekannten Preis (z.B. Bot-Stop)."""
    direction = 1 if trade.side == "long" else -1
    pnl_abs = (last_price - trade.entry) * direction * trade.size
    pnl_pct = (last_price - trade.entry) / trade.entry * 100.0 * direction
    risk_amount = abs(trade.entry - trade.sl) * trade.size
    r_multiple = pnl_abs / risk_amount if risk_amount > 0 else 0.0
    return ClosedTrade(
        id=trade.id, instrument=trade.instrument, side=trade.side,
        open_time=trade.open_time, close_time=_now(),
        entry=trade.entry, exit=last_price,
        sl=trade.sl, tp=trade.tp, size=trade.size,
        variant=trade.variant, htf_used=trade.htf_used, ltf_used=trade.ltf_used,
        rr_at_open=trade.rr_at_open,
        pnl_abs=pnl_abs, pnl_pct=pnl_pct, r_multiple=r_multiple,
        exit_reason=reason,
    )
