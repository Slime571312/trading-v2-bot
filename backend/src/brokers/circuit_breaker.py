"""Circuit Breaker für Capital.com — verhindert Self-DoS bei Outages.

Spec: Vault/Bot/Resilience-Patterns.md.

Drei Zustände:
- **CLOSED** — Calls gehen durch, Failures werden gezählt
- **OPEN** — Calls fail SOFORT ohne HTTP-Roundtrip (CircuitOpenError),
            Cool-off-Phase läuft (Default 60s)
- **HALF_OPEN** — Nach Cool-off: 1 Probe-Call, success → CLOSED, fail → wieder OPEN

Ohne Circuit Breaker: bei einer 10-Min-Maintenance von Capital floodet
`live/loop.py` die API mit Tausenden 5xx-Calls, eskaliert Rate-Limits und
übersieht den Trade-Ausfall bis morgens beim Dashboard-Check.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised wenn ein Call zum Circuit kommt der gerade OPEN ist."""

    def __init__(self, name: str, reopens_in_s: float):
        super().__init__(f"Circuit '{name}' offen — Wiedereröffnung in {reopens_in_s:.1f}s")
        self.name = name
        self.reopens_in_s = reopens_in_s


@dataclass
class CircuitBreaker:
    """Async-sicherer Circuit Breaker.

    `failure_threshold` Fehler innerhalb `failure_window_s` → OPEN. Nach
    `cool_off_s` → HALF_OPEN: nächster Call ist Probe. Bei success → CLOSED.

    Threadsafe-ish via asyncio.Lock — alle Tick-Loops teilen sich einen Breaker
    pro Broker, der Lock serialisiert State-Übergänge.
    """
    name: str = "capital_com"
    failure_threshold: int = 5
    failure_window_s: float = 60.0
    cool_off_s: float = 60.0
    # interner State
    state: State = State.CLOSED
    failure_times: list[float] = field(default_factory=list)
    opened_at: float | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def before_call(self) -> None:
        """Vor jedem Call aufrufen. Raises CircuitOpenError wenn OPEN.

        OPEN → bei abgelaufener Cool-off automatisch HALF_OPEN.
        """
        async with self._lock:
            if self.state == State.OPEN:
                assert self.opened_at is not None
                elapsed = time.monotonic() - self.opened_at
                if elapsed >= self.cool_off_s:
                    log.info("Circuit '%s' → HALF_OPEN (probe)", self.name)
                    self.state = State.HALF_OPEN
                else:
                    raise CircuitOpenError(self.name, self.cool_off_s - elapsed)
            # CLOSED/HALF_OPEN → Call darf durch

    async def on_success(self) -> None:
        """Nach erfolgreichem Call aufrufen."""
        async with self._lock:
            if self.state == State.HALF_OPEN:
                log.info("Circuit '%s' → CLOSED (probe ok, %d Failures geklärt)",
                         self.name, len(self.failure_times))
                self.state = State.CLOSED
                self.opened_at = None
                self.failure_times.clear()
            elif self.state == State.CLOSED and self.failure_times:
                # Auch im CLOSED-State Failures verfallen lassen wenn lange her
                now = time.monotonic()
                self.failure_times = [
                    t for t in self.failure_times if now - t < self.failure_window_s
                ]

    async def on_failure(self) -> None:
        """Nach gescheitertem Call aufrufen. Kann State auf OPEN setzen."""
        async with self._lock:
            now = time.monotonic()
            if self.state == State.HALF_OPEN:
                # Probe gescheitert → wieder OPEN
                log.warning("Circuit '%s' → OPEN (probe fail)", self.name)
                self.state = State.OPEN
                self.opened_at = now
                return
            # CLOSED: Failure-Window updaten
            self.failure_times.append(now)
            self.failure_times = [
                t for t in self.failure_times if now - t < self.failure_window_s
            ]
            if len(self.failure_times) >= self.failure_threshold:
                log.warning(
                    "Circuit '%s' → OPEN (%d Fehler in %.0fs, Cool-off %ds)",
                    self.name, len(self.failure_times),
                    self.failure_window_s, self.cool_off_s,
                )
                self.state = State.OPEN
                self.opened_at = now
                self.failure_times.clear()

    def snapshot(self) -> dict:
        """State für /health oder /metrics — ohne Lock (nur informativ)."""
        return {
            "name": self.name,
            "state": self.state.value,
            "recent_failures": len(self.failure_times),
            "opened_at_monotonic": self.opened_at,
        }


# Globaler Singleton für Capital.com-Aufrufe.
capital_breaker = CircuitBreaker(
    name="capital_com",
    failure_threshold=5,
    failure_window_s=60.0,
    cool_off_s=60.0,
)
