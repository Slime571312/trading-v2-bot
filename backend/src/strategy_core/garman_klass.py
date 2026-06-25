"""Garman-Klass Volatility — nutzt OHLC pro Bar statt nur True-Range.

Vault: Bot/Garman-Klass-Volatility.md. Kernformel:
    gk_per_bar = sqrt( 0.5 * ln(H/L)² - (2*ln(2)-1) * ln(C/O)² )

Vorteile ggü. ATR:
- Nutzt alle 4 OHLC-Werte statt nur 3
- Kein Lookback-Lag (kein prev_close nötig)
- Effizienter Schätzer (~5× weniger Daten für gleiche Präzision)
- Besser für 24/7-Märkte (Crypto) wo Overnight-Gap-Logik von ATR irrelevant ist

Für unseren Bot-Zweck (Vol-Sizing-Faktor) ist Annualisierung egal — wir nutzen
GK-Werte als Verhältnis: current / reference. Die Konstanten kürzen sich raus.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_GK_K = 2.0 * float(np.log(2)) - 1.0   # ≈ 0.3863


def gk_per_bar(df: pd.DataFrame) -> pd.Series:
    """Pro-Bar GK-Volatilität (nicht annualisiert).

    Returns pd.Series indexiert wie df. NaN bei Bars mit ungültiger OHLC.
    """
    if df.empty:
        return pd.Series(dtype=float)

    # Defensive: positive Preise sicherstellen — log(0) und log(neg) verhindern
    high = df["high"].astype(float).clip(lower=1e-12)
    low = df["low"].astype(float).clip(lower=1e-12)
    close = df["close"].astype(float).clip(lower=1e-12)
    open_ = df["open"].astype(float).clip(lower=1e-12)

    ln_hl = np.log(high / low)
    ln_co = np.log(close / open_)

    inside = 0.5 * ln_hl**2 - _GK_K * ln_co**2
    # Für sehr starke Trend-Bars kann der Innenausdruck minimal negativ werden
    # (mathematische Eigenschaft der GK-Schätzung). Negativwerte sind Artefakt;
    # auf 0 clampen statt NaN zu erzeugen.
    inside = inside.clip(lower=0.0)
    gk = np.sqrt(inside)
    return pd.Series(gk, index=df.index)


def gk_rolling(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Rolling-Mittel der GK-Per-Bar-Vol über `window` Bars (= „GK-Vol")."""
    return gk_per_bar(df).rolling(window=window, min_periods=max(2, window // 2)).mean()


def compute_continuous_vol_factor_gk(
    df: pd.DataFrame,
    reference_window: int = 30,
    gk_window: int = 14,
    min_factor: float = 0.5,
    max_factor: float = 1.3,
) -> tuple[float, float, float]:
    """GK-basierte Version von compute_continuous_vol_factor (Sprint #1).

    Returns (size_factor, current_gk, reference_gk). Drop-in für die ATR-Variante.
    """
    needed = gk_window + reference_window + 1
    if len(df) < needed:
        return (1.0, 0.0, 0.0)

    gk_series = gk_rolling(df, window=gk_window).dropna()
    if len(gk_series) < reference_window + 1:
        cur = float(gk_series.iloc[-1]) if len(gk_series) > 0 else 0.0
        return (1.0, cur, cur)

    current_gk = float(gk_series.iloc[-1])
    reference_gk = float(gk_series.iloc[-(reference_window + 1):-1].mean())

    if current_gk <= 0 or reference_gk <= 0:
        return (1.0, current_gk, reference_gk)

    ratio = reference_gk / current_gk
    size_factor = max(min_factor, min(max_factor, ratio))
    return (size_factor, current_gk, reference_gk)
