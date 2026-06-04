"""BotState — Multi-Instance-Persistenz für den Live-Paper-Bot.

Eine BotInstance pro Instrument (DE40 / NASDAQ / SP500 / BTC), jede mit eigener
Equity, eigenen Params, eigenem Tick-Loop, eigenen Trades. Erlaubt es BTC mit
anderen RR-Schwellen zu fahren als DE40 etc.

Zusätzlich pro Instance: `tick_log` — letzte N Tick-Reasoning-Einträge für die
„warum hat der Bot das gemacht"-View im Frontend.

Schema-Bruch zur Single-Bot-Version (commits ≤ Etappe 5): bei alter JSON ohne
`instances`-Key wird komplett resettet (4 leere Instances).
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

DEFAULT_INSTRUMENTS = ["DE40", "NASDAQ", "SP500", "BTC"]
TICK_LOG_MAX = 200  # zirkular, älteste fallen raus

Side = Literal["long", "short"]
SignalVariant = Literal["primary", "ob_retest", "fvg_retest", "ultimate"]
ExitReason = Literal["sl", "tp", "manual", "bot_stopped"]
TickAction = Literal["eval", "open", "close", "skip", "error", "outside_session"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _new_trade_id() -> str:
    return secrets.token_hex(6)


# ── Trade-Records (unverändert vs. Single-Bot) ─────────────────────────


@dataclass(slots=True)
class OpenTrade:
    id: str
    instrument: str
    side: Side
    open_time: datetime
    entry: float
    sl: float
    tp: float
    size: float
    variant: SignalVariant
    htf_used: str
    ltf_used: str
    rr_at_open: float
    # Partial-Close + Dynamic SL (Bot/Partial-Close.md + Bot/Dynamic-SL.md)
    tp1: float | None = None           # Erstes 50%-Teilziel (Hälfte des Wegs zu TP)
    tp1_hit: bool = False              # True nach TP1-Hit + BE-Move
    original_size: float | None = None # Ursprüngliche Size (vor partial close)
    original_sl: float | None = None   # Ursprünglicher SL (Analytics)
    partial_pnl: float = 0.0           # Bereits realisierter PnL aus TP1
    partial_close_time: datetime | None = None  # Wann TP1 geschnitten wurde

    def to_json(self) -> dict:
        d = asdict(self)
        d["open_time"] = _iso(self.open_time)
        d["partial_close_time"] = _iso(self.partial_close_time)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "OpenTrade":
        return cls(
            id=d["id"], instrument=d["instrument"], side=d["side"],
            open_time=_parse_iso(d["open_time"]),
            entry=d["entry"], sl=d["sl"], tp=d["tp"], size=d["size"],
            variant=d["variant"], htf_used=d["htf_used"], ltf_used=d["ltf_used"],
            rr_at_open=d["rr_at_open"],
            tp1=d.get("tp1"),
            tp1_hit=d.get("tp1_hit", False),
            original_size=d.get("original_size"),
            original_sl=d.get("original_sl"),
            partial_pnl=d.get("partial_pnl", 0.0),
            partial_close_time=_parse_iso(d.get("partial_close_time")),
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


# ── TickLog — neu für „warum hat der Bot das gemacht" ─────────────────


@dataclass(slots=True)
class TickLogEntry:
    """Ein Eintrag pro Tick (oder pro signifikantem Ereignis innerhalb des Ticks).

    Liefert die Antwort auf „warum hat der Bot in diesem Moment X gemacht":
    welcher Bias, welcher HTF-Sweep, welcher LTF-BOS, welche RR-Rechnung,
    welche Entscheidung.
    """
    timestamp: datetime
    action: TickAction
    decision: str  # human-readable: "opened_primary", "skipped_no_setup", etc.
    bias: str | None = None
    htf_used: str | None = None
    ltf_used: str | None = None
    sweep_time: str | None = None
    sweep_direction: str | None = None
    bos_time: str | None = None
    rr_computed: float | None = None
    variant: str | None = None
    detail: str | None = None
    related_trade_id: str | None = None

    def to_json(self) -> dict:
        d = asdict(self)
        d["timestamp"] = _iso(self.timestamp)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "TickLogEntry":
        return cls(
            timestamp=_parse_iso(d["timestamp"]),
            action=d["action"],
            decision=d["decision"],
            bias=d.get("bias"),
            htf_used=d.get("htf_used"),
            ltf_used=d.get("ltf_used"),
            sweep_time=d.get("sweep_time"),
            sweep_direction=d.get("sweep_direction"),
            bos_time=d.get("bos_time"),
            rr_computed=d.get("rr_computed"),
            variant=d.get("variant"),
            detail=d.get("detail"),
            related_trade_id=d.get("related_trade_id"),
        )


# ── BotInstance — eine Bot-Instanz pro Instrument ─────────────────────


@dataclass(slots=True)
class BotInstance:
    instrument: str
    running: bool = False
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    last_tick: datetime | None = None
    last_signal_check: datetime | None = None
    tick_interval_s: int = 60
    initial_capital: float = 10_000.0
    equity: float = 10_000.0
    open_trades: list[OpenTrade] = field(default_factory=list)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    last_error: str | None = None
    rr_threshold: float = 2.0
    risk_pct: float = 0.01
    sweep_lookback: int = 10
    tick_log: list[TickLogEntry] = field(default_factory=list)

    def append_tick_log(self, entry: TickLogEntry) -> None:
        self.tick_log.append(entry)
        if len(self.tick_log) > TICK_LOG_MAX:
            del self.tick_log[: len(self.tick_log) - TICK_LOG_MAX]

    def to_json(self) -> dict:
        return {
            "instrument": self.instrument,
            "running": self.running,
            "started_at": _iso(self.started_at),
            "stopped_at": _iso(self.stopped_at),
            "last_tick": _iso(self.last_tick),
            "last_signal_check": _iso(self.last_signal_check),
            "tick_interval_s": self.tick_interval_s,
            "initial_capital": self.initial_capital,
            "equity": self.equity,
            "open_trades": [t.to_json() for t in self.open_trades],
            "closed_trades": [t.to_json() for t in self.closed_trades],
            "last_error": self.last_error,
            "rr_threshold": self.rr_threshold,
            "risk_pct": self.risk_pct,
            "sweep_lookback": self.sweep_lookback,
            "tick_log": [t.to_json() for t in self.tick_log],
        }

    @classmethod
    def from_json(cls, d: dict) -> "BotInstance":
        return cls(
            instrument=d["instrument"],
            running=d.get("running", False),
            started_at=_parse_iso(d.get("started_at")),
            stopped_at=_parse_iso(d.get("stopped_at")),
            last_tick=_parse_iso(d.get("last_tick")),
            last_signal_check=_parse_iso(d.get("last_signal_check")),
            tick_interval_s=d.get("tick_interval_s", 60),
            initial_capital=d.get("initial_capital", 10_000.0),
            equity=d.get("equity", 10_000.0),
            open_trades=[OpenTrade.from_json(t) for t in d.get("open_trades", [])],
            closed_trades=[ClosedTrade.from_json(t) for t in d.get("closed_trades", [])],
            last_error=d.get("last_error"),
            rr_threshold=d.get("rr_threshold", 2.0),
            risk_pct=d.get("risk_pct", 0.01),
            sweep_lookback=d.get("sweep_lookback", 10),
            tick_log=[TickLogEntry.from_json(t) for t in d.get("tick_log", [])],
        )


@dataclass(slots=True)
class BotState:
    """Multi-Instance State. Schlüssel = Instrument-Name."""
    instances: dict[str, BotInstance] = field(default_factory=dict)

    def get(self, instrument: str) -> BotInstance | None:
        return self.instances.get(instrument)

    def ensure(self, instrument: str) -> BotInstance:
        if instrument not in self.instances:
            self.instances[instrument] = BotInstance(instrument=instrument)
        return self.instances[instrument]

    def list_instruments(self) -> list[str]:
        return sorted(self.instances.keys())

    def any_running(self) -> bool:
        return any(inst.running for inst in self.instances.values())

    def total_equity(self) -> float:
        return sum(inst.equity for inst in self.instances.values())

    def to_json(self) -> dict:
        return {
            "schema_version": 2,
            "instances": {k: v.to_json() for k, v in self.instances.items()},
        }

    @classmethod
    def from_json(cls, d: dict) -> "BotState":
        instances_d = d.get("instances", {})
        return cls(
            instances={k: BotInstance.from_json(v) for k, v in instances_d.items()},
        )


# ── Defaults / Migration ────────────────────────────────────────────────


def _fresh_state() -> BotState:
    """4 leere Instanzen für die Default-Instrumente."""
    state = BotState()
    for inst in DEFAULT_INSTRUMENTS:
        state.ensure(inst)
    return state


def load_state() -> BotState:
    """Lädt State aus JSON. Bei Schema-Bruch oder Fehler → frische 4 Instanzen.

    Always-Running flags werden beim Load auf False zurückgesetzt — Bot muss
    explizit (re-)gestartet werden.
    """
    if not STATE_FILE.exists():
        log.info("Kein State gefunden → fresh state mit %d Instanzen", len(DEFAULT_INSTRUMENTS))
        return _fresh_state()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if "instances" not in data:
            log.warning(
                "Alter Single-Bot State erkannt (kein 'instances'-Key) — wird verworfen, "
                "starte mit %d leeren Instances", len(DEFAULT_INSTRUMENTS),
            )
            return _fresh_state()
        state = BotState.from_json(data)
        for inst in state.instances.values():
            inst.running = False  # nach Restart kein Auto-Start
        # Fehlende Default-Instrumente nachziehen
        for inst_name in DEFAULT_INSTRUMENTS:
            state.ensure(inst_name)
        return state
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.error("State-Datei korrupt (%s) — frischer State", e)
        return _fresh_state()


def save_state(state: BotState) -> None:
    """Atomic-write (tempfile + rename)."""
    tmp_path = None
    try:
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
