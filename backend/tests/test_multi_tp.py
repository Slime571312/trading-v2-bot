"""Tests für 3-Tier Multi-TP-Staffelung (Vault/Bot/TP-Staffelung.md).

Deckt ab:
- compute_multi_tp_targets() mit 0/1/2/3+ Pivots
- open_position übernimmt Signal-tp1/tp2/pcts korrekt
- scan_with_partial_close: A-Setup 40/40/20 läuft TP1 → TP2 → TP3
- scan_with_partial_close: B-Setup 50/50/0 schließt bei TP2 voll
- Lock-Profit-SL nach TP2 (SL auf TP1) feuert sauber
- Legacy-Trade ohne tp1/tp2 → alte 2-Tier-Logik bleibt funktional
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from src.live import paper_broker
from src.live.state import OpenTrade
from src.strategy_core._types import Bias, Signal, Sweep, StructureBreak, Swing
from src.strategy_core.rr import compute_multi_tp_targets


def _bar(o: float, h: float, l: float, c: float, ts: str = "2026-06-01T12:00:00Z") -> pd.Series:
    s = pd.Series({"open": o, "high": h, "low": l, "close": c, "volume": 1000.0})
    s.name = pd.Timestamp(ts)
    return s


def _bars_df(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    """[(ts, o, h, l, c), ...] → DataFrame mit DatetimeIndex."""
    df = pd.DataFrame(
        rows, columns=["ts", "open", "high", "low", "close"],
    )
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts")
    return df


# ── compute_multi_tp_targets ────────────────────────────────────────────────

def _swing(price: float, kind: str, idx: int = 0) -> Swing:
    return Swing(time=pd.Timestamp("2026-01-01T00:00:00Z"), price=price, kind=kind, bar_idx=idx)


def test_multi_tp_targets_long_three_pivots():
    """3+ Pivots: nähester → tp1, median → tp2, entferntester → tp3."""
    pivots = [
        _swing(110, "high"), _swing(115, "high"),
        _swing(120, "high"), _swing(130, "high"),
    ]
    tp1, tp2, tp3 = compute_multi_tp_targets("long", entry=100, htf_pivots=pivots)
    assert tp1 == 110
    assert tp3 == 130
    assert tp2 is not None and 110 < tp2 < 130


def test_multi_tp_targets_short_three_pivots():
    pivots = [_swing(90, "low"), _swing(85, "low"), _swing(70, "low")]
    tp1, tp2, tp3 = compute_multi_tp_targets("short", entry=100, htf_pivots=pivots)
    assert tp1 == 90   # nähester (höchster) Low unter Entry
    assert tp3 == 70   # entferntester
    assert tp2 == 85


def test_multi_tp_targets_two_pivots():
    """2 Pivots: tp1 + tp3; tp2 = None (Caller behält Default)."""
    pivots = [_swing(110, "high"), _swing(120, "high")]
    tp1, tp2, tp3 = compute_multi_tp_targets("long", entry=100, htf_pivots=pivots)
    assert tp1 == 110
    assert tp2 is None
    assert tp3 == 120


def test_multi_tp_targets_single_pivot():
    """1 Pivot: nur tp3 (Legacy-Fallback)."""
    pivots = [_swing(120, "high")]
    tp1, tp2, tp3 = compute_multi_tp_targets("long", entry=100, htf_pivots=pivots)
    assert tp1 is None
    assert tp2 is None
    assert tp3 == 120


def test_multi_tp_targets_no_opposite_pivots():
    """Kein passender Pivot in Trade-Richtung → alles None."""
    pivots = [_swing(80, "low")]  # Low aber wir wollen Long-Target (Highs)
    tp1, tp2, tp3 = compute_multi_tp_targets("long", entry=100, htf_pivots=pivots)
    assert (tp1, tp2, tp3) == (None, None, None)


# ── ERL/IRL — FVG-Fill als TP1 (Vault/Bot/ERL-IRL.md) ──────────────────────


def _fvg(upper: float, lower: float, direction: str) -> "FVG":
    from src.strategy_core._types import FVG
    return FVG(time=pd.Timestamp("2026-01-01T00:00:00Z"),
               bar_idx=0, upper=upper, lower=lower, direction=direction)


def test_erl_irl_long_uses_fvg_fill_as_tp1():
    """FVG zwischen Entry und Pivot → IRL-Fill als TP1, Pivot wird ERL = TP2."""
    pivots = [_swing(120, "high"), _swing(130, "high")]
    # Bearish FVG (opposite direction) zwischen Entry 100 und Pivot 120
    fvgs = [_fvg(upper=112, lower=108, direction="short")]
    tp1, tp2, tp3 = compute_multi_tp_targets("long", entry=100, htf_pivots=pivots,
                                             irl_fvgs=fvgs)
    assert tp1 == 110   # FVG-Mid
    assert tp2 == 120   # ERL = nächster Pivot
    assert tp3 == 130


def test_erl_irl_short_uses_fvg_fill_as_tp1():
    """Short-Trade: Bullish FVG zwischen Pivot und Entry → IRL-Fill als TP1."""
    pivots = [_swing(85, "low"), _swing(75, "low")]
    fvgs = [_fvg(upper=92, lower=88, direction="long")]
    tp1, tp2, tp3 = compute_multi_tp_targets("short", entry=100, htf_pivots=pivots,
                                             irl_fvgs=fvgs)
    assert tp1 == 90    # FVG-Mid
    assert tp2 == 85    # ERL
    assert tp3 == 75


def test_erl_irl_picks_nearest_fvg_to_entry():
    """Zwei FVGs im Pfad → die näher am Entry gewinnt (= wird zuerst getroffen)."""
    pivots = [_swing(130, "high")]
    fvgs = [
        _fvg(upper=125, lower=120, direction="short"),  # weiter weg
        _fvg(upper=108, lower=104, direction="short"),  # näher
    ]
    tp1, tp2, tp3 = compute_multi_tp_targets("long", entry=100, htf_pivots=pivots,
                                             irl_fvgs=fvgs)
    assert tp1 == 106   # näheste FVG-Mid


def test_erl_irl_falls_back_when_fvg_outside_path():
    """FVG außerhalb Entry-Pivot-Range → Legacy-Fallback auf reine Pivot-Heuristik."""
    pivots = [_swing(110, "high"), _swing(120, "high"), _swing(130, "high")]
    # FVG liegt JENSEITS des Pivots → irrelevant
    fvgs = [_fvg(upper=140, lower=135, direction="short")]
    tp1, tp2, tp3 = compute_multi_tp_targets("long", entry=100, htf_pivots=pivots,
                                             irl_fvgs=fvgs)
    assert tp1 == 110  # Legacy: nächster Pivot
    assert tp3 == 130


def test_erl_irl_no_fvgs_uses_legacy():
    """Leere FVG-Liste → Legacy-Verhalten unverändert."""
    pivots = [_swing(110, "high"), _swing(120, "high"), _swing(130, "high")]
    tp1, tp2, tp3 = compute_multi_tp_targets("long", entry=100, htf_pivots=pivots,
                                             irl_fvgs=[])
    assert tp1 == 110
    assert tp3 == 130


# ── TP1-Min-Distance ≥ 0.5R (Live-Fix 2026-06-24) ─────────────────────────


def test_tp1_min_distance_filters_too_close_fvg():
    """FVG 0.3R weg vom Entry wird ignoriert (< 0.5R Mindest-Distanz).
    Entry 100, SL 95 (sl_dist=5), min_tp1_dist = 2.5. FVG bei 101 (dist=1) → raus."""
    pivots = [_swing(120, "high")]
    fvgs = [_fvg(upper=101.5, lower=100.5, direction="short")]   # mid=101, dist=1
    tp1, tp2, tp3 = compute_multi_tp_targets(
        "long", entry=100, htf_pivots=pivots, irl_fvgs=fvgs, sl_distance=5.0,
    )
    # FVG zu nah → Legacy-Fallback auf nähesten Pivot
    assert tp1 is None
    assert tp3 == 120


def test_tp1_min_distance_accepts_distant_fvg():
    """FVG mit Distance ≥ 0.5R wird akzeptiert.
    Entry 100, SL 95 (sl_dist=5), min_tp1_dist = 2.5. FVG bei 104 (dist=4) → ok."""
    pivots = [_swing(120, "high"), _swing(130, "high")]
    fvgs = [_fvg(upper=104.5, lower=103.5, direction="short")]   # mid=104, dist=4
    tp1, tp2, tp3 = compute_multi_tp_targets(
        "long", entry=100, htf_pivots=pivots, irl_fvgs=fvgs, sl_distance=5.0,
    )
    assert tp1 == 104
    assert tp2 == 120


def test_tp1_min_distance_short_side():
    """Selbe Logik für Shorts: FVG zu nah am Entry → raus."""
    pivots = [_swing(80, "low")]
    fvgs = [_fvg(upper=99.5, lower=98.5, direction="long")]   # mid=99, dist=1
    tp1, tp2, tp3 = compute_multi_tp_targets(
        "short", entry=100, htf_pivots=pivots, irl_fvgs=fvgs, sl_distance=5.0,
    )
    assert tp1 is None
    assert tp3 == 80


def test_tp1_min_distance_zero_keeps_old_behavior():
    """sl_distance=None → kein Min-Distance-Filter (Backward-Compat)."""
    pivots = [_swing(120, "high")]
    fvgs = [_fvg(upper=101.5, lower=100.5, direction="short")]
    tp1, tp2, tp3 = compute_multi_tp_targets(
        "long", entry=100, htf_pivots=pivots, irl_fvgs=fvgs, sl_distance=None,
    )
    assert tp1 == 101    # FVG akzeptiert obwohl nah, weil kein sl_distance gesetzt


# ── open_position mit Multi-TP ──────────────────────────────────────────────

def _signal_with_multi_tp(
    side: str, entry: float, sl: float, tp: float,
    tp1: float | None, tp2: float | None,
    pct1: float, pct2: float, pct3: float,
) -> Signal:
    now = pd.Timestamp("2026-01-01T12:00:00Z")
    bias = Bias(direction=side, last_bos_time=now, last_bos_level=entry)
    sw = Swing(time=now, price=sl, kind="high" if side == "short" else "low", bar_idx=0)
    sweep = Sweep(time=now, bar_idx=0, level=sl, swing=sw,
                  direction="bsl" if side == "short" else "ssl", tf="1h")
    broken = Swing(time=now, price=entry, kind="high" if side == "long" else "low", bar_idx=5)
    bos = StructureBreak(time=now, bar_idx=10, body_extreme=entry, broken_swing=broken, direction=side)
    sl_dist = abs(entry - sl)
    rr = abs(tp - entry) / sl_dist
    return Signal(
        time=now, instrument="BTC", side=side,
        entry=entry, sl=sl, tp=tp, rr=rr,
        variant="primary", htf_used="1h", ltf_used="5m",
        bias=bias, sweep=sweep, structure_break=bos,
        tp1=tp1, tp2=tp2, pct1=pct1, pct2=pct2, pct3=pct3,
    )


def test_open_position_picks_up_multi_tp_from_signal():
    """Engine setzt tp1/tp2/pcts → OpenTrade übernimmt sie."""
    sig = _signal_with_multi_tp(
        "long", entry=100, sl=95, tp=130,
        tp1=110, tp2=120, pct1=0.4, pct2=0.4, pct3=0.2,
    )
    bar = _bar(100, 100.1, 99.9, 100)
    trade = paper_broker.open_position(sig, equity=10_000, risk_pct=0.01, current_bar=bar)
    assert trade is not None
    assert trade.tp1 == 110
    assert trade.tp2 == 120
    assert trade.tp == 130
    assert (trade.pct1, trade.pct2, trade.pct3) == (0.4, 0.4, 0.2)


def test_open_position_carries_journal_tags_from_signal():
    """Engine packt session/atr_regime/weekday/conviction in Signal → OpenTrade übernimmt."""
    sig = _signal_with_multi_tp(
        "long", entry=50_000, sl=49_500, tp=53_000,
        tp1=51_000, tp2=52_000, pct1=0.4, pct2=0.4, pct3=0.2,
    )
    sig.session_label = "london"
    sig.atr_regime = "high"
    sig.atr_pct = 0.038
    sig.vol_size_factor = 0.7
    sig.weekday = 1
    sig.conviction_score = 8
    sig.conviction_grade = "A"
    sig.adx_regime = "trending"
    bar = _bar(50_000, 50_010, 49_990, 50_000)
    trade = paper_broker.open_position(sig, equity=10_000, risk_pct=0.01, current_bar=bar)
    assert trade is not None
    assert trade.session_at_open == "london"
    assert trade.atr_regime_at_open == "high"
    assert trade.atr_pct_at_open == 0.038
    assert trade.vol_size_factor_at_open == 0.7
    assert trade.weekday_at_open == 1
    assert trade.conviction_score_at_open == 8
    assert trade.conviction_grade_at_open == "A"
    assert trade.adx_regime_at_open == "trending"


def test_close_position_preserves_journal_tags():
    """Tags überleben open → close (notwendig für Performance-Attribution)."""
    sig = _signal_with_multi_tp(
        "long", entry=50_000, sl=49_500, tp=53_000,
        tp1=51_000, tp2=52_000, pct1=0.4, pct2=0.4, pct3=0.2,
    )
    sig.session_label = "ny"
    sig.atr_regime = "normal"
    sig.weekday = 2
    sig.conviction_score = 7
    bar = _bar(50_000, 50_010, 49_990, 50_000)
    trade = paper_broker.open_position(sig, equity=10_000, risk_pct=0.01, current_bar=bar)
    exit_bar = _bar(50_000, 50_100, 49_400, 49_500, ts="2026-01-01T13:00:00Z")
    closed = paper_broker.close_position(
        trade, exit_price_raw=49_500.0, current_bar=exit_bar, reason="sl",
        close_time=pd.Timestamp("2026-01-01T13:00:00Z"),
    )
    assert closed.session_at_open == "ny"
    assert closed.atr_regime_at_open == "normal"
    assert closed.weekday_at_open == 2
    assert closed.conviction_score_at_open == 7


def test_close_position_preserves_multi_tp_audit_fields():
    """tp1/tp2/pct1-3/original_sl/original_size müssen im ClosedTrade landen.
    Sonst keine retro-Verifikation (Live-Analyse 2026-06-24 zeigte 8 Trades nicht
    verifizierbar weil diese Felder fehlten)."""
    sig = _signal_with_multi_tp(
        "long", entry=50_000, sl=49_500, tp=53_000,
        tp1=51_000, tp2=52_000, pct1=0.4, pct2=0.4, pct3=0.2,
    )
    bar = _bar(50_000, 50_010, 49_990, 50_000)
    trade = paper_broker.open_position(sig, equity=10_000, risk_pct=0.01, current_bar=bar)
    orig_size = trade.original_size
    orig_sl = trade.original_sl
    exit_bar = _bar(50_000, 53_100, 49_990, 53_000, ts="2026-01-01T15:00:00Z")
    closed = paper_broker.close_position(
        trade, exit_price_raw=53_000.0, current_bar=exit_bar, reason="tp",
        close_time=pd.Timestamp("2026-01-01T15:00:00Z"),
    )
    assert closed.tp1 == 51_000
    assert closed.tp2 == 52_000
    assert closed.pct1 == 0.4
    assert closed.pct2 == 0.4
    assert closed.pct3 == 0.2
    assert closed.original_sl == orig_sl
    assert closed.original_size == orig_size


def test_open_position_legacy_fallback_when_signal_has_no_tp1():
    """Signal ohne tp1/tp2 → Heuristik: tp1=halbweg, 50/50.

    Wir nutzen entry=50_000 statt 100 damit der instrument-spezifische
    BTC-Spread von 50 USD nicht 50% der Preis-Skala ausmacht.
    """
    sig = _signal_with_multi_tp(
        "long", entry=50_000, sl=49_500, tp=51_000,
        tp1=None, tp2=None, pct1=0.5, pct2=0.5, pct3=0.0,
    )
    bar = _bar(50_000, 50_010, 49_990, 50_000)
    trade = paper_broker.open_position(sig, equity=10_000, risk_pct=0.01, current_bar=bar)
    assert trade is not None
    # halbweg zwischen entry (mit ~25 USD Slippage) und tp=51_000.
    assert trade.tp1 == pytest.approx(50_500, abs=50)
    assert trade.tp2 is None
    assert (trade.pct1, trade.pct2, trade.pct3) == (0.5, 0.5, 0.0)


# ── scan_with_partial_close: 3-Tier-Lifecycle ───────────────────────────────

def _trade_long_3tier() -> OpenTrade:
    """A-Setup: 40/40/20, Entry 100, SL 95, TP1 110, TP2 120, TP3 130, Size 10."""
    return OpenTrade(
        id="t3tier", instrument="BTC", side="long",
        open_time=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        entry=100.0, sl=95.0, tp=130.0, size=10.0,
        variant="primary", htf_used="1h", ltf_used="5m", rr_at_open=6.0,
        tp1=110.0, tp2=120.0,
        pct1=0.4, pct2=0.4, pct3=0.2,
        original_size=10.0, original_sl=95.0,
    )


def test_scan_3tier_a_setup_full_lifecycle():
    """Bar 1: TP1-Hit (40% close, SL→BE).
    Bar 2: TP2-Hit (40% close, SL→TP1).
    Bar 3: TP3-Hit auf entferntester (20% Runner).
    Wichtig: Bar-Lows >= jeweiliger SL, sonst BE/Lock-SL triggert in der Hit-Bar.
    """
    trade = _trade_long_3tier()
    bars = _bars_df([
        ("2026-06-01T13:00:00Z", 100, 112, 101, 111),   # TP1=110, low>entry=100
        ("2026-06-01T14:00:00Z", 111, 122, 111, 121),   # TP2=120, low>TP1=110
        ("2026-06-01T15:00:00Z", 121, 132, 121, 131),   # TP3=130, low>TP1=110
    ])
    events = paper_broker.scan_with_partial_close(trade, bars)
    kinds = [e["kind"] for e in events]
    assert kinds == ["partial_tp1", "partial_tp2", "full_close"]
    assert trade.tp1_hit and trade.tp2_hit
    assert events[-1]["reason"] == "tp"


def test_scan_b_setup_closes_fully_at_tp2():
    """B-Setup (pct3=0): nach TP2 wird direkt voll geschlossen, kein Runner."""
    trade = _trade_long_3tier()
    trade.pct1, trade.pct2, trade.pct3 = 0.5, 0.5, 0.0
    bars = _bars_df([
        ("2026-06-01T13:00:00Z", 100, 112, 101, 111),
        ("2026-06-01T14:00:00Z", 111, 122, 111, 121),
    ])
    events = paper_broker.scan_with_partial_close(trade, bars)
    kinds = [e["kind"] for e in events]
    assert kinds == ["partial_tp1", "partial_tp2", "full_close"]
    assert events[-1]["exit"] == 120.0


def test_scan_lock_profit_sl_after_tp2():
    """A-Setup: nach TP2 (SL=TP1=110) — folgende Bar fällt auf 108 → SL-Hit (Lock-Profit)."""
    trade = _trade_long_3tier()
    bars = _bars_df([
        ("2026-06-01T13:00:00Z", 100, 112, 101, 111),   # TP1 ohne BE-Hit
        ("2026-06-01T14:00:00Z", 111, 122, 111, 121),   # TP2 ohne Lock-Hit
        ("2026-06-01T15:00:00Z", 121, 122, 108, 109),   # low<TP1=110 → SL gerissen
    ])
    events = paper_broker.scan_with_partial_close(trade, bars)
    kinds = [e["kind"] for e in events]
    assert kinds == ["partial_tp1", "partial_tp2", "full_close"]
    assert events[-1]["reason"] == "sl"
    assert events[-1]["exit"] == 110.0   # = TP1 = neuer SL


def test_scan_be_sl_after_tp1_long():
    """A-Setup: TP1-Hit → SL=Entry. Folge-Bar fällt unter Entry → BE-Close."""
    trade = _trade_long_3tier()
    bars = _bars_df([
        ("2026-06-01T13:00:00Z", 100, 112, 99,  111),   # TP1 → SL=100
        ("2026-06-01T14:00:00Z", 111, 112, 98,  99),    # fällt unter SL=100
    ])
    events = paper_broker.scan_with_partial_close(trade, bars)
    kinds = [e["kind"] for e in events]
    assert kinds == ["partial_tp1", "full_close"]
    assert events[-1]["reason"] == "sl"
    assert events[-1]["exit"] == 100.0


def test_scan_legacy_2tier_still_works():
    """Trade ohne tp2 (Legacy) → bei TP1-Hit + finalem TP-Hit zwei Events."""
    trade = _trade_long_3tier()
    trade.tp2 = None
    trade.pct1, trade.pct2, trade.pct3 = 0.5, 0.5, 0.0
    bars = _bars_df([
        ("2026-06-01T13:00:00Z", 100, 112, 101, 111),
        ("2026-06-01T14:00:00Z", 111, 132, 111, 131),   # low>SL=Entry=100
    ])
    events = paper_broker.scan_with_partial_close(trade, bars)
    kinds = [e["kind"] for e in events]
    assert kinds == ["partial_tp1", "full_close"]
    assert events[-1]["reason"] == "tp"


def test_scan_sl_before_tp1_no_partials():
    """SL gerissen bevor TP1 → nur full_close, keine partial-Events."""
    trade = _trade_long_3tier()
    bars = _bars_df([
        ("2026-06-01T13:00:00Z", 100, 102, 94, 95),   # Low unter SL=95
    ])
    events = paper_broker.scan_with_partial_close(trade, bars)
    assert len(events) == 1
    assert events[0]["kind"] == "full_close"
    assert events[0]["reason"] == "sl"


# ── close_position: total_pnl/r_multiple-Korrektheit nach Multi-TP ─────────


def _trade_btc_3tier_realistic() -> OpenTrade:
    """BTC mit realistischen Preisen (50k Range), damit Capital-Spread (50 USD)
    nicht das Test-Signal überfrisst. SL-Distanz 500, Risk = 500×0.5 = 250.
    """
    return OpenTrade(
        id="t3tier_btc", instrument="BTC", side="long",
        open_time=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        entry=50_000.0, sl=49_500.0, tp=53_000.0, size=0.5,
        variant="primary", htf_used="1h", ltf_used="5m", rr_at_open=6.0,
        tp1=51_000.0, tp2=52_000.0,
        pct1=0.4, pct2=0.4, pct3=0.2,
        original_size=0.5, original_sl=49_500.0,
    )


def test_close_position_b_setup_total_pnl_includes_partials():
    """B-Setup TP1+TP2 voll geschlossen: pnl_abs=0 (size leer), partial_pnl>0,
    total_pnl reflektiert echten Trade-Gewinn. r_multiple gegen Original-Risk."""
    trade = _trade_btc_3tier_realistic()
    trade.pct1, trade.pct2, trade.pct3 = 0.5, 0.5, 0.0
    bar_tp1 = _bar(50_000, 51_100, 50_500, 51_000, ts="2026-06-01T13:00:00Z")
    bar_tp2 = _bar(51_000, 52_100, 51_000, 52_000, ts="2026-06-01T14:00:00Z")
    paper_broker.apply_tp1_partial(trade, bar_tp1, pd.Timestamp("2026-06-01T13:00:00Z"))
    paper_broker.apply_tp2_partial(trade, bar_tp2, pd.Timestamp("2026-06-01T14:00:00Z"))
    assert trade.size == pytest.approx(0.0, abs=1e-6)
    assert trade.partial_pnl > 0
    closed = paper_broker.close_position(
        trade, exit_price_raw=52_000.0, current_bar=bar_tp2, reason="tp",
        close_time=pd.Timestamp("2026-06-01T14:00:00Z"),
    )
    # Runner-Portion ist 0 (B-Setup, alles in TP1+TP2 verteilt)
    assert closed.pnl_abs == pytest.approx(0.0, abs=1e-6)
    # Aber Trade hat echten Gewinn aus Partials
    assert closed.partial_pnl == pytest.approx(trade.partial_pnl, abs=1e-6)
    assert closed.total_pnl > 0
    assert closed.total_pnl == pytest.approx(closed.pnl_abs + closed.partial_pnl, abs=1e-6)
    # R-Multiple gegen Original-Risk
    expected_r = closed.total_pnl / (abs(50_000.0 - 49_500.0) * 0.5)
    assert closed.r_multiple == pytest.approx(expected_r, abs=1e-6)
    assert closed.r_multiple > 1.0   # Trade hat klar > 1R verdient


def test_close_position_a_setup_runner_adds_to_partials():
    """A-Setup TP1+TP2 partials + TP3 Runner-Close: total_pnl = partial_pnl + pnl_abs."""
    trade = _trade_btc_3tier_realistic()
    bar_tp1 = _bar(50_000, 51_100, 50_500, 51_000, ts="2026-06-01T13:00:00Z")
    bar_tp2 = _bar(51_000, 52_100, 51_000, 52_000, ts="2026-06-01T14:00:00Z")
    bar_tp3 = _bar(52_000, 53_100, 52_000, 53_000, ts="2026-06-01T15:00:00Z")
    paper_broker.apply_tp1_partial(trade, bar_tp1, pd.Timestamp("2026-06-01T13:00:00Z"))
    paper_broker.apply_tp2_partial(trade, bar_tp2, pd.Timestamp("2026-06-01T14:00:00Z"))
    # Rest = pct3 × 0.5 = 0.1 (Runner)
    assert trade.size == pytest.approx(0.1, abs=1e-6)
    closed = paper_broker.close_position(
        trade, exit_price_raw=53_000.0, current_bar=bar_tp3, reason="tp",
        close_time=pd.Timestamp("2026-06-01T15:00:00Z"),
    )
    assert closed.pnl_abs > 0   # Runner-Portion
    assert closed.partial_pnl > 0
    assert closed.total_pnl == pytest.approx(closed.pnl_abs + closed.partial_pnl, abs=1e-6)
    assert closed.r_multiple > 1.0


def test_close_position_pure_sl_no_partials_unchanged():
    """SL direkt gehittet ohne Partials → pnl_abs<0, partial_pnl=0, total_pnl=pnl_abs.
    r_multiple ≈ -1 (Slippage am SL etwas schlechter)."""
    trade = _trade_btc_3tier_realistic()
    bar = _bar(50_000, 50_100, 49_400, 49_500, ts="2026-06-01T13:00:00Z")
    closed = paper_broker.close_position(
        trade, exit_price_raw=49_500.0, current_bar=bar, reason="sl",
        close_time=pd.Timestamp("2026-06-01T13:00:00Z"),
    )
    assert closed.partial_pnl == 0.0
    assert closed.pnl_abs < 0
    assert closed.total_pnl == pytest.approx(closed.pnl_abs, abs=1e-6)
    # ≈ -1R, mit etwas Slippage-Aufschlag
    assert -1.5 < closed.r_multiple < -0.9


# ── Pct-Sizing-Invariante: Summe muss originaler Size entsprechen ──────────

def test_partial_sizes_sum_to_original():
    """Nach TP1 + TP2: closed_sizes addieren sich zu pct1+pct2 der original_size."""
    trade = _trade_long_3tier()
    bar_tp1 = _bar(100, 112, 99, 111)
    bar_tp2 = _bar(111, 122, 110, 121)
    paper_broker.apply_tp1_partial(trade, bar_tp1, pd.Timestamp("2026-06-01T13:00:00Z"))
    paper_broker.apply_tp2_partial(trade, bar_tp2, pd.Timestamp("2026-06-01T14:00:00Z"))
    # Rest = pct3 × original_size
    assert trade.size == pytest.approx(10.0 * 0.2, abs=1e-6)


# ── Min-Size-Gate ────────────────────────────────────────────────────────

def test_open_position_rejects_tiny_lot_for_btc():
    """Wenn Risk-Multiplikatoren die Size unter MIN_SIZE['BTC']=0.01 drücken,
    wird der Trade übersprungen statt mit Mikro-Volumen geöffnet."""
    # equity=10000, risk_pct=0.0001 (extrem klein nach Multiplikatoren),
    # sl_dist=500 → size = 10000*0.0001/500 = 0.002 < min=0.01
    sig = _signal_with_multi_tp("long", entry=50_000, sl=49_500, tp=51_500,
                                tp1=None, tp2=None, pct1=0.5, pct2=0.5, pct3=0.0)
    bar = _bar(50_000, 50_010, 49_990, 50_000)
    trade = paper_broker.open_position(sig, equity=10_000, risk_pct=0.0001, current_bar=bar)
    assert trade is None


def test_open_position_accepts_normal_size_btc():
    """Normale Konfig: size=0.2 > min=0.01 → Trade geht durch."""
    sig = _signal_with_multi_tp("long", entry=50_000, sl=49_500, tp=51_500,
                                tp1=None, tp2=None, pct1=0.5, pct2=0.5, pct3=0.0)
    bar = _bar(50_000, 50_010, 49_990, 50_000)
    trade = paper_broker.open_position(sig, equity=10_000, risk_pct=0.01, current_bar=bar)
    assert trade is not None
    assert trade.size == pytest.approx(0.2, abs=0.01)
