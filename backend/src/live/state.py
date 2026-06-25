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
ARCHIVE_DIR = STATE_DIR / "archive"
STATE_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_INSTRUMENTS = ["DE40", "NASDAQ", "SP500", "BTC"]
TICK_LOG_MAX = 200          # zirkular, älteste fallen raus
CLOSED_TRADES_HOT_MAX = 500 # nur die jüngsten N im JSON-State, Rest in Archive (.parquet via to_csv)

# Schema-Version wird in jede Datei geschrieben. Bei Bruch (z.B. Felder
# umbenannt, neue required-Fields) inkrementieren — load_state erkennt
# alte Versionen und resettet sauber, statt Trades mit Default-Müll zu laden.
STATE_SCHEMA_VERSION = 3

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
    tp: float                          # finales TP (= tp3, das weitestes Target)
    size: float
    variant: SignalVariant
    htf_used: str
    ltf_used: str
    rr_at_open: float
    # Multi-TP-Staffelung (Vault/Bot/TP-Staffelung.md)
    # 3-Tier: tp1 (IRL/nähester Pivot) → tp2 (ERL/mittlerer Pivot) → tp (HTF-Draw)
    # Splits müssen zu 1.0 summieren. A-Setup: 0.4/0.4/0.2 (Runner aktiv).
    # B-Setup: 0.5/0.5/0.0 (bei tp2 wird voll geschlossen).
    tp1: float | None = None           # Erstes Teilziel (IRL)
    tp2: float | None = None           # Zweites Teilziel (ERL) — NEU für 3-Tier
    tp1_hit: bool = False              # True nach TP1-Hit + BE-Move (SL → Entry)
    tp2_hit: bool = False              # True nach TP2-Hit + Lock-Profit (SL → tp1)
    pct1: float = 0.5                  # Default 50/50 (2-Tier, alte Logik)
    pct2: float = 0.5
    pct3: float = 0.0
    original_size: float | None = None # Ursprüngliche Size (vor partial close)
    original_sl: float | None = None   # Ursprünglicher SL (Analytics)
    partial_pnl: float = 0.0           # Bereits realisierter PnL aus TP1/TP2
    partial_close_time: datetime | None = None  # Wann TP1 geschnitten wurde
    # Outage-Tracking: jüngste Bar, die der Loop dem Trade gegenüber verarbeitet
    # hat. Beim Restart nach Outage lädt der Loop gezielt nur die Bars seit
    # `last_seen_bar` statt einfach die letzten 5 — verhindert dass SL/TP-Hits
    # in der Lücke übersehen werden, ohne historisches Backfill als Lookahead.
    last_seen_bar: datetime | None = None
    # GSL-Decision (Vault/Bot/GSL-Manager.md): "regular" | "guaranteed" | "trailing"
    # Im Paper-Mode nur informativ. Beim späteren Live-Switch wird das in den
    # Capital.com Order-Body als `guaranteedStop=true` übersetzt.
    stop_type: str = "regular"
    # Trade-Journal-Tags (Vault/Bot/Trade-Journal.md): Snapshot bei Entry für
    # Performance-Attribution (gruppiert nach Session, Vol-Regime, Wochentag).
    session_at_open: str | None = None
    atr_regime_at_open: str | None = None
    atr_pct_at_open: float | None = None
    vol_size_factor_at_open: float | None = None
    weekday_at_open: int | None = None
    conviction_score_at_open: int | None = None
    conviction_grade_at_open: str | None = None
    adx_regime_at_open: str | None = None
    confidence_at_open: float | None = None
    confidence_tier_at_open: str | None = None

    def to_json(self) -> dict:
        d = asdict(self)
        d["open_time"] = _iso(self.open_time)
        d["partial_close_time"] = _iso(self.partial_close_time)
        d["last_seen_bar"] = _iso(self.last_seen_bar)
        d["stop_type"] = self.stop_type
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
            tp2=d.get("tp2"),
            tp1_hit=d.get("tp1_hit", False),
            tp2_hit=d.get("tp2_hit", False),
            pct1=d.get("pct1", 0.5),
            pct2=d.get("pct2", 0.5),
            pct3=d.get("pct3", 0.0),
            original_size=d.get("original_size"),
            original_sl=d.get("original_sl"),
            partial_pnl=d.get("partial_pnl", 0.0),
            partial_close_time=_parse_iso(d.get("partial_close_time")),
            last_seen_bar=_parse_iso(d.get("last_seen_bar")),
            stop_type=d.get("stop_type", "regular"),
            session_at_open=d.get("session_at_open"),
            atr_regime_at_open=d.get("atr_regime_at_open"),
            atr_pct_at_open=d.get("atr_pct_at_open"),
            vol_size_factor_at_open=d.get("vol_size_factor_at_open"),
            weekday_at_open=d.get("weekday_at_open"),
            conviction_score_at_open=d.get("conviction_score_at_open"),
            conviction_grade_at_open=d.get("conviction_grade_at_open"),
            adx_regime_at_open=d.get("adx_regime_at_open"),
            confidence_at_open=d.get("confidence_at_open"),
            confidence_tier_at_open=d.get("confidence_tier_at_open"),
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
    pnl_abs: float          # Runner-Portion (= equity-Increment beim Final-Close)
    pnl_pct: float
    r_multiple: float       # Total-R (gegen ursprüngliches Risk, inkl. Partials)
    exit_reason: ExitReason
    partial_pnl: float = 0.0   # Realisierter PnL aus TP1/TP2 vor Final-Close
    total_pnl: float = 0.0     # Wahres Trade-Ergebnis = pnl_abs + partial_pnl
    # Multi-TP-Audit-Felder — sonst sind ClosedTrades retro nicht verifizierbar.
    # (Live-Verhalten unverändert; OpenTrade hatte diese Felder schon.)
    tp1: float | None = None
    tp2: float | None = None
    pct1: float | None = None
    pct2: float | None = None
    pct3: float | None = None
    original_sl: float | None = None
    original_size: float | None = None
    # Trade-Journal-Tags — Übernahme aus OpenTrade beim Schließen.
    session_at_open: str | None = None
    atr_regime_at_open: str | None = None
    atr_pct_at_open: float | None = None
    vol_size_factor_at_open: float | None = None
    weekday_at_open: int | None = None
    conviction_score_at_open: int | None = None
    conviction_grade_at_open: str | None = None
    adx_regime_at_open: str | None = None
    confidence_at_open: float | None = None
    confidence_tier_at_open: str | None = None

    def to_json(self) -> dict:
        d = asdict(self)
        d["open_time"] = _iso(self.open_time)
        d["close_time"] = _iso(self.close_time)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "ClosedTrade":
        pnl_abs = d["pnl_abs"]
        partial_pnl = d.get("partial_pnl", 0.0)
        # Legacy-Records ohne total_pnl: gleich pnl_abs setzen, war damals das
        # gespeicherte Trade-Ergebnis (auch wenn bei B-Setups TP-Partials fehlten).
        total_pnl = d.get("total_pnl", pnl_abs + partial_pnl)
        return cls(
            id=d["id"], instrument=d["instrument"], side=d["side"],
            open_time=_parse_iso(d["open_time"]),
            close_time=_parse_iso(d["close_time"]),
            entry=d["entry"], exit=d["exit"], sl=d["sl"], tp=d["tp"], size=d["size"],
            variant=d["variant"], htf_used=d["htf_used"], ltf_used=d["ltf_used"],
            rr_at_open=d["rr_at_open"],
            pnl_abs=pnl_abs, pnl_pct=d["pnl_pct"], r_multiple=d["r_multiple"],
            exit_reason=d["exit_reason"],
            partial_pnl=partial_pnl, total_pnl=total_pnl,
            tp1=d.get("tp1"),
            tp2=d.get("tp2"),
            pct1=d.get("pct1"),
            pct2=d.get("pct2"),
            pct3=d.get("pct3"),
            original_sl=d.get("original_sl"),
            original_size=d.get("original_size"),
            session_at_open=d.get("session_at_open"),
            atr_regime_at_open=d.get("atr_regime_at_open"),
            atr_pct_at_open=d.get("atr_pct_at_open"),
            vol_size_factor_at_open=d.get("vol_size_factor_at_open"),
            weekday_at_open=d.get("weekday_at_open"),
            conviction_score_at_open=d.get("conviction_score_at_open"),
            conviction_grade_at_open=d.get("conviction_grade_at_open"),
            adx_regime_at_open=d.get("adx_regime_at_open"),
            confidence_at_open=d.get("confidence_at_open"),
            confidence_tier_at_open=d.get("confidence_tier_at_open"),
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
    # `was_running`: bleibt True wenn der Bot ZULETZT lief und NICHT vom User
    # gestoppt wurde. Beim Backend-Start liest der Lifespan-Hook das Flag und
    # startet die betroffenen Instanzen automatisch — damit überlebt der „24/7
    # bis ich selbst stoppe"-Modus Crashes, OS-Updates und Boot-Zyklen.
    was_running: bool = False
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
    # Setup-FSM (Vault/Bot/Setup-State-Machine.md): höchste erreichte Stufe
    # der letzten Engine-Eval. Stage-Werte: idle | init | daily_bars_ok |
    # bias_set | in_session | sweep_detected | bos_confirmed | rr_ok | entry_ready
    setup_stage: str = "idle"

    def append_tick_log(self, entry: TickLogEntry) -> None:
        self.tick_log.append(entry)
        if len(self.tick_log) > TICK_LOG_MAX:
            del self.tick_log[: len(self.tick_log) - TICK_LOG_MAX]

    def append_closed_trade(self, trade: "ClosedTrade") -> None:
        """Trade an closed_trades anhängen. Wenn die Liste über CLOSED_TRADES_HOT_MAX
        wächst, werden die ältesten in eine CSV-Archive-Datei ausgelagert.

        Damit bleibt die JSON-State-Datei klein und ladbar — die Historie
        verschwindet nicht, sie wandert nur in `state/archive/<instrument>.csv`.
        """
        self.closed_trades.append(trade)
        if len(self.closed_trades) > CLOSED_TRADES_HOT_MAX:
            overflow = len(self.closed_trades) - CLOSED_TRADES_HOT_MAX
            to_archive = self.closed_trades[:overflow]
            self.closed_trades = self.closed_trades[overflow:]
            _append_to_archive(self.instrument, to_archive)

    def to_json(self) -> dict:
        return {
            "instrument": self.instrument,
            "running": self.running,
            "was_running": self.was_running,
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
            "setup_stage": self.setup_stage,
        }

    @classmethod
    def from_json(cls, d: dict) -> "BotInstance":
        return cls(
            instrument=d["instrument"],
            running=d.get("running", False),
            was_running=d.get("was_running", False),
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
            setup_stage=d.get("setup_stage", "idle"),
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
            "schema_version": STATE_SCHEMA_VERSION,
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
    """4 leere Instanzen für die Default-Instrumente. BTC bekommt strengere Defaults."""
    state = BotState()
    for name in DEFAULT_INSTRUMENTS:
        inst = state.ensure(name)
        if name == "BTC":
            # BTC-Hardening: strengeres RR-Gate, längeres Sweep-Lookback,
            # passt zu A-only Filter + min_sl_pct=0.5% im Broker.
            inst.rr_threshold = 2.5
            inst.sweep_lookback = 20
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
        file_version = data.get("schema_version", 0)
        if file_version < STATE_SCHEMA_VERSION:
            backup = STATE_FILE.with_suffix(f".v{file_version}.bak")
            log.warning(
                "State schema_version=%d < current=%d — Trades enthalten u.U. "
                "alte/fehlende Felder. Sichere als %s, starte fresh.",
                file_version, STATE_SCHEMA_VERSION, backup.name,
            )
            try:
                STATE_FILE.rename(backup)
            except OSError:
                pass
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


def _append_to_archive(instrument: str, trades: list["ClosedTrade"]) -> None:
    """Hängt Trades an `state/archive/<instrument>.csv` an (CSV, header beim ersten Write).

    CSV statt JSON, damit Pandas das ohne Parsing-Overhead in DataFrames lädt
    (für spätere Analyse/Backtest-Replays).
    """
    if not trades:
        return
    import csv
    p = ARCHIVE_DIR / f"{instrument}.csv"
    header_needed = not p.exists()
    try:
        with p.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if header_needed:
                writer.writerow([
                    "id", "instrument", "side", "open_time", "close_time",
                    "entry", "exit", "sl", "tp", "size",
                    "variant", "htf_used", "ltf_used", "rr_at_open",
                    "pnl_abs", "pnl_pct", "r_multiple", "exit_reason",
                ])
            for t in trades:
                writer.writerow([
                    t.id, t.instrument, t.side,
                    _iso(t.open_time), _iso(t.close_time),
                    t.entry, t.exit, t.sl, t.tp, t.size,
                    t.variant, t.htf_used, t.ltf_used, t.rr_at_open,
                    t.pnl_abs, t.pnl_pct, t.r_multiple, t.exit_reason,
                ])
        log.info("Archived %d Trades → %s", len(trades), p.name)
    except OSError as e:
        log.error("Archive-Write fehlgeschlagen für %s: %s", instrument, e)


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
