"""Slippage-Modell — 1:1 aus Trading/Bot/Backtest-Methodik.md.

5-Komponenten-Modell von QuantJourney (OHLCV-only). Für Capital.com-CFDs auf
Retail-Lots dominieren **Spread (52 %)** und **Range-Intensität (26 %)**.
Marktimpact ist bei kleinen Sizes vernachlässigbar.

Per Decision in Backtest-Methodik.md: **Required Dependency in runner.py**,
kein optionaler Flag — sonst werden Backtests unvergleichbar.

Typische Werte:
- DE40 / NASDAQ / SP500 (Indizes): Slippage ≈ 0.006–0.03 % pro Seite
- BTC-CFD:                         Slippage ≈ 0.01–0.06 % pro Seite
- Round-Trip (Entry + Exit):       × 2
"""
from __future__ import annotations

from typing import Literal


def estimate_slippage(close: float, high: float, low: float) -> float:
    """Schätzt relative Slippage als Bruchteil des Preises aus OHLCV.

    `spread_proxy = (high − low) / close`  ist die intrabar-Range relativ
    zum Close. Multipliziert mit 0.5 ergibt sich die typische Halb-Spread-
    Crossing-Cost; ein Volatilitäts-Aufschlag (Faktor 0.2) modelliert
    erhöhten Slippage in volatilen Phasen.

    Gesamt:  `0.7 × (high − low) / close`
    """
    if close <= 0:
        return 0.0
    spread_proxy = (high - low) / close
    spread_cost = spread_proxy * 0.5
    vol_surcharge = spread_proxy * 0.2
    return spread_cost + vol_surcharge


def apply_slippage_entry(price: float, side: Literal["long", "short"],
                         high: float, low: float) -> float:
    """Slippage gegen den Entry — Long kauft teurer, Short verkauft günstiger."""
    slip = estimate_slippage(price, high, low)
    return price * (1 + slip) if side == "long" else price * (1 - slip)


def apply_slippage_exit(price: float, side: Literal["long", "short"],
                        high: float, low: float) -> float:
    """Slippage gegen den Exit — Long-Verkauf zu schlechterem (= tieferem) Preis,
    Short-Rückkauf zu schlechterem (= höherem) Preis."""
    slip = estimate_slippage(price, high, low)
    return price * (1 - slip) if side == "long" else price * (1 + slip)
