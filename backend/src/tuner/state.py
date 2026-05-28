"""Persistenz für Tuner-Runs (History). Schema-Equivalent zu live/state.py
und chat/state.py — atomare JSON-Persistenz via tempfile-rename."""
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
TUNER_FILE = STATE_DIR / "tuner_history.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)

TunerStatus = Literal["running", "completed", "failed"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return f"t_{secrets.token_hex(6)}"


@dataclass(slots=True)
class TunerRun:
    """Ein Tuner-Lauf — alle Instrumente in einem Pass."""

    id: str
    started_at: str
    finished_at: str | None = None
    status: TunerStatus = "running"
    instruments: list[str] = field(default_factory=list)
    iter_tf: str = "5m"
    bars_used: int = 1000
    # Pro Instrument: GridResult.to_json() (full)
    results: dict[str, dict] = field(default_factory=dict)
    # Proposal-IDs die aus diesem Run generiert wurden
    proposal_ids: list[str] = field(default_factory=list)
    # Optional Claude-Rationale (wenn API-Key vorhanden, sonst None)
    claude_summary: str | None = None
    error: str | None = None
    triggered_by: Literal["manual", "cron"] = "manual"

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "TunerRun":
        return cls(**d)


@dataclass(slots=True)
class TunerHistory:
    runs: list[TunerRun] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"runs": [r.to_json() for r in self.runs]}

    @classmethod
    def from_json(cls, d: dict) -> "TunerHistory":
        return cls(runs=[TunerRun.from_json(r) for r in d.get("runs", [])])


def load_history() -> TunerHistory:
    if not TUNER_FILE.exists():
        return TunerHistory()
    try:
        with TUNER_FILE.open("r", encoding="utf-8") as f:
            return TunerHistory.from_json(json.load(f))
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.error("Tuner-History korrupt (%s) — starte leer", e)
        return TunerHistory()


def save_history(history: TunerHistory) -> None:
    """Behält maximal 50 letzte Runs (älteres droppen — JSON wäre sonst riesig)."""
    if len(history.runs) > 50:
        history.runs = history.runs[-50:]
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".tuner.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(history.to_json(), f, indent=2, default=str)
        os.replace(tmp, TUNER_FILE)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def new_run(instruments: list[str], iter_tf: str, bars: int,
            triggered_by: Literal["manual", "cron"]) -> TunerRun:
    return TunerRun(
        id=_new_run_id(),
        started_at=_now_iso(),
        instruments=instruments,
        iter_tf=iter_tf,
        bars_used=bars,
        triggered_by=triggered_by,
    )
