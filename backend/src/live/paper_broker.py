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

    # TP1 = halbe Distanz zwischen Entry und TP (Bot/Partial-Close.md)
    # Bei Long: tp1 < tp.  Bei Short: tp1 > tp.
    tp1 = entry + 0.5 * (signal.tp - entry)

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
        tp1=tp1,
        tp1_hit=False,
        original_size=size,
        original_sl=signal.sl,
    )


def _hit_tp1(trade: OpenTrade, bar: pd.Series) -> bool:
    """True wenn diese Bar TP1 erreicht hat."""
    if trade.tp1 is None or trade.tp1_hit:
        return False
    if trade.side == "long":
        return float(bar.high) >= trade.tp1
    return float(bar.low) <= trade.tp1


def apply_tp1_partial(trade: OpenTrade, bar: pd.Series, ts) -> float:
    """Realisiert 50% bei TP1 + zieht SL auf Entry (BE-Move).

    Mutiert trade in-place. Gibt realisierten PnL der Teilschließung zurück.
    """
    if trade.tp1 is None or trade.tp1_hit:
        return 0.0

    # Mit Slippage: Long-Exit slipped DOWN, Short-Exit slipped UP
    exit_slipped = apply_slippage_exit(
        trade.tp1, trade.side, float(bar.high), float(bar.low),
    )
    closed_size = trade.size * 0.5
    direction = 1 if trade.side == "long" else -1
    partial_pnl = (exit_slipped - trade.entry) * direction * closed_size

    # State-Mutation: halbe Position weg, SL auf Entry (BE)
    trade.size -= closed_size
    trade.tp1_hit = True
    trade.partial_pnl += partial_pnl
    trade.sl = trade.entry   # Break-Even
    import pandas as _pd
    if isinstance(ts, _pd.Timestamp):
        from datetime import timezone as _tz
        trade.partial_close_time = (
            ts.to_pydatetime().replace(tzinfo=_tz.utc)
            if ts.tzinfo is None else ts.to_pydatetime()
        )
    else:
        trade.partial_close_time = ts
    return partial_pnl


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


def scan_exit_over_bars(
    trade: OpenTrade,
    bars: pd.DataFrame,
) -> tuple[pd.Series, float, ExitReason] | None:
    """Scannt eine Reihe von Bars chronologisch auf erstes SL/TP-Hit (ohne TP1-Logik).

    Für Trades die NICHT die Partial-Close-Mechanik nutzen (Legacy / Tests).
    Gibt (hit_bar, exit_price_raw, reason, bar_timestamp) zurück, oder None.
    """
    bars_after = bars[bars.index > trade.open_time]
    for ts, bar in bars_after.iterrows():
        result = check_intrabar_exit(trade, bar)
        if result is not None:
            exit_price, reason = result
            return bar, exit_price, reason, ts
    return None


def scan_with_partial_close(
    trade: OpenTrade,
    bars: pd.DataFrame,
) -> list[dict]:
    """Voller Scan inkl. TP1-Teilschließung + BE-Move (Bot/Partial-Close.md).

    Returns Liste von Events in chronologischer Reihenfolge:
      - {kind: 'partial_tp1', bar, ts, pnl}        — TP1 erreicht, halbe Position geschlossen
      - {kind: 'full_close',  bar, ts, exit, reason} — SL oder TP für Rest-Position

    Mutiert `trade` in-place für TP1 (Size halbiert, SL→Entry, tp1_hit=True).
    Caller MUSS bei 'full_close' die Position dann komplett schließen.
    """
    events: list[dict] = []
    # Bars ab Trade-Öffnung — wenn TP1 schon vorher gehittet wurde, ab partial_close_time
    cutoff = trade.partial_close_time or trade.open_time
    bars_after = bars[bars.index > cutoff]

    for ts, bar in bars_after.iterrows():
        # Schritt 1: TP1-Check (nur einmal pro Trade)
        if not trade.tp1_hit and trade.tp1 is not None and _hit_tp1(trade, bar):
            # Wichtig: zuerst SL-Check innerhalb derselben Bar — wenn die Bar auch SL
            # gerissen hat, gewinnt SL (konservativ). _hit_tp1 ist boolesche Check.
            sl_hit_in_same_bar = check_intrabar_exit(trade, bar)
            if sl_hit_in_same_bar is not None and sl_hit_in_same_bar[1] == "sl":
                exit_price, reason = sl_hit_in_same_bar
                events.append({"kind": "full_close", "bar": bar, "ts": ts,
                               "exit": exit_price, "reason": reason})
                return events
            # TP1 sauber erreicht ohne gleichzeitigen SL → partial close
            partial_pnl = apply_tp1_partial(trade, bar, ts)
            events.append({"kind": "partial_tp1", "bar": bar, "ts": ts, "pnl": partial_pnl})
            # Nach BE-Move (SL = Entry): prüfen ob dieselbe Bar bereits BE gerissen hat
            # → das wäre dann sofortiger BE-Exit auf den Rest
            be_hit = check_intrabar_exit(trade, bar)
            if be_hit is not None:
                exit_price, reason = be_hit
                events.append({"kind": "full_close", "bar": bar, "ts": ts,
                               "exit": exit_price, "reason": reason})
                return events
            continue

        # Schritt 2: SL oder TP voll
        result = check_intrabar_exit(trade, bar)
        if result is not None:
            exit_price, reason = result
            events.append({"kind": "full_close", "bar": bar, "ts": ts,
                           "exit": exit_price, "reason": reason})
            return events

    return events


def close_position(
    trade: OpenTrade,
    exit_price_raw: float,
    current_bar: pd.Series,
    reason: ExitReason,
    close_time=None,
) -> ClosedTrade:
    """Closed-Trade aus Open-Trade + Exit-Bar. PnL + R-Multiple berechnen.

    `close_time`: optionaler expliziter Schließzeitpunkt (für Catch-up nach
    Offline-Phase). Wenn None, wird _now() verwendet.
    """
    from datetime import datetime
    if close_time is None:
        close_time = _now()
    elif isinstance(close_time, pd.Timestamp):
        from datetime import timezone
        close_time = close_time.to_pydatetime().replace(tzinfo=timezone.utc) if close_time.tzinfo is None else close_time.to_pydatetime()

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
        open_time=trade.open_time, close_time=close_time,
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
