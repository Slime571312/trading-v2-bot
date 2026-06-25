"""Tests für slippage — Spec aus Trading/Bot/Backtest-Methodik.md."""
import pytest

from src.backtest.slippage import (
    apply_slippage_entry, apply_slippage_exit, estimate_slippage,
)


def test_estimate_slippage_zero_range_unknown_instrument():
    # Unbekanntes Instrument + Zero-Range → 3 bps Fallback / 2 = 1.5 bps
    assert estimate_slippage(100.0, 100.0, 100.0) == pytest.approx(0.00015)


def test_estimate_slippage_typical_de40():
    # DE40 SPREAD_ABS=1.5pt bei 25272 → 1.5/2 / 25272 ≈ 0.0000297 = 0.003%
    slip = estimate_slippage(25272.0, 25290.0, 25260.0, instrument="DE40")
    assert 0.00002 < slip < 0.0001


def test_estimate_slippage_btc_realistic_price():
    # BTC SPREAD_ABS=50 USD bei 76900 → 50/2 / 76900 ≈ 0.000325
    slip = estimate_slippage(76900.0, 77000.0, 76800.0, instrument="BTC")
    assert 0.0002 < slip < 0.0006


def test_estimate_slippage_btc_widening_in_high_volatility():
    """Bei sehr volatiler Bar (range > base_spread) dominiert range × 5%."""
    # Range 5000 USD → max(50, 5000*0.05=250) = 250 → /2 / 76900 ≈ 0.00163
    slip = estimate_slippage(76900.0, 79000.0, 74000.0, instrument="BTC")
    assert slip > 0.001  # range-getrieben, deutlich höher als Base


def test_entry_slippage_long_pays_more():
    # Long-Entry: zahlt Slippage drauf → höherer Preis
    slipped = apply_slippage_entry(100.0, "long", 101.0, 99.0)
    assert slipped > 100.0


def test_entry_slippage_short_gets_less():
    # Short-Entry: bekommt weniger → niedrigerer Preis
    slipped = apply_slippage_entry(100.0, "short", 101.0, 99.0)
    assert slipped < 100.0


def test_exit_slippage_long_gets_less():
    # Long-Exit (verkauf): bekommt weniger als ideal
    slipped = apply_slippage_exit(100.0, "long", 101.0, 99.0)
    assert slipped < 100.0


def test_exit_slippage_short_pays_more():
    # Short-Exit (Rückkauf): zahlt mehr als ideal
    slipped = apply_slippage_exit(100.0, "short", 101.0, 99.0)
    assert slipped > 100.0


def test_slippage_invariants():
    # Entry und Exit-Slippage zusammen erzeugen immer einen Verlust für die Position
    # (= round-trip cost), egal welche Side
    for side in ("long", "short"):
        entry = apply_slippage_entry(100.0, side, 101.0, 99.0)
        exit_ = apply_slippage_exit(100.0, side, 101.0, 99.0)
        direction = 1 if side == "long" else -1
        rt_pnl = (exit_ - entry) * direction
        assert rt_pnl < 0, f"round-trip pnl bei zero-move sollte negativ sein ({side})"
