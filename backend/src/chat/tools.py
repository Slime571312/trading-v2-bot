"""Tool-Definitionen für den Chat-Bot — Read + Trigger + Vorschlag-Scope.

Tools sind statisch — die Tools-Liste wird gecacht (cache_control auf dem letzten
Tool). Der Dispatcher ist async, weil run_backtest mehrere TFs vom Capital
fetchen kann.

Spec: Trading/Bot/Chat-Engine.md + wiki/sources/2026-05-27-anthropic-advanced-tool-use.md
"""
from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd

from src.brokers.capital_com import CapitalAPIError, CapitalAuthError
from src.backtest import run_backtest
from src.data.fetcher import load_candles
from src.live import get_orchestrator
from src.strategy_core import evaluate as evaluate_signal
from src.strategy_core.bias import compute_bias

from .state import get_chat_state

log = logging.getLogger(__name__)


# ── Tool-Definitionen ────────────────────────────────────────────────────


TOOLS: list[dict] = [
    {
        "name": "read_status",
        "description": (
            "Liefert den aktuellen Bot-Status: ob er läuft, Equity, P&L, offene Positionen, "
            "Anzahl geschlossener Trades, aktive Strategie-Parameter (rr_threshold, risk_pct), "
            "letzter Tick-Zeitpunkt. Benutze dies, wenn du wissen musst was der Bot gerade macht."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_recent_trades",
        "description": (
            "Liefert die letzten N geschlossenen Trades mit Entry/Exit/R-Multiple/PnL/Exit-Reason. "
            "Standard: 20 letzte. Optional Filter nach Instrument."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Anzahl Trades (Default 20, max 100)"},
                "instrument": {
                    "type": "string",
                    "enum": ["DE40", "NASDAQ", "SP500", "BTC"],
                    "description": "Filter auf ein Instrument (optional)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_signal",
        "description": (
            "Liefert das aktuelle Setup-Signal für ein Instrument (live, kein Cache). "
            "Zeigt Bias, Side, Entry/SL/TP/RR + welcher Variant (primary/ob_retest/fvg_retest/ultimate). "
            "Bei keinem Signal: erklärt warum (z.B. neutraler Bias, kein HTF-Sweep)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "instrument": {
                    "type": "string", "enum": ["DE40", "NASDAQ", "SP500", "BTC"],
                },
            },
            "required": ["instrument"],
        },
    },
    {
        "name": "diagnose",
        "description": (
            "Zeigt schrittweise warum aktuell kein Setup entsteht: Daily-Bias, HTF-Pivots, "
            "Liquidity-Levels, Sweeps im Lookback, Session-Status, LTF-BOS-Check. "
            "Benutze dies wenn der User fragt 'warum gibt es kein Signal'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "instrument": {
                    "type": "string", "enum": ["DE40", "NASDAQ", "SP500", "BTC"],
                },
            },
            "required": ["instrument"],
        },
    },
    {
        "name": "run_backtest",
        "description": (
            "Startet einen Backtest und gibt Metriken zurück (win_rate, total_return_pct, "
            "max_drawdown_pct, sharpe, expectancy_r, profit_factor, total_trades). "
            "Nur die wichtigsten Metriken landen im Context, kein Trade-Log (würde Token kosten). "
            "Dauert je nach bars 3-30 Sekunden."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "instrument": {
                    "type": "string", "enum": ["DE40", "NASDAQ", "SP500", "BTC"],
                },
                "iter_tf": {
                    "type": "string", "enum": ["1m", "5m", "15m", "30m", "1h"],
                    "description": "Replay-Auflösung (5m ist Default für ICT-Stack)",
                },
                "bars": {
                    "type": "integer",
                    "description": "Anzahl Bars (200-1000)",
                },
                "rr_threshold": {
                    "type": "number",
                    "description": "RR-Schwelle für Direct-Entry (Default 2.0)",
                },
                "risk_pct": {
                    "type": "number",
                    "description": "Risk pro Trade (z.B. 0.01 = 1%)",
                },
            },
            "required": ["instrument"],
        },
    },
    {
        "name": "propose_config_diff",
        "description": (
            "Schlägt eine Änderung der Bot-Strategie-Parameter vor. Schreibt NICHT direkt — "
            "die Änderung landet in der Proposal-Queue, der User muss im Dashboard auf 'Apply' "
            "klicken. Nutze dies wenn du nach Analyse einen konkreten Verbesserungsvorschlag hast."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "diff": {
                    "type": "object",
                    "description": (
                        "Key-Value-Diff. Erlaubte Keys: rr_threshold (float 1.0-5.0), "
                        "risk_pct (float 0.001-0.05), sweep_lookback (int 3-50), "
                        "tick_interval_s (int 10-600), instruments (list of DE40/NASDAQ/SP500/BTC)"
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": "Klare Begründung warum diese Änderung sinnvoll ist (max 500 Zeichen).",
                },
            },
            "required": ["diff", "rationale"],
        },
    },
]


