"""Tests für structure.find_bos_after — LTF-BOS nach Sweep."""
from src.strategy_core.structure import find_bos_after
from tests.helpers import make_bars


def test_bullish_bos_on_ltf():
    # Pivot-High = 105 bei Bar 3 (right highs strikt < 105), ab Bar 6 suchen → Bar 9 BOS
    df = make_bars([
        (100, 102, 99, 101),
        (101, 103, 100, 102),
        (102, 104, 101, 103),
        (103, 105, 102, 104),   # Pivot-High = 105
        (104, 104, 102, 103),   # high=104 (strikt < 105)
        (103, 104, 100, 101),
        (101, 102, 99, 100),    # 6 = from_bar_idx
        (100, 103, 99, 102),
        (102, 104, 101, 103),
        (103, 107, 102, 106),   # ← BOS: body 106 > 105
        (106, 108, 105, 107),
    ], tf="5m")
    bos = find_bos_after(df, direction="long", from_bar_idx=6, n_left=2, n_right=2)
    assert bos is not None
    assert bos.direction == "long"
    assert bos.bar_idx == 9
    assert bos.broken_swing.price == 105.0


def test_bearish_bos_on_ltf():
    df = make_bars([
        (100, 102, 99, 101),
        (101, 103, 100, 102),
        (102, 103, 101, 102),
        (102, 103, 95, 96),     # Pivot-Low = 95
        (96, 99, 96, 98),       # low=96 (strikt > 95)
        (98, 100, 97, 99),
        (99, 101, 98, 100),     # 6 = from_bar_idx
        (100, 102, 99, 101),
        (101, 102, 96, 97),
        (97, 98, 92, 93),       # ← BOS: body 93 < 95
        (93, 94, 88, 90),
    ], tf="5m")
    bos = find_bos_after(df, direction="short", from_bar_idx=6, n_left=2, n_right=2)
    assert bos is not None
    assert bos.direction == "short"
    assert bos.bar_idx == 9
    assert bos.broken_swing.price == 95.0


def test_no_bos_when_no_breakthrough():
    df = make_bars([
        (100, 102, 99, 101),
        (101, 103, 100, 102),
        (102, 104, 101, 103),
        (103, 110, 102, 104),   # Pivot-High = 110
        (104, 105, 102, 103),
        (103, 104, 100, 101),
        (101, 102, 99, 100),
        (100, 103, 99, 102),
        (102, 105, 101, 104),
        (104, 106, 103, 105),   # Body max 105, niemals > 110
        (105, 107, 104, 106),
    ], tf="5m")
    bos = find_bos_after(df, direction="long", from_bar_idx=6, n_left=2, n_right=2)
    assert bos is None
