"""Tests für bias.compute_bias — Daily-BoS-Erkennung."""
from src.strategy_core.bias import compute_bias
from tests.helpers import make_bars


def test_bullish_bos_yields_long_bias():
    # Sequenz: Pivot-High bei Bar 3 (high=110), später (Bar 9) schließt der Body über 110
    df = make_bars([
        (100, 102, 99, 101),
        (101, 103, 100, 102),
        (102, 104, 101, 103),
        (103, 110, 102, 104),   # Pivot-High = 110
        (104, 105, 102, 103),
        (103, 104, 100, 101),
        (101, 102, 99, 100),
        (100, 103, 99, 102),
        (102, 108, 101, 107),
        (107, 113, 106, 112),   # ← Body-Close 112 > 110 = Bullish-BoS
        (112, 114, 110, 113),
    ], tf="1d")
    bias = compute_bias(df, n_left=2, n_right=2)
    assert bias.direction == "long"
    assert bias.last_bos_index == 9
    assert bias.last_bos_level == 110.0


def test_bearish_bos_yields_short_bias():
    # Pivot-Low bei Bar 3 (low=90), später (Bar 9) Body-Close unter 90
    df = make_bars([
        (100, 102, 99, 101),
        (101, 103, 100, 102),
        (102, 104, 101, 103),
        (103, 104, 90, 96),     # Pivot-Low = 90
        (96, 101, 95, 100),
        (100, 103, 99, 102),
        (102, 105, 101, 104),
        (104, 105, 100, 101),
        (101, 102, 95, 96),
        (96, 97, 85, 88),       # ← Body-Close 88 < 90 = Bearish-BoS
        (88, 89, 80, 82),
    ], tf="1d")
    bias = compute_bias(df, n_left=2, n_right=2)
    assert bias.direction == "short"
    assert bias.last_bos_index == 9
    assert bias.last_bos_level == 90.0


def test_consolidation_yields_neutral_bias():
    # Range-Bound ohne Body-Close jenseits eines Pivots
    df = make_bars([
        (100, 101, 99, 100),
        (100, 102, 99, 101),
        (101, 102, 100, 101),
        (101, 103, 100, 102),
        (102, 103, 100, 101),
        (101, 102, 99, 100),
        (100, 101, 99, 100),
    ], tf="1d")
    bias = compute_bias(df, n_left=2, n_right=2)
    assert bias.direction == "neutral"


def test_latest_bos_wins_when_multiple():
    # Erst Bullish-BoS bei Bar 6 (bricht 110), dann Bearish-BoS bei Bar 15 (bricht 95)
    # → letzter BoS = bearish → bias short
    df = make_bars([
        (100, 102, 99, 101),     # 0
        (101, 103, 100, 102),    # 1
        (102, 104, 101, 103),    # 2
        (103, 110, 102, 104),    # 3 — Pivot-High 110 (right highs 106, 105 < 110)
        (104, 106, 102, 103),    # 4
        (103, 105, 101, 102),    # 5
        (102, 113, 101, 112),    # 6 — Bullish-BoS (Body 112 > 110)
        (112, 114, 110, 113),    # 7
        (113, 115, 109, 110),    # 8 — Pivot-High 115 (right highs 111, 109 < 115)
        (110, 111, 106, 107),    # 9
        (107, 109, 104, 105),    # 10
        (105, 107, 95, 96),      # 11 — Pivot-Low 95 (left lows 106, 104; right lows 96, 97)
        (96, 99, 96, 98),        # 12
        (98, 101, 97, 100),      # 13
        (100, 102, 98, 101),     # 14
        (101, 102, 90, 91),      # 15 — Bearish-BoS (Body 91 < 95)
    ], tf="1d")
    bias = compute_bias(df, n_left=2, n_right=2)
    assert bias.direction == "short"
    assert bias.last_bos_index == 15
    assert bias.last_bos_level == 95.0
