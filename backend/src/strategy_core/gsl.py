"""Guaranteed-Stop-Loss-Decision (Vault/Bot/GSL-Manager.md).

Capital.com bietet `guaranteedStop=true` der Stops gegen Gap-Slippage absichert.
Kosten: ~0.02% des Trade-Werts (nur bei Trigger). Lohnt sich für Indizes-Positionen
die über Nacht / Wochenende offen sind — sonst kann ein Friday-Close-to-Sunday-Open-Gap
den Stop massiv durchschlagen.

**Im Paper-Trading kein Capital-API-Call.** Wir berechnen nur welcher Stop-Typ
*gewählt würde* und loggen ihn. Beim späteren Live-Switch wird das gleiche Modul
genutzt — dann führt es zum API-Parameter im Order-Body.

Entscheidung pro Trade:
- BTC: kein GSL (24/7-Markt, kein echtes Gap)
- Trade-Holdtime < 4h: kein GSL (intraday)
- Index-Trade mit Holdtime > 4h: vergleiche expected_gap_loss vs. GSL-Prämie
- Wenn `expected_gap_loss > 0.02% × position_value` → GSL lohnt
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timezone
from typing import Literal

log = logging.getLogger(__name__)

StopType = Literal["regular", "guaranteed", "trailing"]

# Typische historische Gap-Größen am Index-Open nach Wochenende (in % vom Close).
# Quelle: Vault-Recherche zu Friday-Sunday-Gaps + COVID-19 Volatility-Periode.
EXPECTED_GAP_PCT: dict[str, float] = {
    "DE40":   0.005,   # ~0.5 % typischer Mo-Gap, 2 % in Crisis
    "NASDAQ": 0.006,
    "SP500":  0.004,
    "BTC":    0.0,     # 24/7 — kein Markt-Open-Gap
}

# Capital.com GSL-Prämie (% des Positions-Werts, wird nur bei Trigger fällig).
GSL_PREMIUM_PCT = 0.0002   # 0.02 % per Capital.com Fees-Seite

# Minimale Trade-Holdtime damit GSL überhaupt erwogen wird (kürzere Trades
# überleben Gaps nicht, weil sie davor schon geschlossen sind).
MIN_HOLDTIME_FOR_GSL_HOURS = 4


def decide_stop_type(
    instrument: str,
    side: Literal["long", "short"],
    open_time_utc: datetime,
    expected_holdtime_hours: float,
) -> StopType:
    """Wählt den Stop-Typ für einen neuen Trade.

    Default: `regular`. Nur Indizes mit Übernacht-Risk bekommen `guaranteed`.
    `trailing` wird vom MultiTP-Lock-Profit-Mechanismus übernommen — wir setzen
    es hier nicht als initialen Typ.
    """
    if instrument == "BTC":
        return "regular"
    if expected_holdtime_hours < MIN_HOLDTIME_FOR_GSL_HOURS:
        return "regular"

    expected_gap_pct = EXPECTED_GAP_PCT.get(instrument, 0.0)
    if expected_gap_pct <= GSL_PREMIUM_PCT:
        return "regular"

    # Trade läuft über Wochenende? → GSL deutlich vorzuziehen
    if _crosses_weekend(open_time_utc, expected_holdtime_hours):
        return "guaranteed"
    # Über Nacht in der Woche? Auch sinnvoll, wenn Gap > Prämie
    return "guaranteed"


def _crosses_weekend(open_time_utc: datetime, holdtime_h: float) -> bool:
    """True wenn der Trade über die Wochenend-Schließung (Fr 22 UTC → So 22 UTC) läuft."""
    from datetime import timedelta
    if open_time_utc.tzinfo is None:
        open_time_utc = open_time_utc.replace(tzinfo=timezone.utc)
    end_time = open_time_utc + timedelta(hours=holdtime_h)
    # Iteriere stundenweise und prüfe ob mindestens 1 Stunde im Weekend liegt
    t = open_time_utc
    while t < end_time:
        dow = t.weekday()
        if (dow == 4 and t.hour >= 22) or dow == 5 or (dow == 6 and t.hour < 22):
            return True
        t += timedelta(hours=1)
    return False


def expected_gsl_premium(position_value: float) -> float:
    """Wie viel würde der GSL kosten wenn er triggert? (Nur bei Trigger fällig.)"""
    return position_value * GSL_PREMIUM_PCT