# ── Dispatcher ───────────────────────────────────────────────────────────


async def dispatch_tool(name: str, input_data: dict, conversation_id: str | None = None) -> str:
    """Routet einen Tool-Call. Gibt einen JSON-String zurück (Anthropic erwartet string content)."""
    log.info("Tool-Call: %s(%s)", name, json.dumps(input_data)[:120])

    try:
        if name == "read_status":
            result = await _tool_read_status()
        elif name == "get_recent_trades":
            result = await _tool_get_recent_trades(input_data)
        elif name == "get_signal":
            result = await _tool_get_signal(input_data)
        elif name == "diagnose":
            result = await _tool_diagnose(input_data)
        elif name == "run_backtest":
            result = await _tool_run_backtest(input_data)
        elif name == "propose_config_diff":
            result = await _tool_propose_config_diff(input_data, conversation_id)
        else:
            result = {"error": f"unknown tool: {name}"}
    except Exception as e:
        log.exception("Tool %s gecrasht: %s", name, e)
        result = {"error": f"{type(e).__name__}: {e}"}

    text = json.dumps(result, default=str, ensure_ascii=False)
    if len(text) > 8000:
        log.warning("Tool %s output >8000 chars (%d) — truncated", name, len(text))
        text = text[:7900] + "\n...[truncated]"
    return text


async def _tool_read_status() -> dict:
    orch = get_orchestrator()
    s = orch.state
    return {
        "running": s.running,
        "equity": round(s.equity, 2),
        "initial_capital": s.initial_capital,
        "pnl_abs": round(s.equity - s.initial_capital, 2),
        "pnl_pct": round((s.equity - s.initial_capital) / s.initial_capital * 100, 2) if s.initial_capital else 0,
        "open_trades": [
            {
                "instrument": t.instrument, "side": t.side,
                "entry": t.entry, "sl": t.sl, "tp": t.tp, "size": t.size,
                "variant": t.variant, "rr_at_open": t.rr_at_open,
                "open_time": t.open_time.isoformat() if t.open_time else None,
            }
            for t in s.open_trades
        ],
        "n_closed": len(s.closed_trades),
        "wins_total": sum(1 for t in s.closed_trades if t.r_multiple > 0),
        "instruments": s.instruments,
        "rr_threshold": s.rr_threshold,
        "risk_pct": s.risk_pct,
        "sweep_lookback": s.sweep_lookback,
        "tick_interval_s": s.tick_interval_s,
        "last_tick": s.last_tick.isoformat() if s.last_tick else None,
        "last_signal_check": s.last_signal_check.isoformat() if s.last_signal_check else None,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "last_error": s.last_error,
    }


