"""Volatilitäts-Regime — ATR-basierter Proxy für VIX.

Capital.com liefert keinen VIX, aber `ATR(14) / Close` auf Daily-Bars ist ein
solider Proxy für Realized Volatility. Daraus klassifizieren wir drei Regime:

- **low**    — Markt ruhig, Mean-Reversion dominiert, Runner gelingt selten
- **normal** — Standard, alle Setups normal
- **high**   — volatile Bewegungen, Setups breit, aber auch False-Breakouts häufig

Effekt auf die Strategie (Vault/Bot/TP-Staffelung.md):
- `high`: TP3-Runner-Toleranz höher (mehr Range), aber Size kleiner (Risk-Adj)
- `low`:  kein Runner — Markt erreicht 3R+ nicht
- `normal`: Default-Verhalten

Schwellen pro Instrument kalibriert — BTC ist deutlich volatiler als Indizes,
absolute ATR-%-Werte sind nicht vergleichbar.
"""
from __future__ import annotations

from typing import Literal

import pandas as pd

from .rr import _atr

VolatilityRegime = Literal["low", "normal", "high"]


# ATR%-Schwellen (= ATR / Close-Preis). Empirische Werte aus Vault-Notes +
# typischen Marktdaten 2024-2026.
_ATR_PCT_THRESHOLDS: dict[str, tuple[float, float]] = {
    # (low/normal, normal/high)
    "BTC":    (0.015, 0.035),   # 1.5% bis 3.5% — BTC ist von Natur aus volatil
    "DE40":   (0.005, 0.012),
    "NASDAQ": (0.005, 0.013),
    "SP500":  (0.004, 0.010),
}


def compute_volatility_regime(
    daily_df: pd.DataFrame,
    instrument: str,
) -> tuple[VolatilityRegime, float]:
    """Returns (regime, atr_pct).

    `atr_pct` ist die rohe Kennzahl (ATR/Close). Bei zu wenig Bars → ("normal", 0.0).
    """
    if len(daily_df) < 15:
        return ("normal", 0.0)
    atr = _atr(daily_df, period=14)
    last_close = float(daily_df["close"].iloc[-1])
    if last_close <= 0 or atr <= 0:
        return ("normal", 0.0)
    atr_pct = atr / last_close
    low_th, high_th = _ATR_PCT_THRESHOLDS.get(instrument, (0.005, 0.012))
    if atr_pct < low_th:
        return ("low", atr_pct)
    if atr_pct > high_th:
        return ("high", atr_pct)
    return ("normal", atr_pct)


def size_factor_for_regime(regime: VolatilityRegime) -> float:
    """Position-Size-Multiplikator nach Regime (Risk-Adjusted-Sizing).

    Legacy 3-Tier (discrete). Aktuell genutzt nur als Fallback und für Tests —
    der Engine nutzt jetzt `compute_continuous_vol_factor` für eine gleitende
    Skala statt diskreter Stufen.
    """
    return {"low": 1.0, "normal": 1.0, "high": 0.7}[regime]


# Continuous Vol-Sizing (Vault/Bot/Volatility-Based-Position-Sizing.md):
# size_factor = clamp(reference_atr / current_atr, MIN, MAX).
# Vorteil ggü. 3-Tier: gleitender Übergang statt Cliff bei der Schwelle.
_CONTINUOUS_VOL_MIN = 0.5    # in extremer Vol max 50% Size
_CONTINUOUS_VOL_MAX = 1.3    # bei sehr ruhigem Markt max 130%
_REFERENCE_WINDOW_BARS = 30  # 30 Tage rollender Mittel-ATR% als „normal"


def compute_continuous_vol_factor(
    daily_df: pd.DataFrame,
    instrument: str,
    reference_window: int = _REFERENCE_WINDOW_BARS,
    min_factor: float = _CONTINUOUS_VOL_MIN,
    max_factor: float = _CONTINUOUS_VOL_MAX,
    use_gk: bool = True,
) -> tuple[float, float, float]:
    """Gleitender Vol-Size-Factor. Returns (size_factor, current_vol, reference_vol).

    Reference-Vol = rollender Mittel-Vol der letzten `reference_window` Daily-Bars.
    Wenn aktueller Markt turbulenter als Norm → factor < 1, sonst > 1 (gecappt).

    use_gk: nutzt Garman-Klass (OHLC-basiert, effizienter, default) statt ATR%.
    Beide Pfade liefern qualitativ identische Faktoren — GK ist präziser, ATR%
    bleibt als Fallback im Code für Tests + Robustheit.

    Bei zu wenig Daten: (1.0, current_vol, current_vol).
    """
    if use_gk:
        # Garman-Klass — separate Implementierung in garman_klass.py
        from .garman_klass import compute_continuous_vol_factor_gk
        return compute_continuous_vol_factor_gk(
            daily_df,
            reference_window=reference_window,
            min_factor=min_factor,
            max_factor=max_factor,
        )
    period = 14
    needed = period + reference_window + 1
    if len(daily_df) < needed:
        # Nicht genug Historie → neutraler Faktor
        last_close = float(daily_df["close"].iloc[-1]) if len(daily_df) > 0 else 0.0
        current_atr = _atr(daily_df, period=period)
        cur_pct = current_atr / last_close if last_close > 0 else 0.0
        return (1.0, cur_pct, cur_pct)

    # Vectorisiert: True-Range pro Bar berechnen, ATR-Serie als rolling mean,
    # ATR%-Serie pro Bar = ATR / Close.
    high = daily_df["high"].astype(float)
    low = daily_df["low"].astype(float)
    close = daily_df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.rolling(period).mean()
    atr_pct_series = (atr_series / close).dropna()

    if len(atr_pct_series) < reference_window + 1:
        cur_pct = float(atr_pct_series.iloc[-1]) if len(atr_pct_series) > 0 else 0.0
        return (1.0, cur_pct, cur_pct)

    current_atr_pct = float(atr_pct_series.iloc[-1])
    # Reference = SMA der letzten reference_window Bars (ohne den aktuellen, damit
    # ein einzelner Vol-Spike sich nicht selbst-normiert).
    reference_atr_pct = float(atr_pct_series.iloc[-(reference_window + 1):-1].mean())

    if current_atr_pct <= 0 or reference_atr_pct <= 0:
        return (1.0, current_atr_pct, reference_atr_pct)

    ratio = reference_atr_pct / current_atr_pct
    size_factor = max(min_factor, min(max_factor, ratio))
    return (size_factor, current_atr_pct, reference_atr_pct)
