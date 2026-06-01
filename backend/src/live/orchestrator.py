"""BotOrchestrator — Lifecycle für n Bot-Instanzen, jede in eigener Task.

Singleton hält State + Task-Dict. HTTP-Handler reden nur mit dem Orchestrator.
Pro Instrument eine eigene Task; Start/Stop/Reset sind pro-Instrument.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from .loop import run_tick_loop
from .state import (
    BotInstance, BotState, DEFAULT_INSTRUMENTS,
    load_state, save_state, _now,
)

log = logging.getLogger(__name__)


class BotOrchestrator:
    def __init__(self) -> None:
        self.state: BotState = load_state()
        self._tasks: dict[str, asyncio.Task] = {}
        # Ein globaler Lock — schützt save_state() vor Race; pro Instance
        # gibts kaum Cross-Mutationen weil jede Task nur ihre eigene Instance
        # anfasst, aber save_state() serialisiert ALLE Instanzen.
        self._state_lock: asyncio.Lock = asyncio.Lock()

    # ── Helpers ─────────────────────────────────────────────────────
    def is_running(self, instrument: str) -> bool:
        inst = self.state.get(instrument)
        if not inst:
            return False
        task = self._tasks.get(instrument)
        return inst.running and task is not None and not task.done()

    def any_running(self) -> bool:
        return any(self.is_running(i) for i in self.state.list_instruments())

    def list_instruments(self) -> list[str]:
        return self.state.list_instruments()

    def get_instance(self, instrument: str) -> BotInstance | None:
        return self.state.get(instrument)

    # ── Start ───────────────────────────────────────────────────────
    async def start(
        self,
        instrument: str,
        *,
        initial_capital: float | None = None,
        tick_interval_s: int | None = None,
        rr_threshold: float | None = None,
        risk_pct: float | None = None,
        sweep_lookback: int | None = None,
        reset: bool = False,
    ) -> BotInstance:
        """Startet eine einzelne Instanz. Idempotent wenn schon running."""
        if instrument not in self.state.instances:
            self.state.ensure(instrument)
        if self.is_running(instrument):
            log.info("Bot %s läuft bereits — no-op", instrument)
            return self.state.instances[instrument]

        async with self._state_lock:
            inst = self.state.instances[instrument]
            if reset:
                inst.closed_trades.clear()
                inst.open_trades.clear()
                inst.equity = inst.initial_capital
                inst.last_error = None
                inst.tick_log.clear()
            if initial_capital is not None:
                inst.initial_capital = initial_capital
                if reset or inst.equity == 0:
                    inst.equity = initial_capital
            if tick_interval_s is not None:
                inst.tick_interval_s = max(10, tick_interval_s)
            if rr_threshold is not None:
                inst.rr_threshold = rr_threshold
            if risk_pct is not None:
                inst.risk_pct = risk_pct
            if sweep_lookback is not None:
                inst.sweep_lookback = sweep_lookback

            inst.running = True
            inst.started_at = _now()
            inst.stopped_at = None
            inst.last_error = None
            save_state(self.state)

        task = asyncio.create_task(
            run_tick_loop(self.state, inst, self._state_lock),
            name=f"bot_loop_{instrument}",
        )
        self._tasks[instrument] = task
        log.info("Bot %s gestartet (rr=%.1f, sl=%d, risk=%.3f, tick=%ds)",
                 instrument, inst.rr_threshold, inst.sweep_lookback,
                 inst.risk_pct, inst.tick_interval_s)
        return inst

    async def start_many(self, instruments: Iterable[str], **opts) -> list[BotInstance]:
        results = []
        for inst in instruments:
            results.append(await self.start(inst, **opts))
        return results

    # ── Stop ────────────────────────────────────────────────────────
    async def stop(self, instrument: str) -> BotInstance:
        inst = self.state.instances.get(instrument)
        if not inst:
            raise KeyError(f"Instance {instrument} nicht vorhanden")
        if not inst.running:
            return inst

        async with self._state_lock:
            inst.running = False
            inst.stopped_at = _now()
            save_state(self.state)

        task = self._tasks.pop(instrument, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        log.info("Bot %s gestoppt (open=%d closed=%d)",
                 instrument, len(inst.open_trades), len(inst.closed_trades))
        return inst

    async def stop_all(self) -> list[BotInstance]:
        out = []
        for inst_name in list(self._tasks.keys()):
            out.append(await self.stop(inst_name))
        return out

    # ── Reset ───────────────────────────────────────────────────────
    async def reset(self, instrument: str) -> BotInstance:
        if self.is_running(instrument):
            await self.stop(instrument)
        async with self._state_lock:
            inst = self.state.ensure(instrument)
            inst.closed_trades.clear()
            inst.open_trades.clear()
            inst.equity = inst.initial_capital
            inst.last_error = None
            inst.tick_log.clear()
            inst.last_tick = None
            inst.last_signal_check = None
            inst.started_at = None
            inst.stopped_at = None
            save_state(self.state)
        log.info("Bot %s zurückgesetzt", instrument)
        return inst

    # ── GitHub Actions Sync ─────────────────────────────────────────
    def reload_from_file(self) -> None:
        """Lädt live_state.json neu — nur wenn keine Bots lokal laufen.

        Wird vom Background-Task aufgerufen um GH-Actions-Updates zu sehen
        ohne den lokalen Server neu zu starten.
        """
        if self.any_running():
            return  # lokale Bots aktiv — nicht überschreiben
        self.state = load_state()
        log.debug("State aus Datei neu geladen (GH Actions Sync)")

    # ── Config-Mutation (für Chat-Proposals) ────────────────────────
    async def apply_config_diff(self, instrument: str, diff: dict) -> BotInstance:
        """Wendet einen Param-Diff auf eine Instance an. Atomic + persistiert."""
        allowed = {"rr_threshold", "risk_pct", "sweep_lookback",
                   "tick_interval_s", "initial_capital"}
        async with self._state_lock:
            inst = self.state.ensure(instrument)
            for key, value in diff.items():
                if key not in allowed:
                    raise KeyError(f"Key '{key}' nicht in Apply-Whitelist {sorted(allowed)}")
                setattr(inst, key, value)
            save_state(self.state)
        log.info("Config %s ← %s", instrument, diff)
        return inst


# ── Singleton ───────────────────────────────────────────────────────────


_orchestrator: BotOrchestrator | None = None


def get_orchestrator() -> BotOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = BotOrchestrator()
    return _orchestrator


async def shutdown_orchestrator() -> None:
    global _orchestrator
    if _orchestrator is not None and _orchestrator.any_running():
        await _orchestrator.stop_all()
