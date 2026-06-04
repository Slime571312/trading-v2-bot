"""GitHub Actions standalone tick runner.

Läuft einmal pro Trigger (alle 5 Min per Cron), macht für jedes Instrument
einen vollen Tick (SL/TP-Check + Signal-Eval + Entry), speichert State zurück
in live_state.json. Danach committed und pushed der Workflow die Datei.

Kein FastAPI, kein WebSocket, keine asyncio.Lock-Komplexität —
nur die reine Trade-Logik aus loop.py, vereinfacht für Einzel-Runs.
"""
from __future__ import annotations

import asyncio
import logging
import sys

import pandas as pd

from src.brokers.capital_com import CapitalAPIError, CapitalAuthError
from src.data.fetcher import load_candles
from src.live import paper_broker
from src.live.state import (
    BotInstance, BotState, TickLogEntry,
    load_state, save_state, _now,
)
from src.live.trade_limits import TradeLimitsConfig, can_trade
from src.strategy_core import evaluate as evaluate_signal
from src.strategy_core.bias import compute_bias

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("gh_runner")

INSTRUMENTS = ["DE40", "NASDAQ", "SP500", "BTC"]
LIVE_TFS = ["1d", "1h", "30m", "15m", "5m", "1m"]


async def _tick(instance: BotInstance) -> None:
    """Vollständiger Tick für ein Instrument: SL/TP-Check + Signal-Eval."""
    inst = instance.instrument

    # 1. 1m-Bars laden — genug um seit Trade-Öffnung alles abzudecken (Catch-up)
    df_1m = pd.DataFrame()
    latest_bar = None
    try:
        # Wie viele 1m-Bars seit dem ältesten offenen Trade?
        bars_needed = 500  # ~8h Default
        if instance.open_trades:
            from datetime import timezone
            import math
            oldest = min(t.open_time for t in instance.open_trades)
            # Sekunden seit Trade-Öffnung + 10% Puffer, in Bars (1 Bar = 1 Min)
            now_utc = _now()
            age_s = (now_utc - oldest).total_seconds()
            bars_needed = min(1000, max(50, math.ceil(age_s / 60) + 20))
        df_1m = await load_candles(inst, "1m", bars=bars_needed, refresh=True)
        if not df_1m.empty:
            latest_bar = df_1m.iloc[-1]
    except (CapitalAuthError, CapitalAPIError) as e:
        log.warning("%s: 1m fetch fehlgeschlagen: %s", inst, e)

    # 2. Offene Trades: Catch-up Scan mit Partial-Close-Logik (Bot/Partial-Close.md)
    if not df_1m.empty:
        still_open = []
        for trade in instance.open_trades:
            events = paper_broker.scan_with_partial_close(trade, df_1m)
            full_closed = False
            for ev in events:
                if ev["kind"] == "partial_tp1":
                    instance.equity += ev["pnl"]
                    instance.append_tick_log(TickLogEntry(
                        timestamp=trade.partial_close_time or _now(),
                        action="close", decision="partial_tp1",
                        variant=trade.variant,
                        detail=(f"50% @ {trade.tp1:.2f} → +{ev['pnl']:.2f}, "
                                f"SL→BE ({trade.entry:.2f})"),
                        related_trade_id=trade.id,
                    ))
                    log.info("%s PARTIAL_TP1 %s 50%% @ %.2f → +%.2f, SL→BE",
                             inst, trade.side, trade.tp1, ev["pnl"])
                elif ev["kind"] == "full_close":
                    closed = paper_broker.close_position(
                        trade, ev["exit"], ev["bar"], ev["reason"], close_time=ev["ts"],
                    )
                    instance.equity += closed.pnl_abs
                    instance.closed_trades.append(closed)
                    # Im Anzeige-PnL die TP1-Teilrealisierung mit aufaddieren
                    total_pnl = closed.pnl_abs + trade.partial_pnl
                    risk_basis = abs(trade.original_sl - trade.entry) * (trade.original_size or trade.size)
                    total_r = total_pnl / risk_basis if risk_basis > 0 else 0.0
                    instance.append_tick_log(TickLogEntry(
                        timestamp=closed.close_time, action="close",
                        decision=f"closed_{ev['reason']}",
                        variant=closed.variant,
                        detail=(f"{closed.side} rest @ {closed.exit:.2f} → "
                                f"+{closed.pnl_abs:+.2f}  | total {total_pnl:+.2f} "
                                f"({total_r:+.2f}R)"),
                        related_trade_id=closed.id,
                    ))
                    log.info("%s CLOSED %s %s → rest %+.2f, total %+.2f (%+.2fR)",
                             inst, closed.side, closed.exit_reason,
                             closed.pnl_abs, total_pnl, total_r)
                    full_closed = True
            if not full_closed:
                still_open.append(trade)
        instance.open_trades = still_open

    # 3. Signal-Eval — Gate 0: Trade-Limits (Bot/TradeGate.md, Bot/Trade-Limits.md)
    if instance.open_trades:
        instance.append_tick_log(TickLogEntry(
            timestamp=_now(), action="skip", decision="already_in_position",
            detail=f"open: {instance.open_trades[0].id}",
        ))
        return

    # Volle Trade-Limits-Pipeline: Daily/Weekly Loss, Consecutive-SL, Revenge-Cooldown,
    # Max-Trades-per-Day/Session/Pair
    cfg = TradeLimitsConfig(initial_capital=instance.initial_capital)
    allowed, reason = can_trade(_now(), inst, instance.equity,
                                 instance.closed_trades, cfg=cfg)
    if not allowed:
        instance.append_tick_log(TickLogEntry(
            timestamp=_now(), action="skip", decision="trade_limit_blocked",
            detail=reason or "unknown",
        ))
        log.info("%s Trade-Limit: %s", inst, reason)
        return

    try:
        bars: dict[str, pd.DataFrame] = {}
        for tf in LIVE_TFS:
            try:
                df = await load_candles(inst, tf, bars=200)
                if not df.empty:
                    bars[tf] = df
            except (CapitalAuthError, CapitalAPIError):
                pass

        if "1d" not in bars or "5m" not in bars:
            instance.append_tick_log(TickLogEntry(
                timestamp=_now(), action="error",
                decision="missing_required_tf",
                detail=f"have: {sorted(bars.keys())}",
            ))
            return

        bias = compute_bias(bars["1d"])
        signal = evaluate_signal(
            inst, bars,
            rr_threshold=instance.rr_threshold,
            sweep_lookback_bars=instance.sweep_lookback,
        )

        if signal is None:
            instance.append_tick_log(TickLogEntry(
                timestamp=_now(), action="eval", decision="no_setup",
                bias=bias.direction,
                detail=(
                    "neutraler Daily-Bias" if bias.direction == "neutral"
                    else "kein HTF-Sweep + LTF-BOS im Lookback"
                ),
            ))
            return

        current_bar = latest_bar if latest_bar is not None else bars["5m"].iloc[-1]

        # Combined Size-Multiplikator: OPEX × Conviction-Grade (A=1.0, B=0.5)
        from src.strategy_core import sessions as sessions_mod
        _opex_mult = sessions_mod.opex_size_multiplier(_now().date())
        _conv_mult = getattr(signal, "size_multiplier", 1.0)
        _effective_risk = instance.risk_pct * _opex_mult * _conv_mult

        new_trade = paper_broker.open_position(
            signal, instance.equity, _effective_risk, current_bar,
        )
        if new_trade is not None and (_opex_mult < 1.0 or _conv_mult < 1.0):
            log.info("%s Size-Anpassung: risk %.2f%% × OPEX %.2f × Conv %.2f → %.3f%% (grade=%s)",
                     inst, instance.risk_pct * 100, _opex_mult, _conv_mult,
                     _effective_risk * 100, getattr(signal, "conviction_grade", "?"))
        if new_trade is None:
            instance.append_tick_log(TickLogEntry(
                timestamp=_now(), action="skip",
                decision="degenerate_signal_sl_eq_entry",
                bias=bias.direction, htf_used=signal.htf_used,
                ltf_used=signal.ltf_used, rr_computed=signal.rr,
                variant=signal.variant,
            ))
            return

        instance.open_trades.append(new_trade)
        instance.append_tick_log(TickLogEntry(
            timestamp=_now(), action="open",
            decision=f"opened_{signal.variant}",
            bias=bias.direction,
            htf_used=signal.htf_used,
            ltf_used=signal.ltf_used,
            sweep_time=signal.sweep.time.isoformat(),
            sweep_direction=signal.sweep.direction,
            bos_time=signal.structure_break.time.isoformat(),
            rr_computed=round(signal.rr, 3),
            variant=signal.variant,
            detail=(f"{new_trade.side} @ {new_trade.entry:.2f}, "
                    f"SL={new_trade.sl:.2f}, TP={new_trade.tp:.2f}"),
            related_trade_id=new_trade.id,
        ))
        log.info("%s OPENED %s %s @ %.2f SL=%.2f TP=%.2f RR=%.2f",
                 inst, new_trade.side, new_trade.variant,
                 new_trade.entry, new_trade.sl, new_trade.tp, new_trade.rr_at_open)

    except (CapitalAuthError, CapitalAPIError) as e:
        instance.append_tick_log(TickLogEntry(
            timestamp=_now(), action="error",
            decision="capital_api_error", detail=str(e)[:200],
        ))
        log.warning("%s Capital-Fehler: %s", inst, e)


