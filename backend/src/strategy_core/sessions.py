"""Session-Filter — Indizes nur London-Open + NY-Open + Wochentag, BTC 24/7.
Plus OPEX-Calendar und Silver-Bullet Window-Boost.

Spec aus Trading/Bot/Playbook.md + Bot/OPEX-Calendar.md + Bot/Silver-Bullet.md:
- **DE40 / NASDAQ / SP500:** London 09:00–12:00 + NY 15:30–18:00 (deutsche Zeit)
- **BTC:** 24/7 (Krypto hat keine Session)
- **OPEX:** Size-Reduktion (×0.5 Triple-Witching, ×0.75 Monthly OPEX);
  letzte 30 Min am OPEX-Tag = kein neuer Entry (Multi-Mrd Pin-Flows).
- **Silver-Bullet:** NY AM 10:00-11:00 ET (= 15:00-16:00 UTC) = höchste Win-Rate.

Wochenend-Regel (aus User-Spec):
- Freitag ab 22:00 UTC → Sonntag 22:00 UTC: Indizes geschlossen, nur BTC läuft.

Außerhalb der Fenster: Setups dürfen erkannt + markiert werden, aber **kein Entry**.
"""
from __future__ import annotations

from datetime import date, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

BERLIN = ZoneInfo("Europe/Berlin")
NY_TZ = ZoneInfo("America/New_York")
CRYPTO_INSTRUMENTS = {"BTC"}

LONDON_OPEN = (dtime(9, 0), dtime(12, 0))
NY_OPEN = (dtime(15, 30), dtime(18, 0))

# Silver-Bullet Fenster (ET, DST-sicher via ZoneInfo)
SILVER_BULLET_WINDOWS: dict[str, tuple[dtime, dtime]] = {
    "london_open": (dtime(3, 0),  dtime(4, 0)),    # 08:00–09:00 UTC
    "ny_am":       (dtime(10, 0), dtime(11, 0)),   # primär — höchste WR
    "ny_pm":       (dtime(14, 0), dtime(15, 0)),
}

# OPEX-Size-Multiplikatoren (Bot/OPEX-Calendar.md)
OPEX_TRIPLE_WITCHING_MULT = 0.50   # Quartal-OPEX (Mar/Jun/Sep/Dez)
OPEX_MONTHLY_MULT = 0.75           # Monthly OPEX
OPEX_NORMAL_MULT = 1.00

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


# ── OPEX-Calendar (Bot/OPEX-Calendar.md) ──────────────────────────────────


def third_friday(year: int, month: int) -> date:
    """Dritter Freitag des Monats — Standard OPEX-Termin."""
    first = date(year, month, 1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(weeks=2)


def is_opex_day(dt: date) -> bool:
    """True wenn dt der 3. Freitag des Monats ist (Monthly OPEX)."""
    return dt == third_friday(dt.year, dt.month)


def is_monthly_opex_week(dt: date) -> bool:
    """Mo–Fr der Woche mit 3. Freitag — Pin-Risk dominiert ganze Woche."""
    tf = third_friday(dt.year, dt.month)
    opex_monday = tf - timedelta(days=tf.weekday())  # Mo der OPEX-Woche
    return opex_monday <= dt <= tf


def is_triple_witching_week(dt: date) -> bool:
    """Quartal-OPEX (Mar/Jun/Sep/Dez) — 2–3× Effektstärke ggü. monatlich."""
    return dt.month in (3, 6, 9, 12) and is_monthly_opex_week(dt)


def opex_size_multiplier(dt: date) -> float:
    """Position-Size-Faktor basierend auf OPEX-Phase.

    Triple Witching → 0.50× (Pin-Risk + Multi-Mrd-Flows)
    Monthly OPEX    → 0.75× (Pin-Risk dominiert)
    Sonst           → 1.00×
    """
    if is_triple_witching_week(dt):
        return OPEX_TRIPLE_WITCHING_MULT
    if is_monthly_opex_week(dt):
        return OPEX_MONTHLY_MULT
    return OPEX_NORMAL_MULT


def is_opex_close_window(ts: pd.Timestamp) -> bool:
    """True wenn ts in den letzten 30 Min des OPEX-Tages liegt (kein neuer Entry).

    OPEX-Tag = 3. Freitag. Letzte 30 Min vor US-Close (= 20:30-21:00 UTC bei EDT,
    21:30-22:00 UTC bei EST). Wir gateen UTC-pragmatisch auf 20:30 UTC.
    """
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_utc = ts.tz_convert("UTC")
    if not is_opex_day(ts_utc.date()):
        return False
    # Letzte 30 Min vor 21:00 UTC (NY-Equity-Close 16:00 ET = 20:00 EDT / 21:00 EST)
    return ts_utc.hour == 20 and ts_utc.minute >= 30


# ── Silver-Bullet Windows (Bot/Silver-Bullet.md) ──────────────────────────


def is_silver_bullet_window(ts: pd.Timestamp, window: str = "ny_am") -> bool:
    """True wenn ts im Silver-Bullet-Fenster liegt (NY-Zeit-basiert, DST-sicher).

    Drei Fenster aus dem Vault:
      - london_open: 03:00-04:00 ET (= 08:00-09:00 UTC EDT)
      - ny_am:       10:00-11:00 ET (= 15:00-16:00 UTC EDT) — primär, höchste WR
      - ny_pm:       14:00-15:00 ET (= 19:00-20:00 UTC EDT)
    """
    if window not in SILVER_BULLET_WINDOWS:
        return False
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ny_t = ts.tz_convert(NY_TZ).time()
    start, end = SILVER_BULLET_WINDOWS[window]
    return start <= ny_t < end


def is_any_silver_bullet_window(ts: pd.Timestamp) -> bool:
    """True wenn in irgendeinem der 3 Silver-Bullet-Fenster."""
    return any(is_silver_bullet_window(ts, w) for w in SILVER_BULLET_WINDOWS)
