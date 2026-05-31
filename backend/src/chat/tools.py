"""Bot-Tools für den Chat — als MCP-Server via Claude Agent SDK.

Tools werden mit `@tool` decoriert und in einem In-Memory-MCP-Server verpackt
(`create_sdk_mcp_server`). Die SDK ruft sie via Claude Code CLI auf — keine
direkte Anthropic-API-Verbindung, läuft über die Claude-Code-Subscription des
User.

Der conversation_id-Kontext (für propose_config_diff) wird via Closure
gebunden: `build_bot_mcp_server(conversation_id)` erzeugt eine frische
Server-Instanz pro Chat-Turn.

Spec: Trading/Bot/Chat-Engine.md + https://code.claude.com/docs/en/agent-sdk/python
"""
from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd
from claude_agent_sdk import create_sdk_mcp_server, tool

from src.backtest import run_backtest
from src.brokers.capital_com import CapitalAPIError, CapitalAuthError
from src.data.fetcher import load_candles
from src.live import get_orchestrator
from src.strategy_core import evaluate as evaluate_signal
from src.strategy_core.bias import compute_bias

from .state import get_chat_state

log = logging.getLogger(__name__)

MCP_SERVER_NAME = "bot_tools"

# Tool-Namen (für allowed_tools-Whitelist + Frontend-Display)
TOOL_NAMES = [
    "read_status",
    "get_recent_trades",
    "get_signal",
    "diagnose",
    "run_backtest",
    "propose_config_diff",
]

# Voll qualifizierte MCP-Tool-Namen: mcp__<server>__<tool>
ALLOWED_TOOLS = [f"mcp__{MCP_SERVER_NAME}__{name}" for name in TOOL_NAMES]


def _text(payload: dict | str) -> dict:
    """MCP-Tool-Result-Format: {content: [{type:'text', text:...}]}."""
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str, ensure_ascii=False)
    if len(text) > 8000:
        log.warning("Tool output >8000 chars (%d) — truncated", len(text))
        text = text[:7900] + "\n...[truncated]"
    return {"content": [{"type": "text", "text": text}]}


# ── Tool-Implementierungen (async, side-effect-isoliert) ────────────────


async def _impl_read_status(_: dict[str, Any]) -> dict:
    """Snapshot aller Bot-Instanzen (Multi-Bot): pro Instrument running, equity, params."""
    orch = get_orchestrator()
    instances = []
    total_equity = 0.0
    total_initial = 0.0
    total_open = 0
    total_closed = 0
    total_wins = 0
    for inst_name, inst in orch.state.instances.items():
        wins = sum(1 for t in inst.closed_trades if t.r_multiple > 0)
        total_equity += inst.equity
        total_initial += inst.initial_capital
        total_open += len(inst.open_trades)
        total_closed += len(inst.closed_trades)
        total_wins += wins
        instances.append({
            "instrument": inst_name,
            "running": inst.running,
            "equity": round(inst.equity, 2),
            "initial_capital": inst.initial_capital,
            "pnl_abs": round(inst.equity - inst.initial_capital, 2),
            "pnl_pct": round((inst.equity - inst.initial_capital) / inst.initial_capital * 100, 2)
                       if inst.initial_capital else 0,
            "rr_threshold": inst.rr_threshold,
            "risk_pct": inst.risk_pct,
            "sweep_lookback": inst.sweep_lookback,
            "tick_interval_s": inst.tick_interval_s,
            "n_open": len(inst.open_trades),
            "n_closed": len(inst.closed_trades),
            "wins": wins,
            "last_tick": inst.last_tick.isoformat() if inst.last_tick else None,
            "last_signal_check": inst.last_signal_check.isoformat() if inst.last_signal_check else None,
            "last_error": inst.last_error,
            "open_trades": [
                {
                    "side": t.side, "entry": t.entry, "sl": t.sl, "tp": t.tp,
                    "variant": t.variant, "rr_at_open": t.rr_at_open,
                    "open_time": t.open_time.isoformat() if t.open_time else None,
                }
                for t in inst.open_trades
            ],
        })
    return _text({
        "instances": instances,
        "totals": {
            "equity": round(total_equity, 2),
            "initial_capital": round(total_initial, 2),
            "pnl_abs": round(total_equity - total_initial, 2),
            "n_open_trades": total_open,
            "n_closed_trades": total_closed,
            "wins": total_wins,
            "overall_win_rate": round(total_wins / total_closed, 4) if total_closed else None,
        },
    })


