"""Session-Filter — Indizes nur London-Open + NY-Open + Wochentag, BTC 24/7.

Spec aus Trading/Bot/Playbook.md:
- **DE40 / NASDAQ / SP500:** London 09:00–12:00 + NY 15:30–18:00 (deutsche Zeit)
- **BTC:** 24/7 (Krypto hat keine Session)

Wochenend-Regel (aus User-Spec):
- Freitag ab 22:00 UTC → Sonntag 22:00 UTC: Indizes geschlossen, nur BTC läuft.
  Begründung: NY-Future-Close Freitag 21:00 UTC + Buffer; Sonntag-Asia-Open 22:00 UTC.

Außerhalb der Fenster: Setups dürfen erkannt + markiert werden, aber **kein Entry**.
"""
from __future__ import annotations

from datetime import time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

BERLIN = ZoneInfo("Europe/Berlin")
CRYPTO_INSTRUMENTS = {"BTC"}

LONDON_OPEN = (dtime(9, 0), dtime(12, 0))
NY_OPEN = (dtime(15, 30), dtime(18, 0))

# Wochenende für Indizes — UTC-basiert (Capital-Timestamps sind UTC).
# Friday 22:00 UTC – Sunday 22:00 UTC = Markt geschlossen für CFD-Indizes.
WEEKEND_START_DOW = 4   # Friday (Monday=0)
WEEKEND_START_HOUR_UTC = 22
WEEKEND_END_DOW = 6     # Sunday
WEEKEND_END_HOUR_UTC = 22


def _is_index_market_closed(ts_utc: pd.Timestamp) -> bool:
    """True wenn Indizes-Markt geschlossen ist (Fr 22:00 UTC → So 22:00 UTC)."""
    dow = ts_utc.dayofweek
    hour = ts_utc.hour
    if dow == WEEKEND_START_DOW and hour >= WEEKEND_START_HOUR_UTC:
        return True
    if dow == 5:   # Saturday — komplett geschlossen
        return True
    if dow == WEEKEND_END_DOW and hour < WEEKEND_END_HOUR_UTC:
        return True
    return False


def is_in_session(ts: pd.Timestamp, instrument: str) -> bool:
    """True wenn der Zeitpunkt in einem erlaubten Trading-Fenster für das Instrument liegt."""
    if instrument in CRYPTO_INSTRUMENTS:
        return True   # BTC: immer offen

    # Capital-Timestamps kommen UTC
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_utc = ts.tz_convert("UTC")

    # Wochenende → Indizes geschlossen
    if _is_index_market_closed(ts_utc):
        return False

    berlin = ts.tz_convert(BERLIN)
    t = berlin.time()
    in_london = LONDON_OPEN[0] <= t < LONDON_OPEN[1]
    in_ny = NY_OPEN[0] <= t < NY_OPEN[1]
    return in_london or in_ny


def session_label(ts: pd.Timestamp, instrument: str) -> str:
    """Lesbares Label fürs Dashboard — 'london' | 'ny' | 'crypto-24h' | 'weekend' | 'closed'."""
    if instrument in CRYPTO_INSTRUMENTS:
        return "crypto-24h"
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    if _is_index_market_closed(ts.tz_convert("UTC")):
        return "weekend"
    berlin = ts.tz_convert(BERLIN)
    t = berlin.time()
    if LONDON_OPEN[0] <= t < LONDON_OPEN[1]:
        return "london"
    if NY_OPEN[0] <= t < NY_OPEN[1]:
        return "ny"
    return "closed"
