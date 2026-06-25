"""Tests für Holiday-Calendar in sessions.py — NYSE/Eurex 2026/2027."""
import pandas as pd

from src.strategy_core.sessions import (
    is_in_session, is_market_holiday, session_label,
)


def _ts(date_str: str, hour: int = 14) -> pd.Timestamp:
    """Helper: UTC-Timestamp aus 'YYYY-MM-DD' + Stunde (default 14:00 UTC = NY-Mittag)."""
    return pd.Timestamp(f"{date_str} {hour:02d}:00:00", tz="UTC")


def test_nasdaq_blocked_on_thanksgiving_2026():
    assert is_market_holiday(_ts("2026-11-26"), "NASDAQ")
    assert not is_in_session(_ts("2026-11-26"), "NASDAQ")
    assert session_label(_ts("2026-11-26"), "NASDAQ") == "holiday"


def test_sp500_blocked_on_christmas_2026():
    assert is_market_holiday(_ts("2026-12-25"), "SP500")
    assert not is_in_session(_ts("2026-12-25"), "SP500")


def test_de40_blocked_on_easter_monday_2026():
    assert is_market_holiday(_ts("2026-04-06"), "DE40")
    assert not is_in_session(_ts("2026-04-06"), "DE40")


def test_de40_blocked_on_labor_day_2026():
    """Tag der Arbeit (1. Mai) — Eurex/Xetra geschlossen."""
    assert is_market_holiday(_ts("2026-05-01"), "DE40")


def test_btc_ignores_holidays():
    """Crypto trade auch an Feiertagen — Holiday-Gate gilt nicht für BTC."""
    assert not is_market_holiday(_ts("2026-12-25"), "BTC")
    # In Crypto-Hours: trade-fähig, auch an Christmas
    assert is_in_session(_ts("2026-12-25", hour=14), "BTC")


def test_nasdaq_open_on_regular_trading_day():
    """Mittwoch außerhalb Holidays + in NY-Open-Window → trade-fähig."""
    # 2026-06-10 = Mittwoch. NY-Open ist 15:30-18:00 Berlin (CEST=UTC+2 → 13:30-16:00 UTC).
    # 14:00 UTC = 16:00 Berlin = mitten im Window.
    ts = _ts("2026-06-10", hour=14)
    assert not is_market_holiday(ts, "NASDAQ")
    assert is_in_session(ts, "NASDAQ")


def test_nyse_holiday_calendar_size():
    """Sanity: 10 NYSE Holidays/Jahr × 2 Jahre = ~20 Einträge."""
    from src.strategy_core.sessions import NYSE_HOLIDAYS, EUREX_HOLIDAYS
    # 2026 hat 10 Holidays, 2027 hat ~10 → minimum 18
    assert len(NYSE_HOLIDAYS) >= 18
    # Eurex 2026: 8 Tage, 2027: 6 → mindestens 14
    assert len(EUREX_HOLIDAYS) >= 13
