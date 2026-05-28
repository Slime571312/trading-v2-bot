"""ChatClient — Anthropic-API-Wrapper mit Multi-Turn-Tool-Loop + Prompt-Caching.

Default-Modell: Sonnet 4.6 (schneller Chat).
Schweres Reasoning: Opus 4.7 (langsamer, in Etappe 7 für Nightly-Tuner).

Prompt-Caching: System-Prompt + Tools-Liste werden mit `cache_control` markiert.
Anthropic cacht alles bis zum letzten cached Element. Cache-TTL 5 Minuten.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import anthropic

from src.config import config

from .tools import cached_tools, dispatch_tool

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 10  # Guard gegen Endlos-Loop (siehe Chat-Engine.md Pitfalls)
MAX_TOKENS_OUT = 4096
ALLOWED_MODELS = {"claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"}

SYSTEM_PROMPT = """Du bist der AI-Assistent für Bot v2 — einen ICT-Multi-TF-Trading-Bot \
auf Capital.com (DE40, NASDAQ, SP500, BTC). Du antwortest auf Deutsch in einem direkten, \
sachlichen Ton ohne unnötige Höflichkeitsfloskeln.

# Deine Werkzeuge

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


def _default_model() -> str:
    m = config.chat_default_model
    if m not in ALLOWED_MODELS:
        log.warning("Unbekanntes chat_default_model '%s' — fallback claude-sonnet-4-6", m)
        return "claude-sonnet-4-6"
    return m


@dataclass(slots=True)
class ToolCallRecord:
    """Eintrag pro Tool-Aufruf für UI-Rendering."""

    name: str
    input: dict
    output: str
    iteration: int


@dataclass(slots=True)
class ChatTurnResult:
    """Output eines Chat-Turns: Antwort-Text + Tool-Calls + Usage."""

    text: str
    tool_calls: list[ToolCallRecord]
    iterations: int
    new_messages: list[dict]  # nur die NEU dazugekommenen Messages (für append)
    model_used: str
    usage: dict[str, int]


class ChatClient:
    """Thin Wrapper um anthropic.AsyncAnthropic + Multi-Turn-Tool-Loop."""

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or config.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY fehlt — setze ihn in .env oder als Umgebungsvariable"
            )
        self._client = anthropic.AsyncAnthropic(api_key=key)

    async def run_turn(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        conversation_id: str | None = None,
    ) -> ChatTurnResult:
        """Führt einen vollständigen User-Turn aus inkl. Multi-Step-Tool-Loop.

        `messages` enthält die History (inkl. der neuen User-Nachricht am Ende).
        Returnt das Ergebnis und die neu erzeugten Messages, damit der Caller
        sie an die History anhängen kann.

        Tool-Schleifen-Guard: nach MAX_TOOL_ITERATIONS wird abgebrochen und ein
        Fehler in die Antwort geschrieben (Schutz vor Endlos-Loop wenn ein Tool
        immer wieder fehlschlägt und Claude es immer wieder versucht).
        """
        chosen_model = model if model in ALLOWED_MODELS else _default_model()
        tools = cached_tools()
        system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

        working_messages = list(messages)  # kopie, original nicht mutieren
        tool_records: list[ToolCallRecord] = []
        new_messages: list[dict] = []
        usage_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

        for iteration in range(MAX_TOOL_ITERATIONS):
            log.info("chat iter %d (model=%s, msgs=%d)", iteration, chosen_model, len(working_messages))
            response = await self._client.messages.create(
                model=chosen_model,
                max_tokens=MAX_TOKENS_OUT,
                tools=tools,
                system=system,
                messages=working_messages,
            )

            for k in usage_totals:
                usage_totals[k] += getattr(response.usage, k, 0) or 0

            # Assistant-Antwort speichern (raw content blocks)
            assistant_msg = {
                "role": "assistant",
                "content": [_block_to_dict(b) for b in response.content],
            }
            working_messages.append(assistant_msg)
            new_messages.append(assistant_msg)

            if response.stop_reason == "end_turn":
                final_text = _extract_text(response.content) or "(keine Text-Antwort)"
                return ChatTurnResult(
                    text=final_text,
                    tool_calls=tool_records,
                    iterations=iteration + 1,
                    new_messages=new_messages,
                    model_used=chosen_model,
                    usage=usage_totals,
                )

            if response.stop_reason != "tool_use":
                # Unerwartet (z.B. max_tokens hit) — letzten Text liefern, raus
                final_text = _extract_text(response.content) or (
                    f"(stop_reason={response.stop_reason} ohne text)"
                )
                return ChatTurnResult(
                    text=final_text,
                    tool_calls=tool_records,
                    iterations=iteration + 1,
                    new_messages=new_messages,
                    model_used=chosen_model,
                    usage=usage_totals,
                )

            # Tool-Calls extrahieren + parallel dispatchen
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results: list[dict] = []
            for block in tool_use_blocks:
                output_text = await dispatch_tool(
                    block.name, dict(block.input), conversation_id=conversation_id,
                )
                tool_records.append(ToolCallRecord(
                    name=block.name, input=dict(block.input),
                    output=output_text, iteration=iteration,
                ))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output_text,
                })

            user_msg = {"role": "user", "content": tool_results}
            working_messages.append(user_msg)
            new_messages.append(user_msg)

        # MAX_TOOL_ITERATIONS exceeded
        log.warning("chat: MAX_TOOL_ITERATIONS (%d) erreicht — Loop abgebrochen", MAX_TOOL_ITERATIONS)
        return ChatTurnResult(
            text=(
                f"⚠️ Tool-Loop-Limit ({MAX_TOOL_ITERATIONS} Iterationen) erreicht. "
                "Das Modell scheint in einer Endlos-Schleife festzuhängen."
            ),
            tool_calls=tool_records,
            iterations=MAX_TOOL_ITERATIONS,
            new_messages=new_messages,
            model_used=chosen_model,
            usage=usage_totals,
        )


# ── Helpers ──────────────────────────────────────────────────────────────


def _block_to_dict(block: Any) -> dict:
    """Convert anthropic SDK content block to a plain dict for serialization."""
    if hasattr(block, "model_dump"):
        return block.model_dump()
    if hasattr(block, "dict"):
        return block.dict()
    # Fallback: best-effort
    base = {"type": getattr(block, "type", "unknown")}
    for attr in ("text", "id", "name", "input"):
        if hasattr(block, attr):
            base[attr] = getattr(block, attr)
    return base


def _extract_text(content: list) -> str:
    parts = []
    for b in content:
        if hasattr(b, "text") and getattr(b, "text", None):
            parts.append(b.text)
    return "\n".join(parts).strip()


# ── Singleton ────────────────────────────────────────────────────────────


_singleton: ChatClient | None = None


def get_chat_client() -> ChatClient:
    global _singleton
    if _singleton is None:
        _singleton = ChatClient()
    return _singleton