async def _impl_get_recent_trades(args: dict[str, Any]) -> dict:
    limit = min(int(args.get("limit", 20)), 100)
    instrument = args.get("instrument")
    orch = get_orchestrator()
    all_trades = []
    for inst_name, inst in orch.state.instances.items():
        if instrument and inst_name != instrument:
            continue
        all_trades.extend(inst.closed_trades)
    trades = sorted(all_trades, key=lambda t: t.close_time, reverse=True)[:limit]
    if not trades:
        return _text({"trades": [], "note": f"keine Trades{' für ' + instrument if instrument else ''}"})
    wins = sum(1 for t in trades if t.r_multiple > 0)
    payload = {
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
    return _text(payload)


async def _impl_get_signal(args: dict[str, Any]) -> dict:
    inst = args["instrument"]
    tfs_needed = ["1d", "1h", "30m", "15m", "5m", "1m"]
    bars: dict[str, pd.DataFrame] = {}
    for tf in tfs_needed:
        try:
            df = await load_candles(inst, tf, bars=200)
            if not df.empty:
                bars[tf] = df
        except (CapitalAuthError, CapitalAPIError) as e:
            return _text({"error": f"{type(e).__name__}: {e}", "tf": tf})
    if "1d" not in bars:
        return _text({"error": "Daily-Daten nicht ladbar"})

    signal = evaluate_signal(inst, bars)
    bias = compute_bias(bars["1d"])
    if signal is None:
        return _text({
            "instrument": inst,
            "side": "none",
            "bias": bias.direction,
            "reason": (
                "neutraler Daily-Bias" if bias.direction == "neutral"
                else "kein HTF-Sweep + LTF-BOS im Lookback-Fenster"
            ),
        })
    return _text({
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
    })


async def _impl_diagnose(args: dict[str, Any]) -> dict:
    from src.strategy_core.liquidity import liquidity_levels
    from src.strategy_core.pivots import find_pivots
    from src.strategy_core.sessions import is_in_session, session_label
    from src.strategy_core.sweep import detect_sweeps

    inst = args["instrument"]
    bars: dict[str, pd.DataFrame] = {}
    for tf in ["1d", "1h", "30m", "15m", "5m", "1m"]:
        try:
            df = await load_candles(inst, tf, bars=200)
            if not df.empty:
                bars[tf] = df
        except Exception:
            pass
    if "1d" not in bars:
        return _text({"error": "Daily-Daten nicht verfügbar"})
    bias = compute_bias(bars["1d"])
    out: dict = {
        "instrument": inst,
        "bias": bias.direction,
        "session": session_label(pd.Timestamp.now(tz="UTC"), inst),
        "htf": {},
    }
    if bias.direction == "neutral":
        out["conclusion"] = "Bias neutral → kein Setup möglich (keine LQ-Suche)"
        return _text(out)
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
    return _text(out)


async def _impl_run_backtest(args: dict[str, Any]) -> dict:
    inst = args["instrument"]
    iter_tf = args.get("iter_tf", "5m")
    bars_n = int(args.get("bars", 500))
    bars_n = max(200, min(bars_n, 1000))
    rr_threshold = float(args.get("rr_threshold", 2.0))
    risk_pct = float(args.get("risk_pct", 0.01))

    tfs_needed = list(dict.fromkeys(["1d", "1h", "30m", "15m", "5m", "1m", iter_tf]))
    bars: dict[str, pd.DataFrame] = {}
    for tf in tfs_needed:
        try:
            df = await load_candles(inst, tf, bars=bars_n)
            if not df.empty:
                bars[tf] = df
        except Exception as e:
            return _text({"error": f"fetch {tf}: {e}"})
    if "1d" not in bars or iter_tf not in bars:
        return _text({"error": "Pflicht-TFs (1d + iter_tf) nicht ladbar"})

    result = await run_backtest(
        instrument=inst, bars_full=bars,
        rr_threshold=rr_threshold, risk_pct_per_trade=risk_pct,
        iter_tf=iter_tf,
    )
    m = result.metrics
    return _text({
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
        "params_used": {"rr_threshold": rr_threshold, "risk_pct": risk_pct},
    })


def _impl_propose_config_diff_factory(conversation_id: str | None):
    """Closure: bindet die conversation_id an den Proposal-Eintrag."""
    async def _impl(args: dict[str, Any]) -> dict:
        diff = args.get("diff", {})
        rationale = args.get("rationale", "")
        if not isinstance(diff, dict) or not diff:
            return _text({"error": "diff muss ein nicht-leeres dict sein"})
        allowed = {"rr_threshold", "risk_pct", "sweep_lookback", "tick_interval_s", "instruments"}
        invalid = set(diff.keys()) - allowed
        if invalid:
            return _text({"error": f"unerlaubte Keys: {sorted(invalid)}. Erlaubt: {sorted(allowed)}"})
        state = get_chat_state()
        proposal = state.add_proposal(diff, rationale, conversation_id=conversation_id)
        return _text({
            "proposal_id": proposal.id,
            "status": "pending",
            "note": "Vorschlag in Proposal-Queue gespeichert. User muss im Dashboard auf 'Apply' klicken.",
            "diff": diff,
        })
    return _impl


# ── MCP-Server-Factory ──────────────────────────────────────────────────


def build_bot_mcp_server(conversation_id: str | None = None):
    """Baut einen frischen MCP-Server pro Chat-Turn (conversation_id-Closure)."""

    @tool("read_status",
          "Liefert den aktuellen Bot-Status: ob er läuft, Equity, P&L, offene Positionen, "
          "Anzahl geschlossener Trades, aktive Strategie-Parameter (rr_threshold, risk_pct), "
          "letzter Tick-Zeitpunkt. Benutze dies, wenn du wissen musst was der Bot gerade macht.",
          {})
    async def read_status(args):
        return await _impl_read_status(args)

    @tool("get_recent_trades",
          "Liefert die letzten N geschlossenen Trades mit Entry/Exit/R-Multiple/PnL/Exit-Reason. "
          "Standard: 20 letzte. Optional Filter nach Instrument.",
          {"limit": int, "instrument": str})
    async def get_recent_trades(args):
        return await _impl_get_recent_trades(args)

    @tool("get_signal",
          "Liefert das aktuelle Setup-Signal für ein Instrument (live, kein Cache). "
          "Zeigt Bias, Side, Entry/SL/TP/RR + Variant (primary/ob_retest/fvg_retest/ultimate). "
          "Bei keinem Signal: erklärt warum (z.B. neutraler Bias, kein HTF-Sweep). "
          "Erlaubte Werte für instrument: DE40, NASDAQ, SP500, BTC.",
          {"instrument": str})
    async def get_signal(args):
        return await _impl_get_signal(args)

    @tool("diagnose",
          "Zeigt schrittweise warum aktuell kein Setup entsteht: Daily-Bias, HTF-Pivots, "
          "Liquidity-Levels, Sweeps im Lookback, Session-Status, LTF-BOS-Check. "
          "Benutze dies wenn der User fragt 'warum gibt es kein Signal'. "
          "Erlaubte Werte für instrument: DE40, NASDAQ, SP500, BTC.",
          {"instrument": str})
    async def diagnose(args):
        return await _impl_diagnose(args)

    @tool("run_backtest",
          "Startet einen Backtest und gibt Metriken zurück (win_rate, total_return_pct, "
          "max_drawdown_pct, sharpe, expectancy_r, profit_factor, total_trades). "
          "Nur die wichtigsten Metriken landen im Context, kein Trade-Log. "
          "Dauert je nach bars 3-30 Sekunden. "
          "Erlaubte Werte: instrument (DE40/NASDAQ/SP500/BTC), iter_tf (1m/5m/15m/30m/1h), "
          "bars (200-1000), rr_threshold (1.0-5.0), risk_pct (0.001-0.05).",
          {"instrument": str, "iter_tf": str, "bars": int,
           "rr_threshold": float, "risk_pct": float})
    async def run_backtest_tool(args):
        return await _impl_run_backtest(args)

    propose_impl = _impl_propose_config_diff_factory(conversation_id)

    @tool("propose_config_diff",
          "Schlägt eine Änderung der Bot-Strategie-Parameter vor. Schreibt NICHT direkt — "
          "die Änderung landet in der Proposal-Queue, der User muss im Dashboard auf 'Apply' "
          "klicken. Nutze dies wenn du nach Analyse einen konkreten Verbesserungsvorschlag hast. "
          "Erlaubte Keys in diff: rr_threshold (float 1.0-5.0), risk_pct (float 0.001-0.05), "
          "sweep_lookback (int 3-50), tick_interval_s (int 10-600), instruments (list). "
          "rationale ist eine klare Begründung (max 500 Zeichen).",
          {"diff": dict, "rationale": str})
    async def propose_config_diff(args):
        return await propose_impl(args)

    return create_sdk_mcp_server(
        name=MCP_SERVER_NAME,
        version="1.0.0",
        tools=[
            read_status, get_recent_trades, get_signal,
            diagnose, run_backtest_tool, propose_config_diff,
        ],
    )
