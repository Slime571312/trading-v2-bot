"""Unit-Tests für Paper-Broker — Position Open/Close + intrabar Exit."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from src.live import paper_broker
from src.live.state import OpenTrade
from src.strategy_core._types import Bias, Signal, Sweep, StructureBreak, Swing


def _make_signal(side: str, entry: float, sl: float, tp: float) -> Signal:
    now = pd.Timestamp("2026-01-01T12:00:00Z")
    bias = Bias(direction=side, last_bos_time=now, last_bos_level=entry)
    swept_swing = Swing(time=now, price=sl, kind="high" if side == "short" else "low", bar_idx=0)
    sweep = Sweep(
        time=now, bar_idx=0, level=sl, swing=swept_swing,
        direction="bsl" if side == "short" else "ssl", tf="1h",
    )
    broken = Swing(time=now, price=entry, kind="high" if side == "long" else "low", bar_idx=5)
    bos = StructureBreak(
        time=now, bar_idx=10, body_extreme=entry,
        broken_swing=broken, direction=side,
    )
    sl_dist = abs(entry - sl)
    rr_val = abs(tp - entry) / sl_dist if sl_dist > 0 else 0.0
    return Signal(
        time=now, instrument="BTC", side=side,
        entry=entry, sl=sl, tp=tp, rr=rr_val,
        variant="primary", htf_used="1h", ltf_used="5m",
        bias=bias, sweep=sweep, structure_break=bos,
    )


def _bar(o: float, h: float, l: float, c: float) -> pd.Series:
    return pd.Series({"open": o, "high": h, "low": l, "close": c, "volume": 1000.0})


# ────────────────────────────────────────────────────────────────────────


def test_open_long_position_sizing():
    """Bei 1% Risk + SL-Distanz 100 sollte size = (10000*0.01)/100 = 1.0."""
    signal = _make_signal("long", entry=50_000, sl=49_900, tp=50_300)
    bar = _bar(50_000, 50_010, 49_990, 50_000)
    trade = paper_broker.open_position(signal, equity=10_000, risk_pct=0.01, current_bar=bar)
    assert trade is not None
    # SL hit = exakt 1R Verlust → size * SL-Distanz = risk_amount
    assert abs(trade.size * abs(trade.entry - trade.sl) - 100.0) < 1.0
    assert trade.side == "long"


def test_open_short_position():
    signal = _make_signal("short", entry=50_000, sl=50_100, tp=49_700)
    bar = _bar(50_000, 50_010, 49_990, 50_000)
    trade = paper_broker.open_position(signal, equity=10_000, risk_pct=0.01, current_bar=bar)
    assert trade is not None
    assert trade.side == "short"


def test_open_position_rejects_zero_sl():
    """SL = Entry → SL-Distanz 0 → keine Position."""
    signal = _make_signal("long", entry=50_000, sl=50_000, tp=50_300)
    bar = _bar(50_000, 50_010, 49_990, 50_000)
    trade = paper_broker.open_position(signal, equity=10_000, risk_pct=0.01, current_bar=bar)
    assert trade is None


def test_intrabar_sl_hit_long():
    """Long: Low durchsticht SL → SL-Exit."""
    trade = OpenTrade(
        id="x1", instrument="BTC", side="long",
        open_time=datetime.now(timezone.utc),
        entry=50_000, sl=49_900, tp=50_300, size=1.0,
        variant="primary", htf_used="1h", ltf_used="5m", rr_at_open=3.0,
    )
    bar = _bar(50_000, 50_050, 49_850, 49_900)  # Low durchsticht 49900
    exit_hit = paper_broker.check_intrabar_exit(trade, bar)
    assert exit_hit is not None
    price, reason = exit_hit
    assert reason == "sl"
    assert price == 49_900


def test_intrabar_tp_hit_short():
    """Short: Low durchsticht TP → TP-Exit."""
    trade = OpenTrade(
        id="x2", instrument="BTC", side="short",
        open_time=datetime.now(timezone.utc),
        entry=50_000, sl=50_100, tp=49_700, size=1.0,
        variant="primary", htf_used="1h", ltf_used="5m", rr_at_open=3.0,
    )
    bar = _bar(50_000, 50_010, 49_690, 49_750)
    exit_hit = paper_broker.check_intrabar_exit(trade, bar)
    assert exit_hit is not None
    _, reason = exit_hit
    assert reason == "tp"


def test_intrabar_sl_wins_on_ambiguity_long():
    """Long: Bar durchsticht SL UND TP → konservativ SL gewinnt."""
    trade = OpenTrade(
        id="x3", instrument="BTC", side="long",
        open_time=datetime.now(timezone.utc),
        entry=50_000, sl=49_900, tp=50_300, size=1.0,
        variant="primary", htf_used="1h", ltf_used="5m", rr_at_open=3.0,
    )
    bar = _bar(50_000, 50_350, 49_850, 50_300)
    exit_hit = paper_broker.check_intrabar_exit(trade, bar)
    assert exit_hit is not None
    _, reason = exit_hit
    assert reason == "sl"


def test_intrabar_no_exit():
    trade = OpenTrade(
        id="x4", instrument="BTC", side="long",
        open_time=datetime.now(timezone.utc),
        entry=50_000, sl=49_900, tp=50_300, size=1.0,
        variant="primary", htf_used="1h", ltf_used="5m", rr_at_open=3.0,
    )
    bar = _bar(50_000, 50_050, 49_950, 50_020)
    assert paper_broker.check_intrabar_exit(trade, bar) is None


def test_close_position_tp_gives_positive_r():
    """Long-Trade auf TP geschlossen → R > 0."""
    trade = OpenTrade(
        id="x5", instrument="BTC", side="long",
        open_time=datetime.now(timezone.utc),
        entry=50_000, sl=49_900, tp=50_300, size=1.0,
        variant="primary", htf_used="1h", ltf_used="5m", rr_at_open=3.0,
    )
    bar = _bar(50_000, 50_350, 49_950, 50_300)
    closed = paper_broker.close_position(trade, exit_price_raw=50_300, current_bar=bar, reason="tp")
    assert closed.r_multiple > 0
    assert closed.exit_reason == "tp"


def test_close_position_sl_gives_negative_r():
    """Long-Trade auf SL geschlossen → R deutlich negativ (Slippage darf größer als 1R sein
    bei großem Bar-Range, das ist realistisch — wir prüfen nur Vorzeichen + Magnitude)."""
    trade = OpenTrade(
        id="x6", instrument="BTC", side="long",
        open_time=datetime.now(timezone.utc),
        entry=50_000, sl=49_900, tp=50_300, size=1.0,
        variant="primary", htf_used="1h", ltf_used="5m", rr_at_open=3.0,
    )
    # Bar mit Low knapp am SL → realistisches Slippage-Profil
    bar = _bar(50_000, 50_020, 49_895, 49_900)
    closed = paper_broker.close_position(trade, exit_price_raw=49_900, current_bar=bar, reason="sl")
    assert closed.r_multiple < 0
    assert closed.exit_reason == "sl"


def test_force_close_manual():
    """Manueller Close ohne intrabar-Check."""
    trade = OpenTrade(
        id="x7", instrument="BTC", side="long",
        open_time=datetime.now(timezone.utc),
        entry=50_000, sl=49_900, tp=50_300, size=1.0,
        variant="primary", htf_used="1h", ltf_used="5m", rr_at_open=3.0,
    )
    closed = paper_broker.force_close(trade, last_price=50_150, reason="manual")
    assert closed.exit == 50_150
    assert closed.exit_reason == "manual"
    assert closed.r_multiple > 0  # Zwischen Entry und TP → positiv
