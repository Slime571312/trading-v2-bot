"""Tests für zones — FVG / OB / Equilibrium."""
from src.strategy_core._types import StructureBreak, Sweep, Swing
from src.strategy_core.zones import (
    compute_equilibrium, find_fvgs, find_order_block, in_eq_zone,
)
from tests.helpers import make_bars

import pandas as pd


def test_bullish_fvg_detected():
    # 3-Kerzen-Pattern: Bar 1 high=100, Bar 2 impulsive up, Bar 3 low=102 → Gap (100, 102)
    df = make_bars([
        (98, 100, 97, 99),
        (99, 105, 99, 104),    # impulsive
        (104, 106, 102, 105),  # low=102 > 100 (Bar0-high) → FVG-Range (100, 102)
        (105, 107, 104, 106),
    ])
    fvgs = find_fvgs(df, start_idx=1, end_idx=2, direction="long")
    assert len(fvgs) == 1
    assert fvgs[0].lower == 100.0
    assert fvgs[0].upper == 102.0


def test_bearish_fvg_detected():
    # Bar 3 high so groß dass bei i=2 KEIN zweiter FVG entsteht
    df = make_bars([
        (105, 107, 104, 106),
        (106, 106, 100, 101),   # impulsive down
        (101, 99, 96, 97),      # high=99 < 104 (Bar0-low) → FVG bei i=1: (99, 104)
        (97, 102, 96, 100),     # high=102 ≥ low[1]=100 → kein 2. FVG bei i=2
    ])
    fvgs = find_fvgs(df, start_idx=1, end_idx=2, direction="short")
    assert len(fvgs) == 1
    assert fvgs[0].lower == 99.0
    assert fvgs[0].upper == 104.0


def test_order_block_finds_last_counter_candle():
    # Bullish-BOS — OB ist die letzte bearishe Kerze (close<open) vor dem Sweep
    df = make_bars([
        (100, 102, 99, 101),    # 0 bull
        (101, 104, 100, 103),   # 1 bull
        (103, 105, 100, 99),    # 2 bear ← Last counter candle
        (99, 100, 95, 96),      # 3 bear   ← Also counter — the LATEST counter is found first scanning backward
        (96, 99, 95, 97),       # 4 bull
        (97, 110, 96, 99),      # 5 sweep (e.g. of some other level)
    ])
    sweep = Sweep(
        time=df.index[5], bar_idx=5, level=99.0,
        swing=Swing(time=df.index[0], price=99.0, kind="low", bar_idx=0),
        direction="ssl", tf="1h",
    )
    bos = StructureBreak(
        time=df.index[5], bar_idx=5, body_extreme=99.0,
        broken_swing=Swing(time=df.index[0], price=99.0, kind="high", bar_idx=0),
        direction="long",
    )
    ob = find_order_block(df, sweep, bos)
    assert ob is not None
    assert ob.direction == "long"
    # latest counter (bear) candle from sweep.bar_idx backwards is bar 3
    assert ob.bar_idx == 3


def test_equilibrium_is_midpoint():
    df = make_bars([
        (100, 102, 95, 101),   # sweep bar — sweep_low = 95
        (101, 104, 100, 103),
        (103, 110, 102, 109),  # bos bar — body_extreme = 109
    ])
    sweep = Sweep(
        time=df.index[0], bar_idx=0, level=95.0,
        swing=Swing(time=df.index[0], price=95.0, kind="low", bar_idx=0),
        direction="ssl", tf="1h",
    )
    bos = StructureBreak(
        time=df.index[2], bar_idx=2, body_extreme=109.0,
        broken_swing=Swing(time=df.index[0], price=95.0, kind="high", bar_idx=0),
        direction="long",
    )
    eq = compute_equilibrium(sweep, bos, df)
    assert eq.swing_low == 95.0
    assert eq.swing_high == 109.0
    assert eq.eq == (95.0 + 109.0) / 2.0
    assert in_eq_zone(100.0, eq, "long") is True   # below eq=102 → discount
    assert in_eq_zone(105.0, eq, "long") is False  # above eq → premium
