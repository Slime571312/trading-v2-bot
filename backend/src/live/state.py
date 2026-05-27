"""BotState — Persistenz für den Live-Paper-Bot.

JSON-Datei (`backend/state/live_state.json`) überlebt Backend-Restarts. Schema
ist identisch zu dem, was die HTTP-API ausliefert — kein Mapping nötig.

Datums-Felder: ISO-Strings im JSON, datetime im Memory (Convertierung in den
classmethod-Konstruktoren).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

STATE_DIR = Path(__file__).resolve().parent.parent.parent / "state"
STATE_FILE = STATE_DIR / "live_state.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)

Side = Literal["long", "short"]
SignalVariant = Literal["primary", "ob_retest", "fvg_retest", "ultimate"]
ExitReason = Literal["sl", "tp", "manual", "bot_stopped"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _new_trade_id() -> str:
    return secrets.token_hex(6)


@dataclass(slots=True)
class OpenTrade:
    id: str
    instrument: str
    side: Side
    open_time: datetime
    entry: float       # nach Slippage
    sl: float
    tp: float
    size: float
    variant: SignalVariant
    htf_used: str
    ltf_used: str
    rr_at_open: float

    def to_json(self) -> dict:
        d = asdict(self)
        d["open_time"] = _iso(self.open_time)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "OpenTrade":
        return cls(
            id=d["id"], instrument=d["instrument"], side=d["side"],
            open_time=_parse_iso(d["open_time"]),
            entry=d["entry"], sl=d["sl"], tp=d["tp"], size=d["size"],
            variant=d["variant"], htf_used=d["htf_used"], ltf_used=d["ltf_used"],
            rr_at_open=d["rr_at_open"],
        )


@dataclass(slots=True)
class ClosedTrade:
    id: str
    instrument: str
    side: Side
    open_time: datetime
    close_time: datetime
    entry: float
    exit: float
    sl: float
    tp: float
    size: float
    variant: SignalVariant
    htf_used: str
    ltf_used: str
    rr_at_open: float
    pnl_abs: float
    pnl_pct: float
    r_multiple: float
    exit_reason: ExitReason

    def to_json(self) -> dict:
        d = asdict(self)
        d["open_time"] = _iso(self.open_time)
        d["close_time"] = _iso(self.close_time)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "ClosedTrade":
        return cls(
            id=d["id"], instrument=d["instrument"], side=d["side"],
            open_time=_parse_iso(d["open_time"]),
            close_time=_parse_iso(d["close_time"]),
            entry=d["entry"], exit=d["exit"], sl=d["sl"], tp=d["tp"], size=d["size"],
            variant=d["variant"], htf_used=d["htf_used"], ltf_used=d["ltf_used"],
            rr_at_open=d["rr_at_open"],
            pnl_abs=d["pnl_abs"], pnl_pct=d["pnl_pct"], r_multiple=d["r_multiple"],
            exit_reason=d["exit_reason"],
        )


@dataclass(slots=True)
class BotState:
    running: bool = False
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    last_tick: datetime | None = None
    last_signal_check: datetime | None = None
    tick_interval_s: int = 60
    initial_capital: float = 10_000.0
    equity: float = 10_000.0
    instruments: list[str] = field(default_factory=lambda: ["DE40", "NASDAQ", "SP500", "BTC"])
    open_trades: list[OpenTrade] = field(default_factory=list)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    last_error: str | None = None

    # Strategy-Parameter (eingefroren bei Start, geändert nur bei Reset/Restart)
    rr_threshold: float = 2.0
    risk_pct: float = 0.01
    sweep_lookback: int = 10

    def to_json(self) -> dict:
        return {
            "running": self.running,
            "started_at": _iso(self.started_at),
            "stopped_at": _iso(self.stopped_at),
            "last_tick": _iso(self.last_tick),
            "last_signal_check": _iso(self.last_signal_check),
            "tick_interval_s": self.tick_interval_s,
            "initial_capital": self.initial_capital,
            "equity": self.equity,
            "instruments": self.instruments,
            "open_trades": [t.to_json() for t in self.open_trades],
            "closed_trades": [t.to_json() for t in self.closed_trades],
            "last_error": self.last_error,
            "rr_threshold": self.rr_threshold,
            "risk_pct": self.risk_pct,
            "sweep_lookback": self.sweep_lookback,
        }

    @classmethod
    def from_json(cls, d: dict) -> "BotState":
        return cls(
            running=d.get("running", False),
            started_at=_parse_iso(d.get("started_at")),
            stopped_at=_parse_iso(d.get("stopped_at")),
            last_tick=_parse_iso(d.get("last_tick")),
            last_signal_check=_parse_iso(d.get("last_signal_check")),
            tick_interval_s=d.get("tick_interval_s", 60),
            initial_capital=d.get("initial_capital", 10_000.0),
            equity=d.get("equity", 10_000.0),
            instruments=d.get("instruments", ["DE40", "NASDAQ", "SP500", "BTC"]),
            open_trades=[OpenTrade.from_json(t) for t in d.get("open_trades", [])],
            closed_trades=[ClosedTrade.from_json(t) for t in d.get("closed_trades", [])],
            last_error=d.get("last_error"),
            rr_threshold=d.get("rr_threshold", 2.0),
            risk_pct=d.get("risk_pct", 0.01),
            sweep_lookback=d.get("sweep_lookback", 10),
        )


def load_state() -> BotState:
    """Lädt State aus JSON oder gibt frischen Default. Always-Running flag bleibt
    nach Restart auf False — Bot muss explizit (re-)gestartet werden."""
    if not STATE_FILE.exists():
        return BotState()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        state = BotState.from_json(data)
        # Sicherheit: nach Backend-Restart starten wir den Tick nicht automatisch.
        # Der User muss das bewusst (re-)triggern via /bot/start.
        state.running = False
        return state
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.error("State-Datei korrupt (%s) — starte mit Default-State", e)
        return BotState()


def save_state(state: BotState) -> None:
    """Atomisches Schreiben: temp-File → rename. Verhindert kaputten State bei Crash."""
    tmp_path = None
    try:
        # NamedTemporaryFile im selben Dir, damit os.replace atomar bleibt (gleiches FS)
        fd, tmp_path = tempfile.mkstemp(dir=STATE_DIR, prefix=".live_state.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state.to_json(), f, indent=2, default=str)
        os.replace(tmp_path, STATE_FILE)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
