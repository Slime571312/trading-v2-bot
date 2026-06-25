"""Tests für Garman-Klass Volatility-Schätzer (Vault/Bot/Garman-Klass-Volatility.md)."""
import numpy as np
import pandas as pd

from src.strategy_core.garman_klass import (
    compute_continuous_vol_factor_gk,
    gk_per_bar,
    gk_rolling,
)


def _ohlc(n: int, oc_step: float = 0.0, hl_range: float = 1.0,
          start: float = 100.0) -> pd.DataFrame:
    """Helper: synthetische OHLC mit kontrollierter Range und Body-Größe."""
    idx = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    opens = np.array([start + i * oc_step for i in range(n)])
    closes = opens + oc_step
    highs = np.maximum(opens, closes) + hl_range / 2
    lows = np.minimum(opens, closes) - hl_range / 2
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1000.0] * n,
    }, index=idx)


# ── gk_per_bar ────────────────────────────────────────────────────────────


def test_gk_per_bar_zero_range_zero_vol():
    """Bar mit H == L == C == O → GK = 0."""
    df = pd.DataFrame({
        "open": [100], "high": [100], "low": [100], "close": [100],
        "volume": [1000.0],
    }, index=pd.date_range("2026-01-01", periods=1, freq="1D", tz="UTC"))
    s = gk_per_bar(df)
    assert s.iloc[0] == 0.0


def test_gk_per_bar_positive_on_normal_bar():
    """Normale OHLC-Bar → GK > 0."""
    df = _ohlc(5, oc_step=0.0, hl_range=2.0, start=100.0)
    s = gk_per_bar(df)
    assert (s > 0).all()


def test_gk_per_bar_higher_for_wider_range():
    """Größere High-Low-Spanne → höhere GK-Vol."""
    narrow = _ohlc(5, oc_step=0.0, hl_range=1.0, start=100.0)
    wide = _ohlc(5, oc_step=0.0, hl_range=5.0, start=100.0)
    assert gk_per_bar(wide).mean() > gk_per_bar(narrow).mean()


def test_gk_per_bar_handles_invalid_ohlc_without_nan():
    """Sehr starker Trend (großer Body): inside-term kann negativ werden → clamp auf 0."""
    df = pd.DataFrame({
        "open": [100.0], "high": [101.0], "low": [99.0], "close": [101.0],
        "volume": [1000.0],
    }, index=pd.date_range("2026-01-01", periods=1, freq="1D", tz="UTC"))
    s = gk_per_bar(df)
    assert not s.isna().any()
    assert (s >= 0).all()


# ── gk_rolling ────────────────────────────────────────────────────────────


def test_gk_rolling_smooths_per_bar_noise():
    """Rolling über 14 → glattere Kurve als Per-Bar."""
    df = _ohlc(50, oc_step=0.0, hl_range=2.0, start=100.0)
    # Inject spike
    df.iloc[20, df.columns.get_loc("high")] = 110.0
    df.iloc[20, df.columns.get_loc("low")] = 90.0
    per_bar = gk_per_bar(df)
    rolling = gk_rolling(df, window=14)
    # Spike sollte in per_bar deutlich sein, in rolling abgemildert
    assert per_bar.iloc[20] > rolling.iloc[20]


# ── compute_continuous_vol_factor_gk ──────────────────────────────────────


def test_gk_factor_neutral_when_stable():
    """Konstante Range → factor ≈ 1.0."""
    df = _ohlc(60, oc_step=0.0, hl_range=2.0, start=100.0)
    factor, cur, ref = compute_continuous_vol_factor_gk(df)
    assert 0.9 <= factor <= 1.1
    assert abs(cur - ref) < 0.01


def test_gk_factor_shrinks_on_vol_spike():
    """Lange ruhige Phase + jüngster Spike → factor < 1."""
    quiet = _ohlc(50, oc_step=0.0, hl_range=1.0, start=100.0)
    spike = _ohlc(10, oc_step=0.0, hl_range=10.0, start=100.0)
    df = pd.concat([quiet, spike])
    df.index = pd.date_range("2026-01-01", periods=len(df), freq="1D", tz="UTC")
    factor, cur, ref = compute_continuous_vol_factor_gk(df)
    assert factor < 0.95     # signifikante Reduktion
    assert cur > ref


def test_gk_factor_grows_on_vol_compression():
    """Lange volatile Phase + jüngste Beruhigung → factor > 1 (gecappt 1.3)."""
    loud = _ohlc(50, oc_step=0.0, hl_range=8.0, start=100.0)
    quiet = _ohlc(10, oc_step=0.0, hl_range=0.5, start=100.0)
    df = pd.concat([loud, quiet])
    df.index = pd.date_range("2026-01-01", periods=len(df), freq="1D", tz="UTC")
    factor, cur, ref = compute_continuous_vol_factor_gk(df)
    assert factor > 1.05
    assert factor <= 1.3
    assert cur < ref


def test_gk_factor_clamp_extreme_spike():
    """Extrem-Spike → genau am unteren Cap (0.5)."""
    quiet = _ohlc(50, oc_step=0.0, hl_range=0.5, start=100.0)
    extreme = _ohlc(10, oc_step=0.0, hl_range=50.0, start=100.0)
    df = pd.concat([quiet, extreme])
    df.index = pd.date_range("2026-01-01", periods=len(df), freq="1D", tz="UTC")
    factor, _, _ = compute_continuous_vol_factor_gk(df)
    assert factor == 0.5


def test_gk_factor_insufficient_data_neutral():
    """< 45 Bars → factor = 1.0 (neutral)."""
    df = _ohlc(20, oc_step=0.0, hl_range=2.0, start=100.0)
    factor, _, _ = compute_continuous_vol_factor_gk(df)
    assert factor == 1.0


# ── Cross-Check: GK vs ATR liefern qualitativ gleiche Richtung ──────────


def test_gk_and_atr_agree_on_spike_direction():
    """Beide Schätzer müssen bei einem Vol-Spike den factor unter 1 senken."""
    from src.strategy_core.volatility import compute_continuous_vol_factor
    quiet = _ohlc(50, oc_step=0.0, hl_range=1.0, start=100.0)
    spike = _ohlc(10, oc_step=0.0, hl_range=8.0, start=100.0)
    df = pd.concat([quiet, spike])
    df.index = pd.date_range("2026-01-01", periods=len(df), freq="1D", tz="UTC")
    f_gk, _, _ = compute_continuous_vol_factor(df, "BTC", use_gk=True)
    f_atr, _, _ = compute_continuous_vol_factor(df, "BTC", use_gk=False)
    assert f_gk < 1.0
    assert f_atr < 1.0
    # Beide gleichen Richtung; absolute Werte können differieren
