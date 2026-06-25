"""Risk-Adjusted Performance-Metriken — Sortino, Calmar, Omega.

Vault: Bot/Performance-Metriken.md.

Warum nicht nur Sharpe?
- ICT-Setups erzeugen positive Skew (kleine Verluste, mittlere/große Gewinne).
- Sharpe bestraft Upside-Vola genauso wie Downside — verzerrt unsere Bewertung.
- **Sortino:** nur Downside im Nenner → fair für Skewed Returns.
- **Calmar:** Return / |MaxDD| — robust gegenüber Verteilungsannahmen.
- **Omega:** vollständige empirische Verteilung, ratio gewichteter Wins zu Losses.

Eingaben: R-Multiple-Serie pro Trade + Equity-Curve. Alle Metriken trade-basiert
(keine Annualisierung nötig, weil Trade-Frequenz pro Instrument niedrig ist).
"""
from __future__ import annotations

import math
from collections.abc import Sequence


def sortino_ratio(r_multiples: Sequence[float], threshold: float = 0.0) -> float | None:
    """Sortino auf Trade-R-Multiples.

    Formel: mean(R) / std_downside(R) wobei std_downside nur über R < threshold.
    Returns None bei < 3 Trades, < 2 negativen Trades oder std_downside = 0.
    """
    if len(r_multiples) < 3:
        return None
    arr = list(r_multiples)
    mean = sum(arr) / len(arr) - threshold
    downside = [r for r in arr if r < threshold]
    if len(downside) < 2:
        return None
    # Population std (n nicht n-1) — bei kleinen Stichproben robuster
    mean_dn = sum(downside) / len(downside)
    var_dn = sum((d - mean_dn) ** 2 for d in downside) / len(downside)
    std_dn = math.sqrt(var_dn)
    if std_dn <= 0:
        # Konstante Losses (typisch ICT: jeder SL-Hit ist exakt -1R) — std=0.
        # Fallback: |mean_loss| als Downside-Proxy, damit Sortino nicht None bleibt.
        std_dn = abs(mean_dn)
        if std_dn <= 0:
            return None
    return mean / std_dn


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """Maximaler Drawdown in %. Returns negativen Wert (-0.23 = -23 %)."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (eq - peak) / peak
            if dd < max_dd:
                max_dd = dd
    return max_dd


def calmar_ratio(
    r_multiples: Sequence[float],
    equity_curve: Sequence[float],
) -> float | None:
    """Calmar = Sum(R) / |MaxDD| über die Equity-Curve.

    Nutzt die normalisierte R-Summe statt Annualised-Return — Trade-Bot-fair.
    Returns None bei < 3 Trades oder MaxDD = 0.
    """
    if len(r_multiples) < 3:
        return None
    total_r = sum(r_multiples)
    mdd = max_drawdown(equity_curve)
    if mdd == 0:
        return None
    return total_r / abs(mdd) / 100.0   # /100 weil mdd in Anteil, nicht Prozent


def omega_ratio(r_multiples: Sequence[float], threshold: float = 0.0) -> float | None:
    """Omega = Σ(R-τ | R>τ) / |Σ(R-τ | R<τ)|.

    Vollständige empirische Verteilung — keine Normalverteilungs-Annahme.
    Returns None bei < 3 Trades oder keinen Losses.
    """
    if len(r_multiples) < 3:
        return None
    excess = [r - threshold for r in r_multiples]
    gains = sum(e for e in excess if e > 0)
    losses = abs(sum(e for e in excess if e < 0))
    if losses <= 0:
        return None
    return gains / losses


def compute_all(
    r_multiples: Sequence[float],
    equity_curve: Sequence[float],
) -> dict[str, float | None]:
    """Liefert alle 3 Metriken + max_dd als Dict — für Stats-Endpoint."""
    return {
        "sortino": sortino_ratio(r_multiples),
        "calmar": calmar_ratio(r_multiples, equity_curve),
        "omega": omega_ratio(r_multiples),
        "max_drawdown_pct": max_drawdown(equity_curve) * 100.0,
    }
