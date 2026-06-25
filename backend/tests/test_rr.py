"""Tests für rr.py — RR-Berechnung + OTE-Funktionen."""
import pytest
from src.strategy_core.rr import (
    OTEZone,
    calculate_ote_zone,
    fib_tp_extension,
    is_in_ote_zone,
    ote_confluence_entry,
    rr_ratio,
)


# ── Existing RR-Ratio Tests ──────────────────────────────────────────────────

def test_rr_ratio_long_valid():
    assert abs(rr_ratio("long", 100, 95, 110) - 2.0) < 1e-9


def test_rr_ratio_short_valid():
    assert abs(rr_ratio("short", 100, 105, 90) - 2.0) < 1e-9


def test_rr_ratio_invalid_returns_zero():
    assert rr_ratio("long", 100, 105, 110) == 0.0   # SL above entry
    assert rr_ratio("long", 100, 95, 99) == 0.0     # TP below entry


# ── OTE-Zone ─────────────────────────────────────────────────────────────────

def test_calculate_ote_zone_bullish():
    # Leg: 100 → 120, bias=1 (bullish)
    zone = calculate_ote_zone(100, 120, 1)
    assert isinstance(zone, OTEZone)
    assert abs(zone.ote_low - (120 - 20 * 0.79)) < 1e-9    # 104.2
    assert abs(zone.ote_705 - (120 - 20 * 0.705)) < 1e-9   # 105.9
    assert abs(zone.ote_high - (120 - 20 * 0.62)) < 1e-9   # 107.6
    assert zone.sl_ref == 100


def test_calculate_ote_zone_bearish():
    # Leg: 120 → 100, bias=-1 (bearish)
    zone = calculate_ote_zone(100, 120, -1)
    assert abs(zone.ote_low - (100 + 20 * 0.62)) < 1e-9    # 112.4
    assert abs(zone.ote_705 - (100 + 20 * 0.705)) < 1e-9   # 114.1
    assert abs(zone.ote_high - (100 + 20 * 0.79)) < 1e-9   # 115.8
    assert zone.sl_ref == 120


def test_is_in_ote_zone_true_at_sweet_spot():
    zone = calculate_ote_zone(100, 120, 1)  # ote_low=104.2, ote_high=107.6
    assert is_in_ote_zone(105.9, zone)   # sweet spot


def test_is_in_ote_zone_true_at_bounds():
    zone = calculate_ote_zone(100, 120, 1)
    assert is_in_ote_zone(104.2, zone)   # lower bound
    assert is_in_ote_zone(107.6, zone)   # upper bound


def test_is_in_ote_zone_false_outside():
    zone = calculate_ote_zone(100, 120, 1)
    assert not is_in_ote_zone(108.0, zone)   # above zone (entry too shallow)
    assert not is_in_ote_zone(103.0, zone)   # below zone (entry too deep)


# ── Fib-Extension ────────────────────────────────────────────────────────────

def test_fib_tp_extension_bullish():
    # Leg: 100 → 120, bias=1
    tp1 = fib_tp_extension(100, 120, -0.27, 1)  # 120 + 20*0.27 = 125.4
    tp2 = fib_tp_extension(100, 120, -0.62, 1)  # 120 + 20*0.62 = 132.4
    assert abs(tp1 - 125.4) < 1e-9
    assert abs(tp2 - 132.4) < 1e-9


def test_fib_tp_extension_bearish():
    # Leg: 120 → 100, bias=-1
    tp1 = fib_tp_extension(100, 120, -0.27, -1)  # 100 - 20*0.27 = 94.6
    tp2 = fib_tp_extension(100, 120, -0.62, -1)  # 100 - 20*0.62 = 87.6
    assert abs(tp1 - 94.6) < 1e-9
    assert abs(tp2 - 87.6) < 1e-9


def test_fib_tp_extension_tp2_further_than_tp1():
    tp1 = fib_tp_extension(100, 120, -0.27, 1)
    tp2 = fib_tp_extension(100, 120, -0.62, 1)
    assert tp2 > tp1  # TP2 always further for bullish


# ── OTE-Confluence ───────────────────────────────────────────────────────────

def test_ote_confluence_entry_true():
    # OTE zone: 104.2–107.6; OB zone: 104–107 → midpoint 105.5 in both
    zone = calculate_ote_zone(100, 120, 1)
    assert ote_confluence_entry(105.5, zone, 104.0, 107.0)


def test_ote_confluence_entry_false_price_not_in_ote():
    # OB at 108–112 (above OTE zone 104.2–107.6)
    zone = calculate_ote_zone(100, 120, 1)
    assert not ote_confluence_entry(110.0, zone, 108.0, 112.0)


def test_ote_confluence_entry_false_price_not_in_ob():
    # Price 105.5 in OTE zone, but OB is 108–112 → not in OB
    zone = calculate_ote_zone(100, 120, 1)
    assert not ote_confluence_entry(105.5, zone, 108.0, 112.0)


def test_ote_confluence_entry_bearish():
    # Bearish: OTE zone 112.4–115.8; OB 112–116 → midpoint 114 in both
    zone = calculate_ote_zone(100, 120, -1)
    assert ote_confluence_entry(114.0, zone, 112.0, 116.0)
