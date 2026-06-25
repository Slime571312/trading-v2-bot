"""Tests für SMT-Divergence + Volatility-Regime + Korrelations-Gate (Punkte 8-10).

Drei Module die Conviction und Portfolio-Risk steuern:
- volatility.compute_volatility_regime()
- smt.detect_smt_divergence()
- trade_limits.correlation_gate_blocked()
"""
import numpy as np
import pandas as pd
import pytest

from src.live.trade_limits import correlation_gate_blocked
from src.strategy_core.smt import detect_smt_divergence, get_reference_instrument
from src.strategy_core.volatility import (
    compute_continuous_vol_factor,
    compute_volatility_regime, size_factor_for_regime,
)


def _bars(closes: list[float], highs: list[float] | None = None,
          lows: list[float] | None = None) -> pd.DataFrame:
    """Helper: baut OHLC-DataFrame aus simplen Listen."""
    n = len(closes)
    if highs is None:
        highs = [c + 0.5 for c in closes]
    if lows is None:
        lows = [c - 0.5 for c in closes]
    idx = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    return pd.DataFrame({
        "open": closes, "high": highs, "low": lows,
        "close": closes, "volume": [1000.0] * n,
    }, index=idx)


# ── Volatility-Regime ────────────────────────────────────────────────────

def test_vol_regime_normal_btc():
    """Stabile 1%-Range pro Bar → normal-regime."""
    closes = [50000 + i * 100 for i in range(30)]
    highs = [c + 800 for c in closes]   # ~1.6% range
    lows = [c - 800 for c in closes]
    df = _bars(closes, highs, lows)
    regime, atr_pct = compute_volatility_regime(df, "BTC")
    assert regime == "normal"
    assert 0.01 < atr_pct < 0.04


def test_vol_regime_low_indices():
    """Sehr ruhige Bars unter SP500-Low-Schwelle (<0.4%)."""
    closes = [4500 + np.sin(i / 5) * 5 for i in range(30)]
    highs = [c + 5 for c in closes]    # ~0.1% range
    lows = [c - 5 for c in closes]
    df = _bars(closes, highs, lows)
    regime, _ = compute_volatility_regime(df, "SP500")
    assert regime == "low"


def test_vol_regime_high_btc():
    """Sehr volatile BTC-Bars über 3.5%-Schwelle."""
    closes = [50000 + i * 1000 for i in range(30)]
    highs = [c + 2500 for c in closes]   # ~5% range
    lows = [c - 2500 for c in closes]
    df = _bars(closes, highs, lows)
    regime, atr_pct = compute_volatility_regime(df, "BTC")
    assert regime == "high"
    assert atr_pct > 0.035


def test_vol_size_factor_high_reduces_position():
    """High-Vol → kleinere Size."""
    assert size_factor_for_regime("low") == 1.0
    assert size_factor_for_regime("normal") == 1.0
    assert size_factor_for_regime("high") < 1.0


# ── Continuous Vol-Factor (Vault: Volatility-Based-Position-Sizing.md) ────

def test_continuous_vol_factor_neutral_when_stable():
    """Konstante Range über lange Historie → current ≈ reference → factor ≈ 1.0."""
    closes = [50000 + (i % 3) * 50 for i in range(60)]
    highs = [c + 500 for c in closes]
    lows = [c - 500 for c in closes]
    df = _bars(closes, highs, lows)
    factor, cur, ref = compute_continuous_vol_factor(df, "BTC")
    assert 0.9 < factor < 1.1
    assert cur > 0 and ref > 0
    assert abs(cur - ref) < 0.002


def test_continuous_vol_factor_shrinks_on_vol_spike():
    """Lange ruhige Phase + jüngster Vol-Spike → factor deutlich < 1."""
    # 50 Bars mit kleiner Range, dann 10 Bars mit großem Range
    quiet_closes = [50000 + i for i in range(50)]
    quiet_highs = [c + 200 for c in quiet_closes]
    quiet_lows = [c - 200 for c in quiet_closes]
    spike_closes = [50050 + i * 500 for i in range(10)]
    spike_highs = [c + 2500 for c in spike_closes]
    spike_lows = [c - 2500 for c in spike_closes]
    df = _bars(quiet_closes + spike_closes,
               quiet_highs + spike_highs,
               quiet_lows + spike_lows)
    factor, cur, ref = compute_continuous_vol_factor(df, "BTC")
    assert factor < 0.8     # signifikante Reduktion
    assert cur > ref        # jüngste ATR% > Referenz-ATR%


