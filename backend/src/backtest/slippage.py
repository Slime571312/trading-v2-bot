"""Slippage-Modell — Capital.com pro Instrument + Session-Multiplikator.

Spec: Vault/Bot/Slippage-Model.md.

Capital.com ist ein Market-Maker mit variablen Spreads. Die Werte unten sind
Recherche-Mittelwerte aus Capital-Spread-Daten + BrokerChooser-Reviews
+ FXVerify (DE40). Spread weitet sich session-abhängig (Pre-Market/News
1.5–3×, Off-Hours weiter).

Modell pro Tick: `slippage = max(base_spread_abs, range × range_factor)`
- `base_spread_abs`: kleinster typischer Spread in absoluten Preis-Einheiten
- `range_factor`: bei sehr volatilen Bars kann der effektive Spread höher
  liegen (Order-Book-Sweeps) — 5 % der Bar-Range
- Session-Multiplikator on top: Silver-Bullet ×1.0, normal ×1.3, news ×2.5
"""
from __future__ import annotations

from typing import Literal

import pandas as pd

Side = Literal["long", "short"]


# Typischer Capital.com Spread in **absoluten Preis-Einheiten** pro Seite.
# Werte aus Vault-Slippage-Model.md (Recherche-Mittelwerte).
SPREAD_ABS: dict[str, float] = {
    "DE40":   1.5,    # 1.5 Pkt bei ~24k Preis → 0.006%
    "NASDAQ": 0.8,    # 0.8 Pkt bei ~30k Preis → 0.003%
    "SP500":  0.5,    # 0.5 Pkt bei ~7.5k → 0.007%
    "BTC":    50.0,   # 50 USD bei ~63k → 0.08%
    "ETH":    5.0,    # ~5 USD bei ~3.5k → 0.14% (Reference-Asset)
}


def _spread_for(instrument: str | None, price: float) -> float:
    """Liefert den absoluten Spread für ein Instrument, gefallback in Bps."""
    if instrument and instrument in SPREAD_ABS:
        return SPREAD_ABS[instrument]
    return price * 0.0003   # 3 Bps Fallback wenn unbekannt


def session_multiplier(ts: pd.Timestamp | None, instrument: str | None) -> float:
    """Session-Aufschlag auf Spread.

    - Off-Hours / Pre-Market: ×1.5 (dünne Order-Books)
    - News-Window (±30 Min um High-Impact-Events): ×2.5
    - Reguläre Session inkl. Silver-Bullet: ×1.0
    """
    if ts is None or instrument is None:
        return 1.0
    try:
        from src.strategy_core import news as news_mod
        from src.strategy_core import sessions as sessions_mod
        in_news, _ = news_mod.is_news_window(instrument, now=ts.to_pydatetime())
        if in_news:
            return 2.5
        if not sessions_mod.is_in_session(ts, instrument):
            return 1.5
    except Exception:
        pass
    return 1.0


def estimate_slippage(
    close: float, high: float, low: float,
    instrument: str | None = None,
    ts: pd.Timestamp | None = None,
) -> float:
    """Schätzt relative Slippage als Bruchteil des Preises.

    Modell:
      effective_spread = max(SPREAD_ABS[inst], range × 0.05) × session_mult
      relative_slip = effective_spread / 2 / close   # halber Spread pro Seite
    """
    if close <= 0:
        return 0.0
    bar_range = max(0.0, high - low)
    spread_abs = max(_spread_for(instrument, close), bar_range * 0.05)
    spread_abs *= session_multiplier(ts, instrument)
    # Spread ist Round-Trip-Cost; pro Seite die Hälfte → / 2
    return (spread_abs / 2.0) / close


def apply_slippage_entry(
    price: float, side: Side, high: float, low: float,
    instrument: str | None = None, ts: pd.Timestamp | None = None,
) -> float:
    """Slippage gegen den Entry — Long kauft teurer, Short verkauft günstiger."""
    slip = estimate_slippage(price, high, low, instrument=instrument, ts=ts)
    return price * (1 + slip) if side == "long" else price * (1 - slip)


def apply_slippage_exit(
    price: float, side: Side, high: float, low: float,
    instrument: str | None = None, ts: pd.Timestamp | None = None,
) -> float:
    """Slippage gegen den Exit — Long-Verkauf zu tieferem, Short-Buyback zu höherem."""
    slip = estimate_slippage(price, high, low, instrument=instrument, ts=ts)
    return price * (1 - slip) if side == "long" else price * (1 + slip)