async def _tool_get_recent_trades(input_data: dict) -> dict:
    limit = min(int(input_data.get("limit", 20)), 100)
    instrument = input_data.get("instrument")
    orch = get_orchestrator()
    trades = orch.state.closed_trades
    if instrument:
        trades = [t for t in trades if t.instrument == instrument]
    trades = sorted(trades, key=lambda t: t.close_time, reverse=True)[:limit]
    if not trades:
        return {"trades": [], "note": f"keine Trades{' für ' + instrument if instrument else ''}"}
    wins = sum(1 for t in trades if t.r_multiple > 0)
    return {
        "n": len(trades),
        "win_rate": round(wins / len(trades), 4),
        "avg_r": round(sum(t.r_multiple for t in trades) / len(trades), 3),
        "trades": [
            {
                "instrument": t.instrument, "side": t.side,
                "entry": t.entry, "exit": t.exit,
                "r": round(t.r_multiple, 2),
                "pnl": round(t.pnl_abs, 2),
                "reason": t.exit_reason,
                "variant": t.variant,
                "open": t.open_time.isoformat() if t.open_time else None,
                "close": t.close_time.isoformat() if t.close_time else None,
            }
            for t in trades
        ],
    }


async def _tool_get_signal(input_data: dict) -> dict:
    inst = input_data["instrument"]
    tfs_needed = ["1d", "1h", "30m", "15m", "5m", "1m"]
    bars: dict[str, pd.DataFrame] = {}
    for tf in tfs_needed:
        try:
            df = await load_candles(inst, tf, bars=200)
            if not df.empty:
                bars[tf] = df
        except (CapitalAuthError, CapitalAPIError) as e:
            return {"error": f"{type(e).__name__}: {e}", "tf": tf}
    if "1d" not in bars:
        return {"error": "Daily-Daten nicht ladbar"}

    signal = evaluate_signal(inst, bars)
    bias = compute_bias(bars["1d"])
    if signal is None:
        return {
            "instrument": inst,
            "side": "none",
            "bias": bias.direction,
            "reason": (
                "neutraler Daily-Bias" if bias.direction == "neutral"
                else "kein HTF-Sweep + LTF-BOS im Lookback-Fenster"
            ),
        }
    return {
        "instrument": inst,
        "side": signal.side,
        "variant": signal.variant,
        "bias": signal.bias.direction,
        "entry": round(signal.entry, 4),
        "sl": round(signal.sl, 4),
        "tp": round(signal.tp, 4),
        "rr": round(signal.rr, 3),
        "htf_used": signal.htf_used,
        "ltf_used": signal.ltf_used,
        "sweep_time": signal.sweep.time.isoformat(),
        "sweep_direction": signal.sweep.direction,
        "has_ob": signal.ob is not None,
        "has_fvg": signal.fvg is not None,
    }


async def _tool_diagnose(input_data: dict) -> dict:
    """Wiederverwendet die /diagnose-Logik in komprimierter Form."""
    from src.strategy_core.liquidity import liquidity_levels
    from src.strategy_core.pivots import find_pivots
    from src.strategy_core.sessions import is_in_session, session_label
    from src.strategy_core.structure import find_bos_after
    from src.strategy_core.sweep import detect_sweeps

    inst = input_data["instrument"]
    bars: dict[str, pd.DataFrame] = {}
    for tf in ["1d", "1h", "30m", "15m", "5m", "1m"]:
        try:
            df = await load_candles(inst, tf, bars=200)
            if not df.empty:
                bars[tf] = df
        except Exception:
            pass
    if "1d" not in bars:
        return {"error": "Daily-Daten nicht verfügbar"}
    bias = compute_bias(bars["1d"])
    out: dict = {
        "instrument": inst,
        "bias": bias.direction,
        "session": session_label(pd.Timestamp.now(tz="UTC"), inst),
        "htf": {},
    }
    if bias.direction == "neutral":
        out["conclusion"] = "Bias neutral → kein Setup möglich (keine LQ-Suche)"
        return out
    for tf in ["1h", "30m", "15m"]:
        if tf not in bars:
            out["htf"][tf] = {"missing": True}
            continue
        df = bars[tf]
        pivots = find_pivots(df)
        levels = liquidity_levels(df, bias.direction)
        sweeps = detect_sweeps(df, levels, tf=tf, lookback=50)
        out["htf"][tf] = {
            "pivots": len(pivots),
            "lq_levels": len(levels),
            "sweeps_in_lookback": len(sweeps),
            "latest_sweep": (
                {
                    "time": sweeps[-1].time.isoformat(),
                    "level": round(sweeps[-1].level, 4),
                    "dir": sweeps[-1].direction,
                } if sweeps else None
            ),
            "in_session": is_in_session(df.index[-1], inst),
        }
    return out


