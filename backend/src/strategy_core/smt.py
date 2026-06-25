"""SMT-Divergence (Smart Money Tool) — Conviction-Punkt im Bot/Conviction-Score.md.

**Konzept (Vault):** Korrelierte Assets sollten gleichzeitig Higher Highs / Lower
Lows machen. Wenn das Haupt-Asset einen neuen Extreme bildet, das Referenz-Asset
aber NICHT, ist das eine Smart-Money-Manipulation (Stop-Hunt im Haupt) ohne
echte Trend-Stärke → der Move ist verdächtig.

**Bullish-Setup divergiert positiv** (= Confluence):
  Main macht Lower Low, Reference macht Higher Low → Smart-Money kauft trotz Stop-Hunt
**Bullish-Setup divergiert negativ** (= Kontradiktion):
  Main macht Higher High, Reference macht Lower High → Bullish Move ohne Bestätigung

Spiegelbildlich für Short.

**Reference-Assets** (per Vault + 1:1 Korrelation):
- BTC → ETH (Crypto-Twin via Binance)
- NASDAQ → SP500 (US-Indizes via Capital)
- SP500  → NASDAQ
- DE40   → SP500 (US dominiert globale Risk-On/Off)

Aktuell deaktiviert in der Engine (`skip` da reference-Daten-Fetcher noch nicht
in den Tick-Loop integriert). Diese Modul liefert die *Logik* — `engine.py` muss
in Etappe 4 die Reference-Bars laden und übergeben.
"""
from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from ._types import Side

SMTResult = Literal["confirms", "diverges", "neutral"]


# Reference-Asset-Mapping (Haupt → Reference)
SMT_REFERENCE: dict[str, str] = {
    "BTC":    "ETH",      # Crypto-Twin
    "NASDAQ": "SP500",    # US-Risk-On Korrelation
    "SP500":  "NASDAQ",
    "DE40":   "SP500",    # globale Risk-On/Off
}


def detect_smt_divergence(
    main_df: pd.DataFrame,
    ref_df: pd.DataFrame,
    side: Side,
    lookback: int = 5,
) -> SMTResult:
    """Vergleicht die letzten `lookback` Bars von main_df und ref_df.

    - Long-Side: bullish wenn main Lower Low macht aber ref Higher Low (confirms).
                 bearish wenn main Higher High macht aber ref Lower High (diverges).
    - Short-Side: spiegelbildlich.

    Returns:
      - "confirms" — Setup-Richtung wird durch Divergence bestätigt
      - "diverges" — Setup-Richtung wird durch Divergence widerlegt
      - "neutral"  — keine klare Aussage (zu wenig Bars, identische Extreme)
    """
    if len(main_df) < lookback or len(ref_df) < lookback:
        return "neutral"

    # Auf gemeinsame Timestamps reduzieren (gleicher Zeitfenster-Vergleich)
    merged = main_df.tail(lookback).join(
        ref_df.tail(lookback).add_suffix("_ref"),
        how="inner",
    )
    if len(merged) < 3:
        return "neutral"

    main_lows = merged["low"].to_numpy(dtype=float)
    main_highs = merged["high"].to_numpy(dtype=float)
    ref_lows = merged["low_ref"].to_numpy(dtype=float)
    ref_highs = merged["high_ref"].to_numpy(dtype=float)

    if side == "long":
        # Bullish Setup: brauchen Sweep nach unten ⇒ jüngeres Low < älteres Low.
        # Wenn Main neues Low macht, Ref aber NICHT → confirms (Smart-Money divergiert positiv)
        main_new_low = main_lows[-1] < main_lows[0]
        ref_new_low = ref_lows[-1] < ref_lows[0]
        if main_new_low and not ref_new_low:
            return "confirms"
        # Wenn Main NEW HIGH macht aber Ref NICHT → diverges (Move ohne Bestätigung)
        main_new_high = main_highs[-1] > main_highs[0]
        ref_new_high = ref_highs[-1] > ref_highs[0]
        if main_new_high and not ref_new_high:
            return "diverges"
        return "neutral"
    else:  # short
        main_new_high = main_highs[-1] > main_highs[0]
        ref_new_high = ref_highs[-1] > ref_highs[0]
        if main_new_high and not ref_new_high:
            return "confirms"
        main_new_low = main_lows[-1] < main_lows[0]
        ref_new_low = ref_lows[-1] < ref_lows[0]
        if main_new_low and not ref_new_low:
            return "diverges"
        return "neutral"


def get_reference_instrument(instrument: str) -> str | None:
    """Reference-Asset für SMT (oder None wenn nicht definiert)."""
    return SMT_REFERENCE.get(instrument)
