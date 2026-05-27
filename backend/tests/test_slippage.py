"""Tests für slippage — Spec aus Trading/Bot/Backtest-Methodik.md."""
import pytest

from src.backtest.slippage import (
    apply_slippage_entry, apply_slippage_exit, estimate_slippage,
)


def test_estimate_slippage_zero_range():
    # Keine Range = keine Slippage
    assert estimate_slippage(100.0, 100.0, 100.0) == 0.0


def test_estimate_slippage_typical_index():
    # Typischer DE40-Bar: 25272 Close, Range 30 Pkt → ~0.083 % Slippage
    slip = estimate_slippage(25272.0, 25290.0, 25260.0)
    # spread_proxy = 30/25272 ≈ 0.00119 → × 0.7 ≈ 0.00083
    assert 0.0005 < slip < 0.002


def test_estimate_slippage_volatile_btc():
    # BTC-Bar mit 200-Pkt Range: höhere Slippage
    slip = estimate_slippage(76900.0, 77000.0, 76800.0)
    # spread_proxy = 200/76900 ≈ 0.0026 → × 0.7 ≈ 0.00182
    assert 0.001 < slip < 0.003


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