LOOP_DURATION_S = 1740  # 29 Min Loop — fast volle 30-Min-Timeout, nur 1 Naht/30min
TICK_INTERVAL_S = 30    # alle 30s ein Tick für alle Instrumente


async def _tick_all(state: BotState) -> bool:
    """Ein Tick für alle Instrumente. Gibt True zurück wenn State geändert."""
    changed = False
    for instrument in INSTRUMENTS:
        instance = state.ensure(instrument)
        open_before = len(instance.open_trades)
        try:
            await _tick(instance)
            instance.last_tick = _now()
            if instance.last_error:
                instance.last_error = None
        except Exception as e:
            log.exception("Tick %s gecrasht: %s", instrument, e)
            instance.last_error = f"{type(e).__name__}: {e}"
        if len(instance.open_trades) != open_before:
            changed = True
    return changed


async def main() -> None:
    import time
    log.info("=== GH Actions Bot-Loop gestartet (%.0fs, Tick alle %ds) ===",
             LOOP_DURATION_S, TICK_INTERVAL_S)
    state = load_state()
    deadline = time.monotonic() + LOOP_DURATION_S
    tick_num = 0

    while time.monotonic() < deadline:
        tick_start = time.monotonic()
        tick_num += 1
        log.info("── Tick #%d ──", tick_num)

        changed = await _tick_all(state)

        # State immer speichern (wird am Ende committed)
        save_state(state)

        if changed:
            log.info("Trade-Event — State persistiert")
            for name, inst in state.instances.items():
                info = f"OPEN {inst.open_trades[0].side}" if inst.open_trades else "flat"
                log.info("  %-8s equity=%8.2f  %s", name, inst.equity, info)

        elapsed = time.monotonic() - tick_start
        sleep = max(1.0, TICK_INTERVAL_S - elapsed)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(sleep, remaining))

    log.info("=== Loop beendet — Zusammenfassung ===")
    for name, inst in state.instances.items():
        info = f"OPEN {inst.open_trades[0].side}" if inst.open_trades else "flat"
        log.info("  %-8s equity=%8.2f  %s", name, inst.equity, info)


if __name__ == "__main__":
    asyncio.run(main())
