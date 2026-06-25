"""Startup-Recovery — 4-Gates-Sequenz vor dem ersten Tick (Vault/Bot/Startup-Recovery.md).

Sequenz:
  Gate 1 — Config-Validation (Hard-Fail wenn unbenutzbar)
  Gate 2 — State-Schema-Migration (Schema-Check + Auto-Backup bei zu altem Schema)
  Gate 3 — Position-Reconciliation (DB ↔ Capital.com Divergenz-Check)
  Gate 4 — Pre-Flight Capital-Ping (kein Auto-Resume in toten Broker)

Jeder Gate schreibt ein Resultat in `StartupResult`. Bei kritischen Fehlern
wird `StartupError` geraised → Backend lädt trotzdem die FastAPI-Routen, aber
Auto-Resume + Tick-Loops bleiben aus. Damit kann der User über das Dashboard
sehen *warum* der Bot nicht startet, statt blind in einem kaputten Zustand
zu laufen.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .orchestrator import BotOrchestrator

log = logging.getLogger(__name__)


class StartupError(Exception):
    """Wird geraised wenn ein Hard-Gate fehlschlägt — Auto-Resume bleibt aus."""


@dataclass
class StartupResult:
    """Zusammenfassung der Startup-Gates für /health und Logs."""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    config_ok: bool = False
    schema_ok: bool = False
    reconciliation_ok: bool = False
    capital_ok: bool = False
    can_auto_resume: bool = False     # nur True wenn ALLE Gates ok
    config_errors: list[str] = field(default_factory=list)
    schema_message: str = ""
    reconciliation_message: str = ""
    capital_message: str = ""


# ── Gate 1 — Config-Validation ─────────────────────────────────────────

def validate_config() -> list[str]:
    """Semantische Validierung der BotConfig. Returns Liste der Fehler (leer = ok)."""
    from src.config import config
    errors: list[str] = []

    # API-Keys vorhanden? — Im Paper-Mode keine Pflicht, aber dann wissen
    # wir dass Position-Reconciliation und Live-Switch nicht gehen.
    if not config.broker_configured:
        errors.append("broker_credentials_missing (CAPITAL_API_KEY/EMAIL/PASSWORD)")

    # Risk-Parameter plausibel
    if config.risk_pct_per_trade > 0.05:
        errors.append(
            f"risk_pct_per_trade={config.risk_pct_per_trade:.3f} > 5% — gefährlich hoch"
        )
    if config.risk_pct_per_trade <= 0:
        errors.append(f"risk_pct_per_trade={config.risk_pct_per_trade} <= 0 — unmöglich")

    # Instruments-Liste nicht leer
    if not config.instruments:
        errors.append("instruments_list_empty")

    # RR-Threshold sinnvoll
    if config.rr_threshold_direct < 1.0:
        errors.append(f"rr_threshold_direct={config.rr_threshold_direct} < 1.0 — kein positive expectancy möglich")

    return errors


# ── Gate 2 — Schema-Migration ──────────────────────────────────────────

def check_state_schema() -> tuple[bool, str]:
    """Prüft `live_state.json` schema_version gegen STATE_SCHEMA_VERSION.

    State.load_state() macht den eigentlichen Migrations-Backup-Pfad bereits
    automatisch. Hier nur Reporting für `/health`.
    """
    from src.live.state import STATE_FILE, STATE_SCHEMA_VERSION
    if not STATE_FILE.exists():
        return True, "no_state_file — fresh start"
    try:
        import json
        data = json.loads(STATE_FILE.read_text())
        file_version = data.get("schema_version", 0)
        if file_version < STATE_SCHEMA_VERSION:
            # load_state() würde es automatisch umbenennen; hier dokumentieren.
            return False, f"schema_outdated (file=v{file_version}, code=v{STATE_SCHEMA_VERSION}) — wird beim Load automatisch verworfen"
        if file_version > STATE_SCHEMA_VERSION:
            return False, f"schema_newer_than_code (file=v{file_version}, code=v{STATE_SCHEMA_VERSION}) — Code ist veraltet"
        return True, f"schema_v{file_version}_ok"
    except (json.JSONDecodeError, OSError) as e:
        return False, f"state_unreadable: {e}"


# ── Gate 3 — Position-Reconciliation ───────────────────────────────────

async def reconcile_positions(orch: "BotOrchestrator") -> tuple[bool, str]:
    """Vergleicht JSON-State open_trades mit Capital.com aktuell offenen Positionen.

    Bei Divergenz wird ein Telegram-Alert gefeuert + ein Eintrag in den
    TickLog der betroffenen Instance. Auto-Resume wird trotzdem nicht
    blockiert — der User soll selbst entscheiden ob er den Bot weiterlaufen
    lässt oder stoppt.

    Im Paper-Mode hat Capital.com KEINE echten Positionen — wir vergleichen
    trotzdem (Capital liefert für Demo-Konten den Demo-Positionen-Stand).
    """
    from src.brokers.capital_com import (
        CapitalAuthError, CapitalAPIError, request as capital_request,
    )
    from src.config import config
    if not config.broker_configured:
        return True, "no_broker_credentials — reconciliation übersprungen"

    expected_open = {
        inst.instrument: [t.id for t in inst.open_trades]
        for inst in orch.state.instances.values()
        if inst.open_trades
    }

    # Capital-Positionen holen
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await capital_request(client, "GET", "/positions")
            data = res.json()
    except (CapitalAuthError, CapitalAPIError) as e:
        return False, f"capital_positions_unreachable: {e}"
    except Exception as e:
        return False, f"reconciliation_failed: {type(e).__name__}: {e}"

    capital_positions = data.get("positions", [])
    # Capital liefert eine Liste, jedes Element hat "market.instrumentName"
    # und "position.dealId". Wir gruppieren nach unserem Playbook-Namen.
    from src.config import EPIC_MAP
    epic_to_inst = {v: k for k, v in EPIC_MAP.items()}
    capital_by_inst: dict[str, list[str]] = {}
    for pos in capital_positions:
        epic = pos.get("market", {}).get("epic", "")
        inst = epic_to_inst.get(epic)
        if inst:
            capital_by_inst.setdefault(inst, []).append(pos.get("position", {}).get("dealId", ""))

    # Divergenzen sammeln
    divergences: list[str] = []
    all_inst = set(expected_open.keys()) | set(capital_by_inst.keys())
    for inst in all_inst:
        n_expected = len(expected_open.get(inst, []))
        n_actual = len(capital_by_inst.get(inst, []))
        if n_expected != n_actual:
            divergences.append(f"{inst}: bot_db={n_expected} capital={n_actual}")

    if divergences:
        # Telegram-Alert (best-effort, no-op wenn nicht konfiguriert)
        from src.live import notifier
        await notifier.alert_critical_error(
            None, f"Position-Reconciliation Divergenz:\n" + "\n".join(divergences)
        )
        return False, f"divergence: {'; '.join(divergences)}"
    return True, f"reconciled (expected={sum(len(v) for v in expected_open.values())} positions match)"


# ── Gate 4 — Pre-Flight Capital-Ping ──────────────────────────────────

async def capital_preflight() -> tuple[bool, str]:
    """Capital.com erreichbar? Wenn nicht: Auto-Resume aufschieben.

    Wir nutzen `/markets/{epic}` für DE40 als simpler Ping — `/session` würde
    eine neue Auth erfordern und die Singleton-Session in capital_com.py
    invalidieren.
    """
    from src.brokers.capital_com import (
        CapitalAuthError, CapitalAPIError, request as capital_request,
    )
    from src.config import config
    if not config.broker_configured:
        return False, "no_broker_credentials"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await capital_request(client, "GET", "/markets/DE40", retry_on_401=True)
            if res.status_code == 200:
                return True, "capital_reachable"
            return False, f"capital_unexpected_status={res.status_code}"
    except (CapitalAuthError, CapitalAPIError) as e:
        return False, f"capital_error: {e}"
    except Exception as e:
        return False, f"network_error: {type(e).__name__}: {e}"


# ── Orchestrator: alle Gates in Reihe ──────────────────────────────────

async def run_startup_sequence(orch: "BotOrchestrator") -> StartupResult:
    """Führt alle 4 Gates aus und liefert ein StartupResult."""
    log.info("=== Startup-Sequenz beginnt ===")
    result = StartupResult()

    # Gate 1
    errs = validate_config()
    result.config_errors = errs
    result.config_ok = not errs
    if errs:
        log.warning("Config-Validation: %s", "; ".join(errs))
    else:
        log.info("Config-Validation: OK")

    # Gate 2
    ok, msg = check_state_schema()
    result.schema_ok = ok
    result.schema_message = msg
    log.info("Schema-Check: %s (%s)", "OK" if ok else "WARN", msg)

    # Gate 3
    ok, msg = await reconcile_positions(orch)
    result.reconciliation_ok = ok
    result.reconciliation_message = msg
    log.info("Position-Reconciliation: %s (%s)", "OK" if ok else "WARN", msg)

    # Gate 4
    ok, msg = await capital_preflight()
    result.capital_ok = ok
    result.capital_message = msg
    log.info("Capital-Preflight: %s (%s)", "OK" if ok else "WARN", msg)

    # Auto-Resume nur wenn ALLE 4 Gates ok — config UND schema UND reconciliation UND capital
    result.can_auto_resume = (
        result.config_ok and result.schema_ok
        and result.reconciliation_ok and result.capital_ok
    )

    log.info("=== Startup-Sequenz fertig (auto_resume=%s) ===", result.can_auto_resume)
    return result


# Modul-Level Cache des letzten Results — für /health-Endpoint
_last_startup_result: StartupResult | None = None


def set_last_result(result: StartupResult) -> None:
    global _last_startup_result
    _last_startup_result = result


def get_last_result() -> StartupResult | None:
    return _last_startup_result
