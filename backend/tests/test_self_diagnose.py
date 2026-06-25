"""Tests für Self-Diagnose-Modul (Vault/Bot/Self-Diagnose.md)."""
from datetime import datetime, timezone

import pytest

from src.live.self_diagnose import (
    build_diagnose_report,
    find_weakness_patterns,
)
from src.live.state import ClosedTrade


def _ct(
    inst: str = "BTC",
    r: float = 1.0,
    pnl: float = 100.0,
    variant: str = "primary",
    session: str | None = "london",
    atr_regime: str | None = "normal",
    weekday: int | None = 2,
    confidence_tier: str | None = "normal",
) -> ClosedTrade:
    """Helper für minimalen ClosedTrade."""
    ts = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    return ClosedTrade(
        id="x", instrument=inst, side="long",
        open_time=ts, close_time=ts,
        entry=100.0, exit=110.0, sl=95.0, tp=115.0, size=1.0,
        variant=variant, htf_used="1h", ltf_used="5m",
        rr_at_open=3.0,
        pnl_abs=pnl, pnl_pct=1.0, r_multiple=r,
        exit_reason="tp",
        total_pnl=pnl,
        session_at_open=session,
        atr_regime_at_open=atr_regime,
        weekday_at_open=weekday,
        confidence_tier_at_open=confidence_tier,
    )


# ── Empty / leere Fälle ──────────────────────────────────────────────────


def test_empty_trades_returns_empty():
    assert find_weakness_patterns([]) == []


def test_below_min_sample_no_patterns():
    """Nur 2 Trades von einem Setup → unter min_sample=3 → kein Pattern."""
    trades = [_ct(r=-1.0, pnl=-50), _ct(r=-1.0, pnl=-50)]
    assert find_weakness_patterns(trades, min_sample=3) == []


# ── Klassische Schwäche-Erkennung ───────────────────────────────────────


def test_detects_losing_variant():
    """4 Trades alle Verlust → variant 'primary' wird kritisch."""
    trades = [_ct(r=-1.0, pnl=-50) for _ in range(4)]
    patterns = find_weakness_patterns(trades, min_sample=3)
    primary_patterns = [p for p in patterns if p.category == "variant" and p.key == "primary"]
    assert len(primary_patterns) == 1
    p = primary_patterns[0]
    assert p.sample_size == 4
    assert p.win_rate == 0.0
    assert p.severity == "critical"


def test_detects_losing_session():
    """Verlierende London-Trades, gewinnende NY-Trades → London als Schwäche markiert."""
    trades = [
        _ct(r=-1.0, pnl=-50, session="london"),
        _ct(r=-1.0, pnl=-50, session="london"),
        _ct(r=-1.0, pnl=-50, session="london"),
        _ct(r=2.0, pnl=100, session="ny"),
        _ct(r=2.0, pnl=100, session="ny"),
        _ct(r=2.0, pnl=100, session="ny"),
    ]
    patterns = find_weakness_patterns(trades, min_sample=3)
    london = [p for p in patterns if p.category == "session" and p.key == "london"]
    ny = [p for p in patterns if p.category == "session" and p.key == "ny"]
    assert len(london) == 1
    assert london[0].severity == "critical"
    assert ny == []   # NY ist profitabel → kein Pattern


def test_winning_set_no_patterns():
    """Alle Trades profitabel + hohe WR → keine Patterns."""
    trades = [_ct(r=2.0, pnl=100) for _ in range(5)]
    patterns = find_weakness_patterns(trades, min_sample=3)
    assert patterns == []


def test_severity_levels():
    """Kritisch (WR<35%) vs Warnung (WR<42%) vs Info."""
    # 3 von 10 Wins → WR = 30% → critical
    critical_trades = [_ct(r=2.0, pnl=100) for _ in range(3)] + [_ct(r=-1.0, pnl=-50) for _ in range(7)]
    patterns_c = find_weakness_patterns(critical_trades, min_sample=3)
    primary = [p for p in patterns_c if p.category == "variant"][0]
    assert primary.severity == "critical"


# ── Sortierung ────────────────────────────────────────────────────────────


def test_patterns_sorted_critical_first():
    """Critical-Patterns kommen vor Warnings."""
    trades = (
        # Critical: variant=ob_retest, 5x Verlust
        [_ct(variant="ob_retest", r=-1.0, pnl=-50) for _ in range(5)]
        # Warning: variant=primary, 4 Verlust + 2 Gewinn (WR 33%)
        + [_ct(variant="primary", r=-1.0, pnl=-50) for _ in range(4)]
        + [_ct(variant="primary", r=2.0, pnl=100) for _ in range(2)]
    )
    patterns = find_weakness_patterns(trades, min_sample=3)
    # Erste sollte critical sein
    assert patterns[0].severity == "critical"


# ── Report ───────────────────────────────────────────────────────────────


def test_report_for_empty_trades():
    report = build_diagnose_report("BTC", [])
    assert report.total_trades == 0
    assert "keine geschlossenen" in report.summary.lower()
    assert report.patterns == []


def test_report_for_clean_book():
    """Alles gewonnen → keine Schwächen, freundliche Summary."""
    trades = [_ct(r=2.0, pnl=100) for _ in range(5)]
    report = build_diagnose_report("BTC", trades, min_sample=3)
    assert report.total_trades == 5
    assert "keine systematischen Schwächen" in report.summary
    assert report.patterns == []


def test_report_top_pattern_in_summary():
    """Summary nennt die Top-Schwäche."""
    trades = [_ct(variant="ob_retest", r=-1.0, pnl=-50) for _ in range(4)]
    report = build_diagnose_report("BTC", trades, min_sample=3)
    assert report.patterns
    assert "ob_retest" in report.summary or "Setup" in report.summary
    assert "Top-Schwäche" in report.summary


# ── Klassifikations-Edge-Cases ──────────────────────────────────────────


def test_ignores_none_tags():
    """Trades ohne Tag-Felder werden ignoriert (Legacy-Records)."""
    trades = [_ct(session=None, r=-1.0, pnl=-50) for _ in range(5)]
    patterns = find_weakness_patterns(trades, min_sample=3)
    sessions = [p for p in patterns if p.category == "session"]
    assert sessions == []   # alle session=None → keine session-Aggregation
    # Aber variant=primary ist gesetzt → Pattern erwartet
    variants = [p for p in patterns if p.category == "variant"]
    assert len(variants) == 1


def test_weekday_label_translates_to_name():
    """Wochentag 2 → 'Mi' in diagnosis-Text."""
    trades = [_ct(weekday=2, r=-1.0, pnl=-50) for _ in range(4)]
    report = build_diagnose_report("BTC", trades, min_sample=3)
    wk_patterns = [p for p in report.patterns if p.category == "weekday"]
    assert wk_patterns
    assert "Mi" in wk_patterns[0].diagnosis
