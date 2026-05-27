"""Session-Filter — Indizes nur London-Open + NY-Open, BTC 24/7.

Spec aus Trading/Bot/Playbook.md:
- **DE40 / NASDAQ / SP500:** London 09:00–12:00 + NY 15:30–18:00 (deutsche Zeit)
- **BTC:** 24/7 (Krypto hat keine Session)

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


def is_in_session(ts: pd.Timestamp, instrument: str) -> bool:
    """True wenn der Zeitpunkt in einem erlaubten Trading-Fenster für das Instrument liegt."""
    if instrument in CRYPTO_INSTRUMENTS:
        return True

    # Capital-Timestamps kommen UTC; auf Berlin konvertieren
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    berlin = ts.tz_convert(BERLIN)
    t = berlin.time()
    in_london = LONDON_OPEN[0] <= t < LONDON_OPEN[1]
    in_ny = NY_OPEN[0] <= t < NY_OPEN[1]
    return in_london or in_ny


def session_label(ts: pd.Timestamp, instrument: str) -> str:
    """Lesbares Label fürs Dashboard — 'london' | 'ny' | 'crypto-24h' | 'closed'."""
    if instrument in CRYPTO_INSTRUMENTS:
        return "crypto-24h"
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    berlin = ts.tz_convert(BERLIN)
    t = berlin.time()
    if LONDON_OPEN[0] <= t < LONDON_OPEN[1]:
        return "london"
    if NY_OPEN[0] <= t < NY_OPEN[1]:
        return "ny"
    return "closed"
