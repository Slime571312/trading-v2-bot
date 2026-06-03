"""Tick-Loop pro Bot-Instance.

Eine asyncio.Task pro Instrument. Pro Tick:
1. Lade 1m-Bar fürs Instrument (intrabar-Check)
2. Prüfe offenen Trade gegen 1m-Bar (SL/TP intrabar)
3. Alle N Ticks (Default 5): re-eval engine wenn kein offener Trade,
   öffne ggf. Position
4. Persistiere State + TickLog, pushe Update an WebSocket-Clients

Jeder relevante Tick-Schritt erzeugt einen TickLogEntry — sichtbar im UI als
„warum hat der Bot das gemacht"-Timeline.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pandas as pd

from src.api.ws import push_to_dashboard
from src.brokers.capital_com import CapitalAPIError, CapitalAuthError
from src.data.fetcher import load_candles
from src.strategy_core import evaluate as evaluate_signal
from src.strategy_core.bias import compute_bias

from . import paper_broker
from .state import (
    BotInstance, BotState, ClosedTrade, OpenTrade, TickLogEntry,
    save_state, _now,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

SIGNAL_TICKS_INTERVAL = 5  # alle 5 Ticks Engine-Eval (bei 60s-Tick = 5min)
LIVE_TFS = ["1d", "1h", "30m", "15m", "5m", "1m"]


async def run_tick_loop(
    state: BotState,
    instance: BotInstance,
    state_lock: asyncio.Lock,
) -> None:
    """Endlos bis `instance.running=False`. Pro Tick: SL/TP-Check + ggf. Eval.

    `state_lock` ist GLOBAL (1 Lock für die ganze BotState), damit
    save_state() serialisiert ist.
    """
    inst_name = instance.instrument
    log.info("Tick-Loop %s gestartet (interval=%ds)", inst_name, instance.tick_interval_s)

    tick_count = 0
    while instance.running:
        tick_count += 1
        tick_start = _now()
        tick_ok = False
        try:
            await _do_tick(state, instance, state_lock, tick_count)
            tick_ok = True
        except asyncio.CancelledError:
            log.info("Tick-Loop %s abgebrochen", inst_name)
            raise
        except Exception as e:
            log.exception("Tick-Loop %s Fehler: %s", inst_name, e)
            async with state_lock:
                instance.last_error = f"{type(e).__name__}: {e}"
                instance.append_tick_log(TickLogEntry(
                    timestamp=tick_start, action="error",
                    decision="tick_crashed", detail=str(e)[:200],
                ))
                save_state(state)
            await push_to_dashboard({
                "type": "error", "instrument": inst_name, "message": str(e),
            })

        async with state_lock:
            instance.last_tick = tick_start
            # Alten Fehler löschen wenn der Bot wieder sauber durchläuft —
            # sonst hängt die rote Fehler-Box im UI ewig fest.
            if tick_ok and instance.last_error is not None:
                log.info("Tick-Loop %s wieder OK — clearing last_error", inst_name)
                instance.last_error = None
            save_state(state)

        # Push State-Update zum Browser
        await push_to_dashboard({
            "type": "instance_update", "instrument": inst_name,
            "instance": instance.to_json(),
        })

        elapsed = (_now() - tick_start).total_seconds()
        sleep_for = max(1.0, instance.tick_interval_s - elapsed)
        try:
            await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            raise

    log.info("Tick-Loop %s beendet", inst_name)


async def _do_tick(
    state: BotState,
    instance: BotInstance,
    state_lock: asyncio.Lock,
    tick_count: int,
) -> None:
    """Ein Tick = SL/TP-Check + (alle N Ticks) Engine-Eval."""
    inst_name = instance.instrument
    eval_this_tick = (tick_count % SIGNAL_TICKS_INTERVAL == 1)

    # ── 1. 1m-Bars laden — Catch-up: genug Bars seit Trade-Öffnung ───
    df_1m = pd.DataFrame()
    latest_bar = None
    try:
        import math
        bars_needed = 50
        if instance.open_trades:
            oldest = min(t.open_time for t in instance.open_trades)
            age_s = (_now() - oldest).total_seconds()
            bars_needed = min(1000, max(50, math.ceil(age_s / 60) + 20))
        df_1m = await load_candles(inst_name, "1m", bars=bars_needed, refresh=True)
        latest_bar = df_1m.iloc[-1] if not df_1m.empty else None
    except (CapitalAuthError, CapitalAPIError) as e:
        log.warning("%s tick %d: 1m-Fetch fehlgeschlagen: %s", inst_name, tick_count, e)

    # ── 2. Offene Trades — Catch-up Scan über alle Bars seit Öffnung ─
    if not df_1m.empty:
        async with state_lock:
            still_open: list[OpenTrade] = []
            for trade in instance.open_trades:
                hit = paper_broker.scan_exit_over_bars(trade, df_1m)
                if hit is None:
                    still_open.append(trade)
                    continue
                hit_bar, exit_raw, reason, hit_ts = hit
                closed = paper_broker.close_position(trade, exit_raw, hit_bar, reason, close_time=hit_ts)
                instance.equity += closed.pnl_abs
                instance.closed_trades.append(closed)
                instance.append_tick_log(TickLogEntry(
                    timestamp=_now(), action="close",
                    decision=f"closed_{reason}",
                    variant=closed.variant,
                    detail=(f"{closed.side} @ {closed.exit:.2f} → "
                            f"{closed.pnl_abs:+.2f} ({closed.r_multiple:+.2f}R)"),
                    related_trade_id=closed.id,
                ))
                log.info("%s CLOSED %s %s %s → %.2f (%+.2fR)",
                         inst_name, closed.side, closed.id, closed.exit_reason,
                         closed.pnl_abs, closed.r_multiple)
                await push_to_dashboard({
                    "type": "trade_closed", "instrument": inst_name,
                    "trade": closed.to_json(),
                })
            instance.open_trades = still_open

    # ── 3. Engine-Eval für neue Entries (nur alle N Ticks) ──────────
    if not eval_this_tick:
        return

    async with state_lock:
        instance.last_signal_check = _now()

    if instance.open_trades:
        instance.append_tick_log(TickLogEntry(
            timestamp=_now(), action="skip", decision="already_in_position",
            detail=f"open: {instance.open_trades[0].id}",
        ))
        return

    # Daily-Loss-Limit (Vault Risk.md): max 2 SL-Hits pro Tag
    today = _now().date()
    sl_today = sum(
        1 for t in instance.closed_trades
        if t.exit_reason == "sl" and t.close_time.date() == today
    )
    if sl_today >= 2:
        instance.append_tick_log(TickLogEntry(
            timestamp=_now(), action="skip", decision="daily_loss_limit",
            detail=f"{sl_today} SL-Hits heute — kein weiterer Entry",
        ))
        return

    try:
        bars: dict[str, pd.DataFrame] = {}
        for tf in LIVE_TFS:
            df = await load_candles(inst_name, tf, bars=200)
            if not df.empty:
                bars[tf] = df
        if "1d" not in bars or "5m" not in bars:
            instance.append_tick_log(TickLogEntry(
                timestamp=_now(), action="error",
                decision="missing_required_tf",
                detail=f"have: {sorted(bars.keys())}",
            ))
            return

        bias = compute_bias(bars["1d"])
        signal = evaluate_signal(
            inst_name, bars,
            rr_threshold=instance.rr_threshold,
            sweep_lookback_bars=instance.sweep_lookback,
        )

        if signal is None:
            instance.append_tick_log(TickLogEntry(
                timestamp=_now(), action="eval", decision="no_setup",
                bias=bias.direction,
                detail=(
                    "neutraler Daily-Bias" if bias.direction == "neutral"
                    else "kein HTF-Sweep + LTF-BOS im Lookback-Fenster"
                ),
            ))
            return

        current_bar = latest_bar if latest_bar is not None else bars["5m"].iloc[-1]
        new_trade = paper_broker.open_position(
            signal, instance.equity, instance.risk_pct, current_bar,
        )
        if new_trade is None:
            instance.append_tick_log(TickLogEntry(
                timestamp=_now(), action="skip",
                decision="degenerate_signal_sl_eq_entry",
                bias=bias.direction, htf_used=signal.htf_used,
                ltf_used=signal.ltf_used, rr_computed=signal.rr,
                variant=signal.variant,
            ))
            return

        async with state_lock:
            if instance.open_trades:
                log.info("%s: zwischenzeitlich Position eröffnet — skip", inst_name)
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
        log.info("%s OPENED %s %s %s @ %.2f SL=%.2f TP=%.2f RR=%.2f",
                 inst_name, new_trade.side, new_trade.id, new_trade.variant,
                 new_trade.entry, new_trade.sl, new_trade.tp, new_trade.rr_at_open)
        await push_to_dashboard({
            "type": "trade_opened", "instrument": inst_name,
            "trade": new_trade.to_json(),
        })

    except (CapitalAuthError, CapitalAPIError) as e:
        instance.append_tick_log(TickLogEntry(
            timestamp=_now(), action="error",
            decision="capital_api_error", detail=str(e)[:200],
        ))
        log.warning("%s eval Capital-Fehler: %s", inst_name, e)
