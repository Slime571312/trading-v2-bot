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

    # 1. 1m-Bar für Intrabar-Check
    latest_bar = None
    try:
        df_1m = await load_candles(inst, "1m", bars=20)
        if not df_1m.empty:
            latest_bar = df_1m.iloc[-1]
    except (CapitalAuthError, CapitalAPIError) as e:
        log.warning("%s: 1m fetch fehlgeschlagen: %s", inst, e)

    # 2. Offene Trades gegen aktuelle Bar checken
    if latest_bar is not None:
        still_open = []
        for trade in instance.open_trades:
            exit_hit = paper_broker.check_intrabar_exit(trade, latest_bar)
            if exit_hit is None:
                still_open.append(trade)
                continue
            exit_raw, reason = exit_hit
            closed = paper_broker.close_position(trade, exit_raw, latest_bar, reason)
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
            log.info("%s CLOSED %s %s → %.2f (%+.2fR)",
                     inst, closed.side, closed.exit_reason,
                     closed.pnl_abs, closed.r_multiple)
        instance.open_trades = still_open

    # 3. Signal-Eval — nur wenn kein offener Trade
    if instance.open_trades:
        instance.append_tick_log(TickLogEntry(
            timestamp=_now(), action="skip", decision="already_in_position",
            detail=f"open: {instance.open_trades[0].id}",
        ))
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


async def main() -> None:
    log.info("=== GitHub Actions Tick gestartet ===")
    state = load_state()

    for instrument in INSTRUMENTS:
        instance = state.ensure(instrument)
        log.info("--- %s  equity=%.2f  open=%d ---",
                 instrument, instance.equity, len(instance.open_trades))
        try:
            await _tick(instance)
            instance.last_tick = _now()
            if instance.last_error:
                instance.last_error = None
        except Exception as e:
            log.exception("Tick %s gecrasht: %s", instrument, e)
            instance.last_error = f"{type(e).__name__}: {e}"

    save_state(state)

    log.info("=== Zusammenfassung ===")
    for name, inst in state.instances.items():
        trade_info = f"OPEN {inst.open_trades[0].side}" if inst.open_trades else "flat"
        log.info("  %-8s equity=%8.2f  %s", name, inst.equity, trade_info)


if __name__ == "__main__":
    asyncio.run(main())
