"""ChatClient via Claude Agent SDK — nutzt die Claude-Code-Subscription des
Users, KEIN ANTHROPIC_API_KEY nötig.

Pro Turn:
  1. Frischer MCP-Server (mit conversation_id-Closure für propose_config_diff)
  2. `query()` mit system_prompt=BOT-Kontext, tools=[] (Claude-Codes builtin
     deaktiviert), mcp_servers=unsere Tools, permission_mode=bypass
  3. Async-Stream parsen: TextBlock → Antwort, ToolUseBlock → Aufruf-Records,
     ResultMessage → session_id für nächsten Turn
  4. Returnt ChatTurnResult mit text + tool_calls + neue session_id

Multi-Turn: Conversation hält `session_id` — wir resumen damit beim nächsten
Turn. Claude Code verwaltet den eigentlichen Conversation-Context.

Spec: Trading/Bot/Chat-Engine.md + https://code.claude.com/docs/en/agent-sdk/python
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)

from .state import ChatTurn, ToolCallRecord
from .tools import ALLOWED_TOOLS, MCP_SERVER_NAME, build_bot_mcp_server

log = logging.getLogger(__name__)

# Diese Modelle sind die typischen Claude-Code-Plan-Optionen. None = Default
# der Subscription (meist Sonnet).
ALLOWED_MODELS = {None, "sonnet", "opus", "haiku",
                  "claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"}

MAX_TURNS = 12  # Guard gegen Endlos-Tool-Loops

SYSTEM_PROMPT = """Du bist der AI-Assistent für Bot v2 — einen ICT-Multi-TF-Trading-Bot \
auf Capital.com (DE40, NASDAQ, SP500, BTC). Du antwortest auf Deutsch in einem direkten, \
sachlichen Ton ohne unnötige Höflichkeitsfloskeln.

# Deine Werkzeuge (alle als mcp__bot_tools__<name>)

- `read_status` — Bot-Status, Equity, offene Positionen, aktive Strategie-Parameter
- `get_recent_trades` — letzte N geschlossene Trades (mit Win/Loss, R-Multiples)
- `get_signal` — aktuelles Setup für ein Instrument (live, nicht gecacht)
- `diagnose` — warum entsteht aktuell KEIN Setup (Bias, Pivots, Sweeps, Session)
- `run_backtest` — Backtest mit aktuellen oder hypothetischen Params (Metriken zurück)
- `propose_config_diff` — Konfigurations-Änderung VORSCHLAGEN (KEIN direktes Schreiben)

# Scope-Grenzen (HART)

✅ Lesen, Erklären, Analysieren, Vorschläge machen
❌ Du wendest NIEMALS Config-Änderungen selbst an. Vorschläge gehen in eine Queue, der User \
klickt im Dashboard auf 'Apply' oder 'Reject'.
❌ Du startest/stoppst NICHT den Bot selbständig. Wenn der User das will, sag ihm wo der \
Start/Stop-Button ist (`/live`-Tab).
❌ Du nutzt KEINE Bash/Read/Write/Edit-Tools von Claude Code. NUR die mcp__bot_tools__*-Tools.

# Strategie-Kontext (kurz)

Der Bot folgt ICT Multi-TF-Stacking:
1. Daily-Bias (long/short/neutral aus letztem Daily-BoS)
2. HTF-Liquidity (1h/30m/15m — höchster gewinnt)
3. HTF-Sweep (Wick durch Level, Body kehrt zurück)
4. LTF-BOS (5m oder 1m, in Bias-Richtung)
5. RR-Check: ≥ 2:1 → Direct-Entry, sonst Retracement
6. Retracement: OB > FVG, optional EQ-Boost = 'ultimate'

Risk: 1% pro Trade (Default), SL exakt = sweep-wick + Spread.
Sessions: DE40/NASDAQ/SP500 nur London + NY-Open, BTC 24/7.

# Antwort-Stil

