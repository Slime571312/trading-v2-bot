"""Tick-Loop des Live-Paper-Bots.

Asyncio-Task, der im FastAPI-Prozess läuft. Pro Tick:
1. Lade aktuellste 1m-Bar für jedes Instrument (Cache, TTL 60s → frisch)
2. Prüfe offene Trades intrabar gegen die 1m-Bar (SL/TP)
3. Alle N Ticks (Default: jeden 5. = 5min): rufe `engine.evaluate()`
   für jedes Instrument auf das keine offene Position hat
4. Persistiere State, pushe Updates an alle WebSocket-Clients

Robustheit:
- Pro-Instrument-Try/Except → ein Capital-Fehler kappt nicht den ganzen Tick
- Bot-Stop respektiert: vor jedem Schritt prüfen
- Asyncio-Lock um State-Mutationen → keine Race mit HTTP-Handlern
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pandas as pd

from src.api.ws import push_to_dashboard
from src.brokers.capital_com import CapitalAPIError, CapitalAuthError
from src.data.fetcher import load_candles
from src.strategy_core import evaluate as evaluate_signal

from . import paper_broker
from .state import BotState, ClosedTrade, OpenTrade, save_state, _now

log = logging.getLogger(__name__)

# Alle wie viele Ticks soll die Engine neu evaluieren? Bei 60s-Tick und 5m-Strategie:
# 5 Ticks = 5 Minuten = Bar-Close-Rhythmus.
SIGNAL_TICKS_INTERVAL = 5

# TFs die das Engine-Eval braucht. 1m für intrabar SL/TP, 5m+15m+30m+1h+1d für Engine.
LIVE_TFS = ["1d", "1h", "30m", "15m", "5m", "1m"]


async def run_tick_loop(state: BotState, state_lock: asyncio.Lock) -> None:
    """Endlos-Loop bis `state.running` auf False geht. Wird als asyncio.Task gestartet."""
    log.info("Tick-Loop gestartet: tick_interval=%ds, instruments=%s",
             state.tick_interval_s, state.instruments)

    tick_count = 0
    while state.running:
        tick_count += 1
        tick_start = _now()
        try:
            await _do_tick(state, state_lock, tick_count)
        except asyncio.CancelledError:
            log.info("Tick-Loop abgebrochen (CancelledError)")
            raise
        except Exception as e:
            log.exception("Tick-Loop unerwarteter Fehler — fahre fort: %s", e)
            async with state_lock:
                state.last_error = f"{type(e).__name__}: {e}"
                save_state(state)
            await push_to_dashboard({"type": "error", "message": str(e)})

        async with state_lock:
            state.last_tick = tick_start
            save_state(state)

        # Sleep so dass jeder Tick min. tick_interval_s vom vorherigen Start entfernt ist
        elapsed = (_now() - tick_start).total_seconds()
        sleep_for = max(1.0, state.tick_interval_s - elapsed)
        try:
            await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            raise

    log.info("Tick-Loop beendet (running=False)")


async def _do_tick(state: BotState, state_lock: asyncio.Lock, tick_count: int) -> None:
    """Ein einzelner Tick. Atomar pro State-Mutation via Lock."""
    eval_this_tick = (tick_count % SIGNAL_TICKS_INTERVAL == 1)

    # ── Step 1: pro Instrument 1m-Bar holen für intrabar-Check ───────────
    latest_bars_1m: dict[str, pd.Series] = {}
    for inst in state.instruments:
        try:
            df = await load_candles(inst, "1m", bars=20)
            if not df.empty:
                latest_bars_1m[inst] = df.iloc[-1]
        except (CapitalAuthError, CapitalAPIError) as e:
            log.warning("tick %d: 1m-Fetch %s fehlgeschlagen: %s", tick_count, inst, e)

    # ── Step 2: offene Trades intrabar-Check (SL/TP) ─────────────────────
    closed_this_tick: list[ClosedTrade] = []
    async with state_lock:
        still_open: list[OpenTrade] = []
        for trade in state.open_trades:
            bar = latest_bars_1m.get(trade.instrument)
            if bar is None:
                still_open.append(trade)
                continue
            exit_hit = paper_broker.check_intrabar_exit(trade, bar)
            if exit_hit is None:
                still_open.append(trade)
                continue
            exit_raw, reason = exit_hit
            closed = paper_broker.close_position(trade, exit_raw, bar, reason)
            state.equity += closed.pnl_abs
            state.closed_trades.append(closed)
            closed_this_tick.append(closed)
            log.info(
                "TRADE CLOSED %s %s %s → %.2f (%+.2fR, reason=%s)",
                closed.instrument, closed.side, closed.id,
                closed.pnl_abs, closed.r_multiple, closed.exit_reason,
            )
        state.open_trades = still_open
        if closed_this_tick:
            save_state(state)

    for ct in closed_this_tick:
        await push_to_dashboard({"type": "trade_closed", "trade": ct.to_json()})

    # ── Step 3: optional Engine-Eval für neue Entries ────────────────────
    if not eval_this_tick:
        await push_to_dashboard(_state_snapshot(state))
        return

    async with state_lock:
        state.last_signal_check = _now()
    open_instruments = {t.instrument for t in state.open_trades}

    opened_this_tick: list[OpenTrade] = []
    for inst in state.instruments:
        if inst in open_instruments:
            continue
        try:
            bars: dict[str, pd.DataFrame] = {}
            for tf in LIVE_TFS:
                df = await load_candles(inst, tf, bars=200)
                if not df.empty:
                    bars[tf] = df
            if "1d" not in bars or "5m" not in bars:
                continue
            signal = evaluate_signal(
                inst, bars,
                rr_threshold=state.rr_threshold,
                sweep_lookback_bars=state.sweep_lookback,
            )
            if signal is None:
                continue

            current_bar = latest_bars_1m.get(inst)
            if current_bar is None:
                current_bar = bars["5m"].iloc[-1]
            new_trade = paper_broker.open_position(
                signal, state.equity, state.risk_pct, current_bar,
            )
            if new_trade is None:
                continue

            async with state_lock:
                # State-Validation: in der Zwischenzeit doch eine Position?
                if any(t.instrument == inst for t in state.open_trades):
                    log.info("tick %d: %s zwischenzeitlich Position — skip", tick_count, inst)
                    continue
                state.open_trades.append(new_trade)
                save_state(state)
            opened_this_tick.append(new_trade)
            log.info(
                "TRADE OPENED %s %s %s @ %.2f, SL=%.2f, TP=%.2f, RR=%.2f, size=%.4f",
                new_trade.instrument, new_trade.side, new_trade.id,
                new_trade.entry, new_trade.sl, new_trade.tp,
                new_trade.rr_at_open, new_trade.size,
            )
        except (CapitalAuthError, CapitalAPIError) as e:
            log.warning("tick %d: engine-eval %s gescheitert: %s", tick_count, inst, e)
        except Exception as e:
            log.exception("tick %d: engine-eval %s unerwarteter Fehler: %s", tick_count, inst, e)

    for ot in opened_this_tick:
        await push_to_dashboard({"type": "trade_opened", "trade": ot.to_json()})

    await push_to_dashboard(_state_snapshot(state))


def _state_snapshot(state: BotState) -> dict:
    return {"type": "state", "state": state.to_json()}
