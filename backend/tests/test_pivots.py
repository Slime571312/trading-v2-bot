"""Tests für pivots.find_pivots — Pivot-High + Pivot-Low Detection."""
from src.strategy_core.pivots import find_pivots
from tests.helpers import make_bars


def test_pivot_high_in_middle():
    # 7 Bars: Bar 3 hat klares Pivot-High (high=110, alle anderen <110)
    df = make_bars([
        (100, 102, 99, 101),
        (101, 103, 100, 102),
        (102, 104, 101, 103),
        (103, 110, 102, 104),   # ← Pivot-High
        (104, 105, 102, 103),
        (103, 104, 100, 101),
        (101, 102, 98, 100),
    ])
    pivots = find_pivots(df, n_left=2, n_right=2)
    highs = [p for p in pivots if p.kind == "high"]
    assert len(highs) == 1
    assert highs[0].bar_idx == 3
    assert highs[0].price == 110.0


def test_pivot_low_in_middle():
    df = make_bars([
        (100, 102, 99, 101),
        (101, 103, 100, 102),
        (102, 104, 101, 103),
        (103, 104, 90, 96),     # ← Pivot-Low
        (96, 101, 95, 100),
        (100, 103, 99, 102),
        (102, 105, 101, 104),
    ])
    pivots = find_pivots(df, n_left=2, n_right=2)
    lows = [p for p in pivots if p.kind == "low"]
    assert len(lows) == 1
    assert lows[0].bar_idx == 3
    assert lows[0].price == 90.0


def test_too_few_bars_returns_empty():
    df = make_bars([(100, 101, 99, 100), (100, 101, 99, 100)])
    assert find_pivots(df, n_left=3, n_right=3) == []


def test_no_strict_pivot_means_no_detection():
    # Plateau-Highs — gleicher Wert links/rechts → kein Pivot (strikt!)
    df = make_bars([(100, 110, 99, 105)] * 7)
    pivots = find_pivots(df, n_left=2, n_right=2)
    assert pivots == []