- Tool-Calls machen, BEVOR du antwortest, wenn der User nach Daten fragt
- Konkrete Zahlen statt Pauschalaussagen
- Bei `propose_config_diff`: erst Daten via `run_backtest` validieren, dann vorschlagen
- Keine emojis, kein "Gerne!" — direkt zur Sache"""


@dataclass(slots=True)
class ChatTurnResult:
    """Output eines Chat-Turns: Antwort-Text + Tool-Calls + Session-ID + Cost."""

    text: str
    tool_calls: list[ToolCallRecord]
    session_id: str | None
    num_turns: int
    cost_usd: float | None
    duration_ms: int | None
    model_used: str | None


def _normalize_model(model: str | None) -> str | None:
    """Akzeptiert auch die alten claude-X-Y-Z-Strings und mapped sie auf die
    Kurz-Aliase die die Agent-SDK/CLI versteht."""
    if model is None:
        return None
    if model in {"sonnet", "opus", "haiku"}:
        return model
    # Mapping der vollen Modellnamen aus Etappe 6 auf SDK-Aliase
    if "opus" in model.lower():
        return "opus"
    if "haiku" in model.lower():
        return "haiku"
    if "sonnet" in model.lower():
        return "sonnet"
    log.warning("Unbekanntes Modell '%s' — fallback default", model)
    return None


async def run_turn(
    *,
    user_message: str,
    conversation_id: str,
    session_id: str | None,
    model: str | None,
) -> ChatTurnResult:
    """Führt einen Chat-Turn aus.

    `session_id` ist die Claude-Code-Session der Conversation (None = neue Session).
    Returnt session_id im Result — sollte mit `session_id` identisch sein außer wenn
    Claude Code intern forkt.
    """
    mcp_server = build_bot_mcp_server(conversation_id=conversation_id)

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        # WICHTIG: tools=[] deaktiviert Claude-Codes builtin Read/Bash/Edit/etc.
        # Nur unsere MCP-Tools sind erlaubt.
        tools=[],
        allowed_tools=ALLOWED_TOOLS,
        mcp_servers={MCP_SERVER_NAME: mcp_server},
        permission_mode="bypassPermissions",  # unsere Tools sind read-only + Proposal-only
        max_turns=MAX_TURNS,
        resume=session_id,
        model=_normalize_model(model),
        cwd=None,  # nicht relevant — unsere Tools brauchen keinen FS-Zugriff
    )

    text_parts: list[str] = []
    tool_records: list[ToolCallRecord] = []
    pending_tool_inputs: dict[str, tuple[str, dict]] = {}
    result_session_id: str | None = None
    num_turns: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None
    model_used: str | None = None

    log.info("Chat-Turn: conv=%s session=%s model=%s", conversation_id, session_id, model)

    async for msg in query(prompt=user_message, options=options):
        if isinstance(msg, AssistantMessage):
            model_used = msg.model
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    pending_tool_inputs[block.id] = (block.name, dict(block.input))
                elif isinstance(block, ToolResultBlock):
                    # Tool-Result kann auch in AssistantMessage stehen (selten)
                    _consume_tool_result(block, pending_tool_inputs, tool_records)
        elif isinstance(msg, ResultMessage):
            result_session_id = msg.session_id
            num_turns = msg.num_turns
            cost_usd = msg.total_cost_usd
            duration_ms = msg.duration_ms
        else:
            # UserMessage (mit ToolResultBlocks), SystemMessage etc.
            # ToolResults kommen meist hier als UserMessage(content=[ToolResultBlock])
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        _consume_tool_result(block, pending_tool_inputs, tool_records)

    # Falls noch tool_use ohne matching tool_result (z.B. Stream abgebrochen)
    for tool_id, (name, inp) in pending_tool_inputs.items():
        tool_records.append(ToolCallRecord(
            name=_short_name(name), input=inp, output="(kein Result-Block empfangen)",
        ))

    final_text = "\n".join(text_parts).strip() or "(keine Text-Antwort)"

    return ChatTurnResult(
        text=final_text,
        tool_calls=tool_records,
        session_id=result_session_id or session_id,
        num_turns=num_turns,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        model_used=model_used,
    )


def _consume_tool_result(
    block: "ToolResultBlock",
    pending: dict[str, tuple[str, dict]],
    records: list[ToolCallRecord],
) -> None:
    """Matched ein ToolResultBlock gegen pending Tool-Use-Inputs und appended Record."""
    name, inp = pending.pop(block.tool_use_id, ("unknown", {}))
    content = block.content
    # content kann string sein oder list[{type:'text', text:'...'}, ...]
    if isinstance(content, str):
        output = content
    elif isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif hasattr(c, "text"):
                parts.append(c.text)
        output = "\n".join(parts)
    else:
        output = str(content)
    records.append(ToolCallRecord(
        name=_short_name(name), input=inp, output=output,
    ))


def _short_name(full_name: str) -> str:
    """mcp__bot_tools__read_status → read_status (UI-freundlich)."""
    if full_name.startswith(f"mcp__{MCP_SERVER_NAME}__"):
        return full_name[len(f"mcp__{MCP_SERVER_NAME}__"):]
    return full_name


# ── Singleton-Compat-Layer (Etappe 6 API beibehalten) ──────────────────


class ChatClient:
    """Thin Wrapper-Klasse — existiert nur damit api/main.py die alte API
    weiterverwenden kann. Die eigentliche Logik liegt in `run_turn()`."""

    async def run_turn(
        self,
        *,
        user_message: str,
        conversation_id: str,
        session_id: str | None,
        model: str | None,
    ) -> ChatTurnResult:
        return await run_turn(
            user_message=user_message,
            conversation_id=conversation_id,
            session_id=session_id,
            model=model,
        )


_singleton: ChatClient | None = None


def get_chat_client() -> ChatClient:
    global _singleton
    if _singleton is None:
        _singleton = ChatClient()
    return _singleton


async def is_available() -> bool:
    """Quick-Check: ist `claude` CLI installiert + lauffähig? Wird vom /health
    und vom /chat/message Endpoint genutzt um sauber 503 zu liefern wenn nicht."""
    import shutil
    return shutil.which("claude") is not None
