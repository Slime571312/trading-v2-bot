"""BotOrchestrator — Lifecycle des Tick-Loops.

Ein einzelner Singleton-Orchestrator hält den State im Memory und verwaltet
die asyncio.Task. HTTP-Handler reden nur mit dem Orchestrator, nie direkt mit
State oder Task.
"""
from __future__ import annotations

import asyncio
import logging

from .loop import run_tick_loop
from .state import BotState, load_state, save_state, _now

log = logging.getLogger(__name__)


class BotOrchestrator:
    def __init__(self) -> None:
        self.state: BotState = load_state()
        self._task: asyncio.Task | None = None
        self._state_lock: asyncio.Lock = asyncio.Lock()

    # ── Status ──────────────────────────────────────────────────────────
    def is_running(self) -> bool:
        return self.state.running and self._task is not None and not self._task.done()

    # ── Start ───────────────────────────────────────────────────────────
    async def start(
        self,
        *,
        initial_capital: float | None = None,
        tick_interval_s: int | None = None,
        instruments: list[str] | None = None,
        rr_threshold: float | None = None,
        risk_pct: float | None = None,
        sweep_lookback: int | None = None,
        reset: bool = False,
    ) -> BotState:
        """Startet den Tick-Loop. Wenn er schon läuft: idempotent, gibt aktuellen State."""
        if self.is_running():
            log.info("Bot ist bereits running — start() ist no-op")
            return self.state

        async with self._state_lock:
            if reset:
                self.state.closed_trades.clear()
                self.state.open_trades.clear()
                self.state.equity = self.state.initial_capital
                self.state.last_error = None

            if initial_capital is not None:
                # Nur sinnvoll bei Reset (laufendes Equity sonst überschrieben)
                self.state.initial_capital = initial_capital
                if reset or self.state.equity == 0:
                    self.state.equity = initial_capital
            if tick_interval_s is not None:
                self.state.tick_interval_s = max(10, tick_interval_s)
            if instruments is not None:
                self.state.instruments = instruments
            if rr_threshold is not None:
                self.state.rr_threshold = rr_threshold
            if risk_pct is not None:
                self.state.risk_pct = risk_pct
            if sweep_lookback is not None:
                self.state.sweep_lookback = sweep_lookback

            self.state.running = True
            self.state.started_at = _now()
            self.state.stopped_at = None
            self.state.last_error = None
            save_state(self.state)

        self._task = asyncio.create_task(run_tick_loop(self.state, self._state_lock))
        log.info("Bot gestartet (instruments=%s, tick=%ds)",
                 self.state.instruments, self.state.tick_interval_s)
        return self.state

    # ── Stop ────────────────────────────────────────────────────────────
    async def stop(self) -> BotState:
        """Setzt running=False; der Loop beendet sich beim nächsten Iterations-Check."""
        if not self.state.running:
            return self.state

        async with self._state_lock:
            self.state.running = False
            self.state.stopped_at = _now()
            save_state(self.state)

        # Aktiv cancellieren falls noch im Sleep
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        log.info("Bot gestoppt — open_trades=%d, closed=%d",
                 len(self.state.open_trades), len(self.state.closed_trades))
        return self.state

    # ── Reset (force) ───────────────────────────────────────────────────
    async def reset(self) -> BotState:
        """Komplett-Reset: stoppt Bot (falls läuft), leert Trade-Listen, Equity = initial."""
        if self.is_running():
            await self.stop()
        async with self._state_lock:
            self.state.closed_trades.clear()
            self.state.open_trades.clear()
            self.state.equity = self.state.initial_capital
            self.state.last_error = None
            self.state.last_tick = None
            self.state.last_signal_check = None
            self.state.started_at = None
            self.state.stopped_at = None
            save_state(self.state)
        log.info("Bot-State zurückgesetzt")
        return self.state


# ── Singleton ───────────────────────────────────────────────────────────
_orchestrator: BotOrchestrator | None = None


def get_orchestrator() -> BotOrchestrator:
    """Lazy Singleton. FastAPI-Lifespan ruft das beim Startup auf."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = BotOrchestrator()
    return _orchestrator


async def shutdown_orchestrator() -> None:
    """Bei FastAPI-Shutdown: Bot stoppen + State persistieren."""
    global _orchestrator
    if _orchestrator is not None and _orchestrator.is_running():
        await _orchestrator.stop()