async def _tool_run_backtest(input_data: dict) -> dict:
    inst = input_data["instrument"]
    iter_tf = input_data.get("iter_tf", "5m")
    bars_n = int(input_data.get("bars", 500))
    bars_n = max(200, min(bars_n, 1000))
    rr_threshold = float(input_data.get("rr_threshold", 2.0))
    risk_pct = float(input_data.get("risk_pct", 0.01))

    tfs_needed = list(dict.fromkeys(["1d", "1h", "30m", "15m", "5m", "1m", iter_tf]))
    bars: dict[str, pd.DataFrame] = {}
    for tf in tfs_needed:
        try:
            df = await load_candles(inst, tf, bars=bars_n)
            if not df.empty:
                bars[tf] = df
        except Exception as e:
            return {"error": f"fetch {tf}: {e}"}
    if "1d" not in bars or iter_tf not in bars:
        return {"error": "Pflicht-TFs (1d + iter_tf) nicht ladbar"}

    result = await run_backtest(
        instrument=inst, bars_full=bars,
        rr_threshold=rr_threshold, risk_pct_per_trade=risk_pct,
        iter_tf=iter_tf,
    )
    m = result.metrics
    return {
        "instrument": inst, "iter_tf": iter_tf,
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
        "metrics": {
            "total_trades": m.total_trades,
            "win_rate": round(m.win_rate, 4),
            "total_return_pct": round(m.total_return_pct, 2),
            "max_drawdown_pct": round(m.max_drawdown_pct, 2),
            "sharpe": round(m.sharpe, 2),
            "expectancy_r": round(m.expectancy_r, 3),
            "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else "inf",
            "avg_win_r": round(m.avg_win_r, 2),
            "avg_loss_r": round(m.avg_loss_r, 2),
            "longs": m.longs, "shorts": m.shorts,
        },
        "params_used": {
            "rr_threshold": rr_threshold, "risk_pct": risk_pct,
        },
    }


async def _tool_propose_config_diff(input_data: dict, conversation_id: str | None) -> dict:
    diff = input_data.get("diff", {})
    rationale = input_data.get("rationale", "")
    if not isinstance(diff, dict) or not diff:
        return {"error": "diff muss ein nicht-leeres dict sein"}
    # Whitelist enforcen, damit Claude nicht versucht etwas Verbotenes zu schreiben
    allowed = {"rr_threshold", "risk_pct", "sweep_lookback", "tick_interval_s", "instruments"}
    invalid = set(diff.keys()) - allowed
    if invalid:
        return {"error": f"unerlaubte Keys: {sorted(invalid)}. Erlaubt: {sorted(allowed)}"}
    state = get_chat_state()
    proposal = state.add_proposal(diff, rationale, conversation_id=conversation_id)
    return {
        "proposal_id": proposal.id,
        "status": "pending",
        "note": "Vorschlag in Proposal-Queue gespeichert. User muss im Dashboard auf 'Apply' klicken.",
        "diff": diff,
    }


# ── Tools mit cache_control für Prompt-Caching ──────────────────────────


def cached_tools() -> list[dict]:
    """Tools mit cache_control auf dem letzten Eintrag — caches die ganze Liste.

    Anthropic-Caching: cache_control gilt für alle Tools BIS UND MIT diesem Eintrag.
    Tools sind statisch → 1× generiert, Cache-Hits danach.
    """
    cloned = [dict(t) for t in TOOLS]
    cloned[-1] = {**cloned[-1], "cache_control": {"type": "ephemeral"}}
    return cloned
