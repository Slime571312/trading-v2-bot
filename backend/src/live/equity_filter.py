"""Equity-Curve-Filter — Drawdown-adaptive Position-Sizing (Vault/Bot/Equity-Curve-Filter.md).

Idee: ICT-Setups bleiben gleich, aber Lot-Size skaliert mit Account-Gesundheit.
Wenn der Bot in einer Drawdown-Phase ist, wird die Position automatisch kleiner.
Bei neuem Equity-High wieder voll. Schützt vor Drawdown-Verstärkung durch
volle Risk-Pct während einer Schwäche-Phase.

Tier-Schema:
  Drawdown von Equity-High …
    < 3%  → 1.0× (voll)
    3–5%  → 0.5×
    5–8%  → 0.25×
    > 8%  → 0.0× (kein neuer Trade)

Equity-High wird über `closed_trades` rekonstruiert; kein zusätzlicher State
notwendig, kein Migrations-Bedarf.
"""
from __future__ import annotations

from typing import Literal


EquityTier = Literal["healthy", "drawdown_light", "drawdown_medium", "drawdown_severe"]


def _running_equity_curve(initial_capital: float, closed_trades_pnls: list[float]) -> list[float]:
    """Cumsum der PnLs auf Initial-Capital — ergibt Equity nach jedem Trade."""
    curve = [initial_capital]
    eq = initial_capital
    for pnl in closed_trades_pnls:
        eq += pnl
        curve.append(eq)
    return curve


def equity_filter_factor(
    initial_capital: float,
    current_equity: float,
    closed_trades_pnls: list[float],
) -> tuple[float, EquityTier, float]:
    """Returns (size_factor, tier, drawdown_pct).

    - size_factor ∈ {1.0, 0.5, 0.25, 0.0} — wird × risk_pct multipliziert.
    - tier: human-readable Label für TickLog/Dashboard.
    - drawdown_pct: aktueller Drawdown gegenüber dem höchsten je erreichten Equity (positiv = im DD).
    """
    curve = _running_equity_curve(initial_capital, closed_trades_pnls)
    equity_high = max(max(curve), current_equity)
    if equity_high <= 0:
        return (1.0, "healthy", 0.0)
    drawdown_pct = (equity_high - current_equity) / equity_high * 100.0
    drawdown_pct = max(0.0, drawdown_pct)

    if drawdown_pct < 3.0:
        return (1.0, "healthy", drawdown_pct)
    if drawdown_pct < 5.0:
        return (0.5, "drawdown_light", drawdown_pct)
    if drawdown_pct < 8.0:
        return (0.25, "drawdown_medium", drawdown_pct)
    return (0.0, "drawdown_severe", drawdown_pct)
