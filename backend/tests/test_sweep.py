"""Tests für sweep.detect_sweeps — Wick durchsticht, Body schließt zurück."""
from src.strategy_core.pivots import find_pivots
from src.strategy_core.sweep import detect_sweeps
from tests.helpers import make_bars


def test_bsl_sweep_detected():
    # Pivot-High = 110 bei Bar 3, später Bar 9: high > 110 ABER body komplett unter 110
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
        (107, 113, 106, 108),   # ← Sweep: high=113 > 110, max(open=107, close=108) < 110
        (108, 109, 100, 101),
    ])
    pivots = find_pivots(df, n_left=2, n_right=2)
    sweeps = detect_sweeps(df, pivots, tf="1h", lookback=20)
    bsl_sweeps = [s for s in sweeps if s.direction == "bsl"]
    assert len(bsl_sweeps) == 1
    assert bsl_sweeps[0].bar_idx == 9
    assert bsl_sweeps[0].level == 110.0


def test_ssl_sweep_detected():
    # Pivot-Low = 90 bei Bar 3, später Bar 9: low < 90 ABER body komplett über 90
    df = make_bars([
        (100, 102, 99, 101),
        (101, 103, 100, 102),
        (102, 104, 101, 103),
        (103, 104, 90, 96),     # Pivot-Low = 90
        (96, 101, 95, 100),
        (100, 103, 99, 102),
        (102, 104, 101, 103),
        (103, 105, 100, 102),
        (102, 103, 95, 98),
        (98, 100, 85, 95),      # ← Sweep: low=85 < 90, min(open=98, close=95) > 90
        (95, 97, 92, 94),
    ])
    pivots = find_pivots(df, n_left=2, n_right=2)
    sweeps = detect_sweeps(df, pivots, tf="1h", lookback=20)
    ssl_sweeps = [s for s in sweeps if s.direction == "ssl"]
    assert len(ssl_sweeps) == 1
    assert ssl_sweeps[0].bar_idx == 9
    assert ssl_sweeps[0].level == 90.0


def test_body_breakthrough_is_not_a_sweep():
    # Body geht über das Level → das ist ein BOS, KEIN Sweep
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
        (107, 113, 106, 112),   # ← Body schließt 112 > 110 = BOS, NICHT Sweep
        (112, 114, 110, 113),
    ])
    pivots = find_pivots(df, n_left=2, n_right=2)
    sweeps = detect_sweeps(df, pivots, tf="1h", lookback=20)
    assert all(s.direction != "bsl" or s.bar_idx != 9 for s in sweeps)
