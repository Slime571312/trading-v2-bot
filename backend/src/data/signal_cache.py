"""SignalCache — hält ausgewertete Signals für alle Instrumente in Memory,
refresht im Hintergrund. /signal/{inst} und /signals werden so instant.

Ohne Cache: Dashboard öffnet 4 Cards × 6 TF-Fetches = 24 Capital-Calls beim Page-Load.
Mit Cache: Calls passieren im Hintergrund alle N Sekunden, UI antwortet aus Memory.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from src.brokers.capital_com import CapitalAPIError, CapitalAuthError
from src.config import EPIC_MAP
from src.data.fetcher import load_candles
from src.strategy_core import evaluate as evaluate_signal
from src.strategy_core.bias import compute_bias

log = logging.getLogger(__name__)

DEFAULT_INSTRUMENTS = ["DE40", "NASDAQ", "SP500", "BTC"]
DEFAULT_REFRESH_INTERVAL_S = 60
TFS_NEEDED = ["1d", "1h", "30m", "15m", "5m", "1m"]


@dataclass(slots=True)
class CachedSignal:
    instrument: str
    epic: str
    refreshed_at: datetime
    side: str = "none"  # long | short | none | error
    bias: str = "neutral"
    variant: str | None = None
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    rr: float | None = None
    htf_used: str | None = None
    ltf_used: str | None = None
    has_ob: bool = False
    has_fvg: bool = False
    reason: str = ""
    error: str | None = None

    def to_json(self) -> dict:
        return {
            "instrument": self.instrument,
            "epic": self.epic,
            "refreshed_at": self.refreshed_at.isoformat(),
            "side": self.side,
            "bias_direction": self.bias,  # kompatibel zur alten API
            "variant": self.variant,
            "entry": self.entry,
            "sl": self.sl,
            "tp": self.tp,
            "rr": self.rr,
            "htf_used": self.htf_used,
            "ltf_used": self.ltf_used,
            "has_ob": self.has_ob,
            "has_fvg": self.has_fvg,
            "reason": self.reason,
            "error": self.error,
            # Felder, die die alte /signal-Response hatte aber nicht mehr nötig sind,
            # liefern wir als None zurück damit das Frontend ohne Schema-Bruch klappt
            "bias_bos_time": None,
            "bias_bos_level": None,
        }


class SignalCache:
    """Singleton mit Background-Refresh-Task."""

    def __init__(self) -> None:
        self._signals: dict[str, CachedSignal] = {}
        self._instruments: list[str] = list(DEFAULT_INSTRUMENTS)
        self._refresh_interval_s = DEFAULT_REFRESH_INTERVAL_S
        self._task: asyncio.Task | None = None
        self._first_refresh_done = asyncio.Event()

    @property
    def instruments(self) -> list[str]:
        return list(self._instruments)

    def get(self, instrument: str) -> CachedSignal | None:
        return self._signals.get(instrument)

    def get_all(self) -> list[CachedSignal]:
        return [self._signals[i] for i in self._instruments if i in self._signals]

    async def wait_initial(self, timeout: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(self._first_refresh_done.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def start(self, instruments: list[str] | None = None,
                    refresh_interval_s: int | None = None) -> None:
        if instruments:
            self._instruments = list(instruments)
        if refresh_interval_s:
            self._refresh_interval_s = max(15, refresh_interval_s)
        if self._task and not self._task.done():
            return  # läuft schon
        log.info("SignalCache start: instruments=%s, refresh=%ds",
                 self._instruments, self._refresh_interval_s)
        self._task = asyncio.create_task(self._loop(), name="signal_cache_refresh")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def refresh_now(self, instrument: str | None = None) -> None:
        """On-demand-Refresh, parallel über alle TFs/Instrumente."""
        targets = [instrument] if instrument else self._instruments
        await asyncio.gather(
            *[self._refresh_one(inst) for inst in targets],
            return_exceptions=True,
        )

    async def _loop(self) -> None:
        # Initial-Refresh sofort (parallel)
        await self.refresh_now()
        self._first_refresh_done.set()
        while True:
            try:
                await asyncio.sleep(self._refresh_interval_s)
                await self.refresh_now()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.exception("SignalCache-Loop Fehler: %s", e)
                await asyncio.sleep(5)  # backoff

    async def _refresh_one(self, instrument: str) -> None:
        try:
            bars: dict[str, pd.DataFrame] = {}
            for tf in TFS_NEEDED:
                try:
                    df = await load_candles(instrument, tf, bars=200)
                    if not df.empty:
                        bars[tf] = df
                except (CapitalAuthError, CapitalAPIError) as e:
                    log.debug("cache %s %s: %s", instrument, tf, e)

            if "1d" not in bars:
                self._signals[instrument] = CachedSignal(
                    instrument=instrument, epic=EPIC_MAP.get(instrument, instrument),
                    refreshed_at=datetime.now(timezone.utc),
                    side="error", reason="Daily-Daten nicht ladbar",
                    error="missing_1d",
                )
                return

            bias = compute_bias(bars["1d"])
            signal = evaluate_signal(instrument, bars)

            if signal is None:
                self._signals[instrument] = CachedSignal(
                    instrument=instrument, epic=EPIC_MAP.get(instrument, instrument),
                    refreshed_at=datetime.now(timezone.utc),
                    side="none", bias=bias.direction,
                    reason=(
                        "neutraler Daily-Bias" if bias.direction == "neutral"
                        else "kein HTF-Sweep + LTF-BOS im Lookback-Fenster"
                    ),
                )
            else:
                self._signals[instrument] = CachedSignal(
                    instrument=instrument, epic=EPIC_MAP.get(instrument, instrument),
                    refreshed_at=datetime.now(timezone.utc),
                    side=signal.side, bias=signal.bias.direction,
                    variant=signal.variant,
                    entry=round(signal.entry, 4),
                    sl=round(signal.sl, 4),
                    tp=round(signal.tp, 4),
                    rr=round(signal.rr, 3),
                    htf_used=signal.htf_used,
                    ltf_used=signal.ltf_used,
                    has_ob=signal.ob is not None,
                    has_fvg=signal.fvg is not None,
                    reason=f"setup: {signal.variant} ({signal.htf_used}-sweep → {signal.ltf_used}-bos)",
                )
        except Exception as e:
            log.exception("cache _refresh_one %s gecrasht", instrument)
            self._signals[instrument] = CachedSignal(
                instrument=instrument, epic=EPIC_MAP.get(instrument, instrument),
                refreshed_at=datetime.now(timezone.utc),
                side="error", reason=f"crash: {e}", error=str(e)[:200],
            )


# ── Singleton ───────────────────────────────────────────────────────────


_cache: SignalCache | None = None


def get_signal_cache() -> SignalCache:
    global _cache
    if _cache is None:
        _cache = SignalCache()
    return _cache
