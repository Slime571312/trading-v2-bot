"""Self-Diagnose: Bot identifiziert systematische Schwäche-Muster.

Vault: Bot/Self-Diagnose.md. Nutzt die Trade-Journal-Tags (session_at_open,
atr_regime_at_open, weekday_at_open, confidence_tier_at_open, variant) um
gruppenbasierte Performance-Auswertungen zu liefern.

Output: Liste von `WeaknessPattern` — sortiert nach Severity (critical > warning > info).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from .state import ClosedTrade


_WEEKDAY_NAMES = {0: "Mo", 1: "Di", 2: "Mi", 3: "Do", 4: "Fr", 5: "Sa", 6: "So"}


@dataclass
class WeaknessPattern:
    """Beschreibt ein identifiziertes Schwäche-Muster im Trade-Verhalten."""
    category: str           # "variant" | "session" | "atr_regime" | "weekday" | "confidence_tier"
    key: str                # konkreter Wert ("ob_retest" | "london" | "high" | ...)
    win_rate: float         # 0.0–1.0
    expectancy_r: float     # avg R pro Trade
    sum_pnl: float          # kumuliertes PnL in € für diese Gruppe
    sample_size: int
    severity: str           # "critical" | "warning" | "info"
    diagnosis: str          # menschen-lesbar
    suggestion: str         # konkrete Maßnahme


@dataclass
class DiagnoseReport:
    instrument: str
    total_trades: int
    patterns: list[WeaknessPattern] = field(default_factory=list)
    summary: str = ""


def _aggregate(trades: list[ClosedTrade], key_fn: Callable[[ClosedTrade], str | None]) -> dict[str, dict]:
    """Groupby + Win-Rate/Expectancy-Berechnung."""
    groups: dict[str, dict] = {}
    for t in trades:
        key = key_fn(t)
        if key is None:
            continue
        key_str = str(key)
        g = groups.setdefault(key_str, {"n": 0, "wins": 0, "sum_r": 0.0, "sum_pnl": 0.0})
        g["n"] += 1
        if t.r_multiple > 0:
            g["wins"] += 1
        g["sum_r"] += t.r_multiple
        g["sum_pnl"] += (t.total_pnl if t.total_pnl else t.pnl_abs)
    return groups


def _severity(win_rate: float, expectancy: float, critical_wr: float, warn_wr: float) -> str:
    if expectancy < -0.2 or win_rate < critical_wr:
        return "critical"
    if expectancy < 0 or win_rate < warn_wr:
        return "warning"
    return "info"


def _format_label(category: str, key: str) -> str:
    """Pretty-Print für category-Werte (z.B. weekday-Zahl → 'Mo')."""
    if category == "weekday":
        try:
            return _WEEKDAY_NAMES.get(int(key), key)
        except ValueError:
            return key
    return key


def _make_pattern(
    category: str, key: str, group: dict,
    critical_wr: float, warn_wr: float,
) -> WeaknessPattern:
    n = group["n"]
    wr = group["wins"] / n if n else 0.0
    exp = group["sum_r"] / n if n else 0.0
    pnl = group["sum_pnl"]
    sev = _severity(wr, exp, critical_wr, warn_wr)
    label = _format_label(category, key)

    diagnosis_map = {
        "variant": f"Setup '{label}': {n} Trades, WR {wr:.0%}, Expectancy {exp:+.2f}R, PnL {pnl:+.2f}€",
        "session": f"Session '{label}': {n} Trades, WR {wr:.0%}, Expectancy {exp:+.2f}R, PnL {pnl:+.2f}€",
        "atr_regime": f"Vol-Regime '{label}': {n} Trades, WR {wr:.0%}, Expectancy {exp:+.2f}R, PnL {pnl:+.2f}€",
        "weekday": f"Wochentag '{label}': {n} Trades, WR {wr:.0%}, Expectancy {exp:+.2f}R, PnL {pnl:+.2f}€",
        "confidence_tier": f"Confidence-Tier '{label}': {n} Trades, WR {wr:.0%}, Expectancy {exp:+.2f}R, PnL {pnl:+.2f}€",
    }
    suggestion_map = {
        "variant": f"Conviction-Schwelle für '{label}' erhöhen oder Setup ganz deaktivieren.",
        "session": f"Session '{label}' aus dem Trade-Window streichen oder Filter strikter setzen.",
        "atr_regime": f"In Vol-Regime '{label}' Position-Size weiter senken oder Trades aussetzen.",
        "weekday": f"Wochentag '{label}': Weekday-Weight in Conviction-Score reduzieren.",
        "confidence_tier": f"Confidence-Tier '{label}' aus Trade-Window streichen (höhere Cutoff-Schwelle).",
    }
    return WeaknessPattern(
        category=category, key=str(key),
        win_rate=round(wr, 3),
        expectancy_r=round(exp, 3),
        sum_pnl=round(pnl, 2),
        sample_size=n,
        severity=sev,
        diagnosis=diagnosis_map.get(category, ""),
        suggestion=suggestion_map.get(category, ""),
    )


def find_weakness_patterns(
    closed_trades: Iterable[ClosedTrade],
    min_sample: int = 3,
    warn_wr: float = 0.42,
    critical_wr: float = 0.35,
) -> list[WeaknessPattern]:
    """Sucht systematische Schwächen in den geschlossenen Trades.

    Aggregiert über 5 Dimensionen (variant, session, atr_regime, weekday,
    confidence_tier) und nimmt Gruppen die `min_sample` erreichen UND
    negative Expectancy oder Win-Rate < warn_wr haben.

    Returns Liste sortiert nach Severity (critical → warning → info), dann
    nach Sample-Size (höher zuerst, weil statistisch signifikanter).
    """
    trades = list(closed_trades)
    if not trades:
        return []

    patterns: list[WeaknessPattern] = []
    categories: list[tuple[str, Callable[[ClosedTrade], str | None]]] = [
        ("variant", lambda t: t.variant),
        ("session", lambda t: t.session_at_open),
        ("atr_regime", lambda t: t.atr_regime_at_open),
        ("weekday", lambda t: str(t.weekday_at_open) if t.weekday_at_open is not None else None),
        ("confidence_tier", lambda t: t.confidence_tier_at_open),
    ]
    for category, key_fn in categories:
        groups = _aggregate(trades, key_fn)
        for key, group in groups.items():
            if group["n"] < min_sample:
                continue
            wr = group["wins"] / group["n"]
            exp = group["sum_r"] / group["n"]
            # Nur Patterns sammeln wo etwas auffällig ist
            if exp >= 0.1 and wr >= warn_wr:
                continue
            patterns.append(_make_pattern(category, key, group, critical_wr, warn_wr))

    sev_order = {"critical": 0, "warning": 1, "info": 2}
    patterns.sort(key=lambda p: (sev_order[p.severity], -p.sample_size))
    return patterns


def build_diagnose_report(
    instrument: str,
    closed_trades: Iterable[ClosedTrade],
    min_sample: int = 3,
) -> DiagnoseReport:
    """High-Level Report: Patterns + Klartext-Summary."""
    trades = list(closed_trades)
    patterns = find_weakness_patterns(trades, min_sample=min_sample)

    if not trades:
        summary = f"{instrument}: Noch keine geschlossenen Trades — keine Diagnose möglich."
    elif not patterns:
        summary = (
            f"{instrument}: {len(trades)} geschlossene Trades — keine systematischen "
            f"Schwächen über min_sample={min_sample} entdeckt."
        )
    else:
        critical = sum(1 for p in patterns if p.severity == "critical")
        warning = sum(1 for p in patterns if p.severity == "warning")
        info = sum(1 for p in patterns if p.severity == "info")
        top = patterns[0]
        summary = (
            f"{instrument}: {len(trades)} Trades — {critical} kritisch, "
            f"{warning} Warnungen, {info} Hinweise. "
            f"Top-Schwäche: {top.category}='{_format_label(top.category, top.key)}' "
            f"({top.win_rate:.0%} WR, {top.expectancy_r:+.2f}R)."
        )
    return DiagnoseReport(
        instrument=instrument,
        total_trades=len(trades),
        patterns=patterns,
        summary=summary,
    )