def test_continuous_vol_factor_grows_on_vol_compression():
    """Lange volatile Phase + jüngste Beruhigung → factor > 1 (gecappt 1.3)."""
    # 50 Bars groß, dann 10 Bars klein
    loud_closes = [50000 + i * 100 for i in range(50)]
    loud_highs = [c + 1500 for c in loud_closes]
    loud_lows = [c - 1500 for c in loud_closes]
    quiet_closes = [55000 + i for i in range(10)]
    quiet_highs = [c + 100 for c in quiet_closes]
    quiet_lows = [c - 100 for c in quiet_closes]
    df = _bars(loud_closes + quiet_closes,
               loud_highs + quiet_highs,
               loud_lows + quiet_lows)
    factor, cur, ref = compute_continuous_vol_factor(df, "BTC")
    assert factor > 1.1
    assert factor <= 1.3    # Cap einhalten
    assert cur < ref


def test_continuous_vol_factor_clamp_extreme_spike():
    """Extreme Vol-Spike darf nicht unter min_factor=0.5 fallen."""
    quiet_closes = [50000 + i for i in range(50)]
    quiet_highs = [c + 100 for c in quiet_closes]
    quiet_lows = [c - 100 for c in quiet_closes]
    spike_closes = [50050 + i * 2000 for i in range(10)]
    spike_highs = [c + 8000 for c in spike_closes]    # extrem
    spike_lows = [c - 8000 for c in spike_closes]
    df = _bars(quiet_closes + spike_closes,
               quiet_highs + spike_highs,
               quiet_lows + spike_lows)
    factor, _, _ = compute_continuous_vol_factor(df, "BTC")
    assert factor == 0.5    # genau am unteren Cap


def test_continuous_vol_factor_insufficient_data_neutral():
    """< 45 Bars (period + reference_window) → neutraler Faktor 1.0."""
    closes = [50000 + i for i in range(20)]
    df = _bars(closes)
    factor, _, _ = compute_continuous_vol_factor(df, "BTC")
    assert factor == 1.0


# ── SMT-Divergence ───────────────────────────────────────────────────────

def test_smt_reference_mapping():
    assert get_reference_instrument("BTC") == "ETH"
    assert get_reference_instrument("NASDAQ") == "SP500"
    assert get_reference_instrument("SP500") == "NASDAQ"
    assert get_reference_instrument("UNKNOWN") is None


def test_smt_long_confirms_when_main_lower_low_ref_higher_low():
    """Main fällt auf neues Tief, Reference fällt nicht — Smart-Money kauft."""
    # Main: low geht runter (100 → 90)
    main = _bars([100, 98, 95, 92, 90])
    # Ref: low bleibt stabil oder steigt (100 → 102) — KEIN neues Tief
    ref = _bars([100, 101, 102, 102, 102])
    result = detect_smt_divergence(main, ref, side="long", lookback=5)
    assert result == "confirms"


def test_smt_long_diverges_when_main_higher_high_ref_lower_high():
    """Main macht Higher High, Reference nicht — Bullish-Move ohne Bestätigung."""
    main = _bars([100, 101, 102, 103, 105], highs=[101, 102, 103, 104, 106])
    ref = _bars([100, 100, 100, 100, 100], highs=[100.5, 100.5, 100.5, 100.5, 100.5])
    result = detect_smt_divergence(main, ref, side="long", lookback=5)
    assert result == "diverges"


def test_smt_short_confirms_when_main_higher_high_ref_lower_high():
    """Short-Setup: Main macht neues High, Ref nicht — Stop-Hunt nach oben."""
    main = _bars([100, 102, 104, 106, 108], highs=[101, 103, 105, 107, 109])
    ref = _bars([100, 100, 100, 100, 100], highs=[100.5, 100.5, 100.5, 100.5, 100.5])
    result = detect_smt_divergence(main, ref, side="short", lookback=5)
    assert result == "confirms"


def test_smt_neutral_when_both_flat():
    flat = _bars([100, 100, 100, 100, 100])
    assert detect_smt_divergence(flat, flat, "long", lookback=5) == "neutral"


def test_smt_neutral_with_too_few_bars():
    short_main = _bars([100, 101])
    short_ref = _bars([100, 101])
    assert detect_smt_divergence(short_main, short_ref, "long", lookback=5) == "neutral"


# ── Korrelations-Gate ────────────────────────────────────────────────────

def test_correlation_gate_blocks_third_index():
    """DE40+NASDAQ schon offen → SP500 wird blockiert (max 2/3)."""
    blocked, reason = correlation_gate_blocked("SP500", ["DE40", "NASDAQ"])
    assert blocked is True
    assert "indices_full" in (reason or "")


def test_correlation_gate_allows_second_index():
    """Nur DE40 offen → NASDAQ darf öffnen (1/2)."""
    blocked, reason = correlation_gate_blocked("NASDAQ", ["DE40"])
    assert blocked is False
    assert reason is None


def test_correlation_gate_ignores_btc():
    """BTC ist in keiner Index-Gruppe → niemals geblockt."""
    blocked, _ = correlation_gate_blocked("BTC", ["DE40", "NASDAQ", "SP500"])
    assert blocked is False


def test_correlation_gate_ignores_non_index_open_lists():
    """BTC offen zählt nicht für Index-Cluster."""
    blocked, _ = correlation_gate_blocked("DE40", ["BTC"])
    assert blocked is False
