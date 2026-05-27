"""Tests für engine.evaluate — End-to-End mit synthetischen Multi-TF-Bars.

Reine Smoke-Tests: das Engine darf nicht abstürzen + sollte bei klar konstruierten
Bias-Situationen die richtige Signal-Side liefern (oder begründet None).
"""
import pandas as pd

from src.strategy_core import evaluate
from src.strategy_core.bias import compute_bias
from tests.helpers import make_bars


def _bullish_daily() -> pd.DataFrame:
    """Daily mit klarem Bullish-BoS (für long-Bias)."""
    return make_bars([
        (100, 102, 99, 101),
        (101, 103, 100, 102),
        (102, 104, 101, 103),
        (103, 110, 102, 104),    # Pivot-High = 110
        (104, 105, 102, 103),
        (103, 104, 100, 101),
        (101, 102, 99, 100),
        (100, 103, 99, 102),
        (102, 108, 101, 107),
        (107, 113, 106, 112),    # Bullish-BoS (Body 112 > 110)
        (112, 114, 110, 113),
        (113, 115, 111, 114),
    ], tf="1d")


def test_engine_returns_none_without_htf_or_ltf():
    # Nur Daily, kein HTF/LTF → kein Signal
    result = evaluate("BTC", {"1d": _bullish_daily()})
    assert result is None


def test_engine_does_not_crash_with_minimal_data():
    # Komplette Multi-TF-Daten, aber ohne perfektem Setup → Engine darf null liefern
    bars = {
        "1d": _bullish_daily(),
        "1h": make_bars([(100, 102, 99, 101)] * 30, tf="1h"),
        "5m": make_bars([(100, 101, 99, 100)] * 30, tf="5m"),
    }
    # Darf nicht crashen; Signal oder None ist beides legitim
    result = evaluate("BTC", bars)
    assert result is None or result.side in ("long", "short")


def test_bullish_daily_yields_long_bias_directly():
    # Sanity: Bias-Modul allein bestätigt long auf unserem synth Daily
    bias = compute_bias(_bullish_daily(), n_left=2, n_right=2)
    assert bias.direction == "long"
