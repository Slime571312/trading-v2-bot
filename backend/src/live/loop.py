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
from src.live.confidence_reeval import re_evaluate_confidence_tier, adjust_position_for_tier

from . import paper_broker
from .state import (
    BotInstance, BotState, ClosedTrade, OpenTrade, TickLogEntry,
    save_state, _now,
)
from . import notifier
from .equity_filter import equity_filter_factor
from .trade_limits import can_trade, correlation_gate_blocked, get_limits_for

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

SIGNAL_TICKS_INTERVAL = 5  # alle 5 Ticks Engine-Eval (bei 60s-Tick = 5min)
LIVE_TFS = ["1d", "1h", "30m", "15m", "5m", "1m"]

# Modul-State für Alert-Deduplizierung (BotInstance ist slots=True → kein
# dynamisches Attribut möglich). Key: instrument-Name, Value: letzter Error
# der via Telegram alarmiert wurde — gleicher Error löst nicht erneut aus.
_last_alerted_error: dict[str, str] = {}


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
            # Transiente Netz-/DNS-/Read-Fehler nicht als Stack-Trace + Telegram-Alarm
            # ausgeben — die sind erwartet bei Mac-Sleep / WiFi-Aussetzer und der
            # Circuit-Breaker übernimmt das Throttling. Nur als WARNING loggen,
            # damit der Log nicht 100× pro Outage zugespammt wird.
            import httpx
            is_transient = isinstance(e, (httpx.TransportError, OSError)) or \
                isinstance(e, (CapitalAPIError, CapitalAuthError))
            if is_transient:
                log.warning("Tick-Loop %s transient: %s: %s",
                            inst_name, type(e).__name__, str(e)[:120])
            else:
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
            # Telegram-Alert nur bei NICHT-transienten + NEUEN Errors
            # (transient = Netz-Glitch, kommt + geht; kein Page-Wert).
            err_snippet = str(e)[:200]
            if not is_transient and _last_alerted_error.get(inst_name) != err_snippet:
                await notifier.alert_critical_error(inst_name, str(e))
                _last_alerted_error[inst_name] = err_snippet

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

    # ── 1. 1m-Bars laden — gezielt Lücke seit letztem Tick + Puffer.
    # `bars_needed` skaliert mit der Zeit seit `last_seen_bar` des ältesten
    # offenen Trades — verhindert dass SL/TP-Hits in Outages übersehen werden,
    # ohne wie historisches Backfill der Vergangenheit zu vertrauen.
    df_1m = pd.DataFrame()
    latest_bar = None
    try:
        import math
        bars_needed = 5
        if instance.open_trades:
            oldest_seen = min(
                (t.last_seen_bar or t.open_time) for t in instance.open_trades
            )
            gap_s = (_now() - oldest_seen).total_seconds()
            # Lücke + 2min Puffer, gecappt bei 30 Bars (~30 min) — alles darüber
            # ist eher Backfill als Live-Tick.
            bars_needed = min(30, max(5, math.ceil(gap_s / 60) + 2))
        df_1m = await load_candles(inst_name, "1m", bars=bars_needed, refresh=True)
        latest_bar = df_1m.iloc[-1] if not df_1m.empty else None
    except (CapitalAuthError, CapitalAPIError) as e:
        log.warning("%s tick %d: 1m-Fetch fehlgeschlagen: %s", inst_name, tick_count, e)

    # ── 2. Offene Trades — Partial-Close + Catch-up (Bot/Partial-Close.md) ──
    # Push-Payloads sammeln und NACH dem Lock-Release feuern — sonst blockiert
    # ein langsamer Websocket-Client den state_lock und damit andere Bot-Instances.
    push_queue: list[dict] = []
    if not df_1m.empty:
        latest_seen = df_1m.index[-1].to_pydatetime()
        if latest_seen.tzinfo is None:
            from datetime import timezone as _tz
            latest_seen = latest_seen.replace(tzinfo=_tz.utc)
        async with state_lock:
            still_open: list[OpenTrade] = []
            for trade in instance.open_trades:
                trade.last_seen_bar = latest_seen   # für lückenloses Catch-up beim nächsten Tick
                events = paper_broker.scan_with_partial_close(trade, df_1m)
                full_closed = False
                for ev in events:
                    if ev["kind"] == "partial_tp1":
                        instance.equity += ev["pnl"]
                        # DON'T MOVE SL! Let it stay at original (protect remaining 50%)
                        instance.append_tick_log(TickLogEntry(
                            timestamp=trade.partial_close_time or _now(),
                            action="close", decision="partial_tp1",
                            variant=trade.variant,
                            detail=(f"{trade.pct1*100:.0f}% @ {trade.tp1:.2f} → "
                                    f"+{ev['pnl']:.2f}, SL unchanged (protect TP2)"),
                            related_trade_id=trade.id,
                        ))
                        log.info("%s PARTIAL_TP1 %s %.0f%% @ %.2f → +%.2f, SL unchanged",
                                 inst_name, trade.side, trade.pct1*100, trade.tp1, ev["pnl"])
                        push_queue.append({
                            "type": "partial_close", "instrument": inst_name,
                            "trade_id": trade.id, "level": "tp1",
                            "price": trade.tp1, "pnl": ev["pnl"],
                            "new_sl": trade.sl,
                        })
                    elif ev["kind"] == "partial_tp2":
                        instance.equity += ev["pnl"]
                        instance.append_tick_log(TickLogEntry(
                            timestamp=trade.partial_close_time or _now(),
                            action="close", decision="partial_tp2",
                            variant=trade.variant,
                            detail=(f"{trade.pct2*100:.0f}% @ {trade.tp2:.2f} → "
                                    f"+{ev['pnl']:.2f}, SL→TP1 ({trade.tp1:.2f}) [Lock-Profit]"),
                            related_trade_id=trade.id,
                        ))
                        log.info("%s PARTIAL_TP2 %s %.0f%% @ %.2f → +%.2f, SL→TP1",
                                 inst_name, trade.side, trade.pct2*100, trade.tp2, ev["pnl"])
                        push_queue.append({
                            "type": "partial_close", "instrument": inst_name,
                            "trade_id": trade.id, "level": "tp2",
                            "price": trade.tp2, "pnl": ev["pnl"],
                            "new_sl": trade.sl,
                        })
                    elif ev["kind"] == "full_close":
                        if any(c.id == trade.id for c in instance.closed_trades):
                            log.warning("%s: Trade %s schon geschlossen — skip duplicate close",
                                        inst_name, trade.id)
                            full_closed = True
                            continue
                        closed = paper_broker.close_position(
                            trade, ev["exit"], ev["bar"], ev["reason"], close_time=ev["ts"],
                        )
                        # Equity-Buchung: nur Runner-Portion (closed.pnl_abs).
                        # Partials wurden schon oben bei partial_tp1/tp2 gebucht.
                        instance.equity += closed.pnl_abs
                        instance.append_closed_trade(closed)
                        instance.setup_stage = "idle"  # FSM zurück auf Anfang
                        instance.append_tick_log(TickLogEntry(
                            timestamp=closed.close_time, action="close",
                            decision=f"closed_{ev['reason']}",
                            variant=closed.variant,
                            detail=(f"{closed.side} rest @ {closed.exit:.2f} → "
                                    f"{closed.pnl_abs:+.2f} | total {closed.total_pnl:+.2f} "
                                    f"({closed.r_multiple:+.2f}R)"),
                            related_trade_id=closed.id,
                        ))
                        log.info("%s CLOSED %s %s rest %+.2f total %+.2f (%+.2fR)",
                                 inst_name, closed.side, closed.exit_reason,
                                 closed.pnl_abs, closed.total_pnl, closed.r_multiple)
                        push_queue.append({
                            "type": "trade_closed", "instrument": inst_name,
                            "trade": closed.to_json(),
                        })
                        # Telegram-Alert (Fire-and-forget außerhalb des Locks).
                        # Speichern wir als Payload, senden später.
                        push_queue.append({
                            "_telegram_close": True,
                            "instrument": inst_name,
                            "side": closed.side,
                            "entry": closed.entry,
                            "exit": closed.exit,
                            "pnl": closed.total_pnl,
                            "r_multiple": closed.r_multiple,
                            "reason": closed.exit_reason,
                            "equity": instance.equity,
                        })
                        full_closed = True
                if not full_closed:
                    still_open.append(trade)
            instance.open_trades = still_open
    # Push außerhalb des Locks — schneller Lock-Release auch wenn WS-Client träge ist.
    # `_telegram_close` markiert Payloads die KEIN WS-Update sind, sondern ein
    # Telegram-Alert (Trade-Close mit Equity-Snapshot).
    for payload in push_queue:
        if payload.get("_telegram_close"):
            await notifier.alert_trade_closed(
                instrument=payload["instrument"], side=payload["side"],
                entry=payload["entry"], exit_price=payload["exit"],
                pnl=payload["pnl"], r_multiple=payload["r_multiple"],
                reason=payload["reason"], equity=payload["equity"],
            )
        else:
            await push_to_dashboard(payload)

    # ── 2b. Confidence-Tier Re-Evaluation (alle 15 Ticks @ 60s = 15min) ─────
    # Mid-trade: re-check 3 gates (regime, structure, momentum) und adjust
    # Position-Risk wenn Tier schlecht wird (A→B, B→C, etc.)
    # Fix 2026-06-26: vorher 180 Ticks bei 60s-Interval = 3h statt 15min.
    if tick_count % 15 == 0 and instance.open_trades and not df_1m.empty:
        reeval_queue: list[dict] = []
        async with state_lock:
            try:
                # Load all TF-bars for gate evaluation
                bars_for_reeval: dict[str, pd.DataFrame] = {}
                for tf in ["1d", "1h", "5m", "1m"]:
                    try:
                        if tf == "1m":
                            bars_for_reeval[tf] = df_1m
                        else:
                            df_tf = await load_candles(inst_name, tf, bars=50)
                            if not df_tf.empty:
                                bars_for_reeval[tf] = df_tf
                    except Exception as e:
                        log.debug("%s: %s-bars für re-eval nicht geladen: %s",
                                 inst_name, tf, str(e)[:50])

                for position in instance.open_trades:
                    old_tier = position.confidence_tier_at_open or "A"
                    try:
                        new_tier = re_evaluate_confidence_tier(position, bars_for_reeval)
                        # Re-Eval-Tier (A/B/C/D) ist eine andere Skala als das
                        # initial gespeicherte Score-Tier (high/normal/reduced/skip).
                        # Statt jeden Refresh als "Change" zu loggen, nur dann
                        # eingreifen wenn das Re-Eval-Ergebnis tatsächlich kritisch
                        # ist (≥ 2 Gates gefailt = C oder D).
                        if new_tier in ("C", "D"):
                            log.info(
                                "Confidence re-eval %s: critical tier=%s (entry=%s)",
                                position.id, new_tier, old_tier,
                            )
                            adjust_position_for_tier(position, new_tier)
                            instance.append_tick_log(TickLogEntry(
                                timestamp=_now(),
                                action="confidence_re_eval",
                                decision=f"gate_fail_{new_tier}",
                                detail=f"entry={old_tier} → reeval={new_tier}, SL adjusted",
                                related_trade_id=position.id,
                            ))
                            reeval_queue.append({
                                "type": "position_adjustment",
                                "instrument": inst_name,
                                "position_id": position.id,
                                "action": "confidence_re_eval",
                                "from_tier": old_tier,
                                "to_tier": new_tier,
                                "new_sl": position.sl,
                            })
                    except Exception as e:
                        log.warning("Confidence re-eval error for %s: %s",
                                   position.id, str(e)[:100])
                save_state(state)
            except Exception as e:
                log.warning("Confidence re-eval batch error: %s", str(e)[:100])

        # Push außerhalb des Locks
        for payload in reeval_queue:
            await push_to_dashboard(payload)

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

    # Volle Trade-Limits-Pipeline (Bot/TradeGate.md, Bot/Trade-Limits.md) —
    # per-Instrument-Config (BTC strikter als Indizes).
    cfg = get_limits_for(inst_name, instance.initial_capital)
    allowed, reason = can_trade(_now(), inst_name, instance.equity,
                                 instance.closed_trades, cfg=cfg)
    if not allowed:
        instance.append_tick_log(TickLogEntry(
            timestamp=_now(), action="skip", decision="trade_limit_blocked",
            detail=reason or "unknown",
        ))
        log.info("%s Trade-Limit: %s", inst_name, reason)
        # Telegram-Alert nur bei Daily-Loss-Limit (das ist das einzige
        # Trade-Limit das einen User-Hinweis rechtfertigt)
        if reason and "daily_loss_limit" in reason:
            last_log = instance.tick_log[-2] if len(instance.tick_log) >= 2 else None
            already_alerted = last_log and "daily_loss_limit" in (last_log.detail or "")
            if not already_alerted:
                pnl_today = sum(
                    t.pnl_abs for t in instance.closed_trades
                    if t.close_time.date() == _now().date()
                )
                loss_pct = abs(pnl_today / instance.initial_capital * 100)
                await notifier.alert_daily_loss_limit(inst_name, instance.equity, loss_pct)
        return

    # Portfolio-Korrelations-Gate (Indizes-Cluster max 2/3 gleichzeitig offen)
    others_open = [n for n, o in state.instances.items()
                   if n != inst_name and o.open_trades]
    corr_blocked, corr_reason = correlation_gate_blocked(inst_name, others_open)
    if corr_blocked:
        instance.append_tick_log(TickLogEntry(
            timestamp=_now(), action="skip", decision="correlation_gate",
            detail=corr_reason or "",
        ))
        log.info("%s Korrelations-Gate: %s", inst_name, corr_reason)
        return

    # Equity-Curve-Filter (Vault/Bot/Equity-Curve-Filter.md): bei Drawdown
    # automatisch Size reduzieren. Bei > 8% DD: gar kein neuer Trade.
    _ec_factor, _ec_tier, _ec_dd_pct = equity_filter_factor(
        instance.initial_capital,
        instance.equity,
        [t.pnl_abs for t in instance.closed_trades],
    )
    if _ec_factor <= 0:
        instance.append_tick_log(TickLogEntry(
            timestamp=_now(), action="skip", decision="equity_filter_severe_dd",
            detail=f"dd={_ec_dd_pct:.1f}% — kein neuer Trade bis Recovery",
        ))
        log.warning("%s Equity-Curve-Filter: severe DD %.1f%% — skip", inst_name, _ec_dd_pct)
        # Telegram-Alert nur einmal pro DD-Tag (heuristisch: nur wenn letzter
        # tick_log-Eintrag NICHT auch schon severe_dd war)
        last_log = instance.tick_log[-2] if len(instance.tick_log) >= 2 else None
        if last_log is None or last_log.decision != "equity_filter_severe_dd":
            await notifier.alert_drawdown_severe(inst_name, _ec_dd_pct, instance.equity)
        return
    if _ec_tier != "healthy":
        log.info("%s Equity-Filter: %s dd=%.1f%% → size ×%.2f",
                 inst_name, _ec_tier, _ec_dd_pct, _ec_factor)

    try:
        # External-Data-Refresh (async, non-blocking, idempotent durch Cache).
        # Engine.evaluate liest die News/Funding-Caches synchron — wir warmen
        # sie hier auf, damit die Engine immer aktuelle Daten sieht.
        from src.strategy_core import news as news_mod
        from src.strategy_core import crypto_sentiment as crypto_mod
        await news_mod.refresh_calendar_async()
        if inst_name == "BTC":
            await crypto_mod.refresh_funding_async()

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

        # SMT-Reference-Bars für die HTF-Stufen laden (1h/30m/15m).
        # Wenn das Mapping fehlt oder der Fetch fehlschlägt → SMT bleibt neutral.
        from src.strategy_core import smt as smt_mod
        ref_bars: dict[str, pd.DataFrame] | None = None
        ref_inst = smt_mod.get_reference_instrument(inst_name)
        if ref_inst:
            ref_bars = {}
            for htf in ("1h", "30m", "15m"):
                try:
                    df_ref = await load_candles(ref_inst, htf, bars=200)
                    if not df_ref.empty:
                        ref_bars[htf] = df_ref
                except (CapitalAuthError, CapitalAPIError) as e:
                    log.debug("%s: ref-bars %s/%s nicht ladbar: %s",
                              inst_name, ref_inst, htf, e)

        bias = compute_bias(bars["1d"])
        stage_tracker: list[str] = []
        signal = evaluate_signal(
            inst_name, bars,
            rr_threshold=instance.rr_threshold,
            sweep_lookback_bars=instance.sweep_lookback,
            ref_bars=ref_bars,
            stage_tracker=stage_tracker,
        )
        # Höchste erreichte Stage = letztes Tracker-Element (= wo Engine
        # entweder Signal gebaut hat oder gestoppt hat).
        new_stage = stage_tracker[-1] if stage_tracker else "idle"
        if new_stage != instance.setup_stage:
            log.info("%s FSM: %s → %s", inst_name, instance.setup_stage, new_stage)
            instance.setup_stage = new_stage

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

        # Combined Size-Multiplikator: OPEX × Conviction-Grade × Equity-Filter
        from src.strategy_core import sessions as sessions_mod
        _opex_mult = sessions_mod.opex_size_multiplier(_now().date())
        _conv_mult = getattr(signal, "size_multiplier", 1.0)
        _effective_risk = instance.risk_pct * _opex_mult * _conv_mult * _ec_factor

        new_trade = paper_broker.open_position(
            signal, instance.equity, _effective_risk, current_bar,
        )
        if new_trade is not None and (_opex_mult < 1.0 or _conv_mult < 1.0):
            log.info("%s Size-Anpassung: risk %.2f%% × OPEX %.2f × Conv %.2f → %.3f%% (grade=%s)",
                     inst_name, instance.risk_pct * 100, _opex_mult, _conv_mult,
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

        async with state_lock:
            if instance.open_trades:
                log.info("%s: zwischenzeitlich Position eröffnet — skip", inst_name)
                return
            instance.open_trades.append(new_trade)
            instance.setup_stage = "in_trade"
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
        await notifier.alert_trade_opened(
            instrument=inst_name, side=new_trade.side,
            entry=new_trade.entry, sl=new_trade.sl, tp=new_trade.tp,
            rr=new_trade.rr_at_open, variant=new_trade.variant,
            conviction=signal.conviction_grade,
        )
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
