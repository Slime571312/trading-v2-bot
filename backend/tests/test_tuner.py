"""Unit-Tests für Tuner-State + Grid-Scoring (ohne Capital + ohne Anthropic)."""
from __future__ import annotations

import pytest

from src.tuner.grid_search import GridCombo, GridResult, _score
from src.tuner.state import TunerHistory, TunerRun, load_history, save_history, new_run


# ── Scoring ─────────────────────────────────────────────────────────────


def test_score_below_min_trades_is_huge_negative():
    """<5 Trades → score wird ausgeschlossen (statistisch unsignifikant)."""
    assert _score(4, 1.0, 2.0) < -1e8


def test_score_above_min_trades_uses_expectancy():
    s = _score(10, 0.5, 1.5)
    # expectancy 0.5 + log(1.5) × 0.05 ≈ 0.5 + 0.02 = ~0.52
    assert 0.4 < s < 0.6


def test_score_profit_factor_tiebreaker():
    """Bei gleicher Expectancy gewinnt das mit höherem PF."""
    s_low_pf = _score(20, 0.3, 1.1)
    s_high_pf = _score(20, 0.3, 2.5)
    assert s_high_pf > s_low_pf


# ── GridResult Properties ───────────────────────────────────────────────


def _combo(rr: float, sl: int, score: float) -> GridCombo:
    return GridCombo(
        rr_threshold=rr, sweep_lookback=sl,
        total_trades=20, win_rate=0.6,
        expectancy_r=score, profit_factor=1.5,
        total_return_pct=10.0, max_drawdown_pct=-3.0, sharpe=1.0,
        score=score,
    )


def test_best_returns_first_combo():
    g = GridResult(instrument="BTC", iter_tf="5m", bars_used=1000,
                   combos=[_combo(2.0, 10, 0.5), _combo(2.5, 10, 0.3)])
    assert g.best is not None
    assert g.best.rr_threshold == 2.0


def test_best_is_none_for_empty():
    g = GridResult(instrument="BTC", iter_tf="5m", bars_used=1000, combos=[])
    assert g.best is None


def test_improvement_calculation():
    g = GridResult(
        instrument="BTC", iter_tf="5m", bars_used=1000,
        combos=[_combo(2.0, 10, 0.7)],
        current_rr_threshold=2.0, current_sweep_lookback=10, current_score=0.3,
    )
    assert g.improvement_vs_current is not None
    assert abs(g.improvement_vs_current - 0.4) < 1e-6


def test_improvement_none_without_current_score():
    g = GridResult(
        instrument="BTC", iter_tf="5m", bars_used=1000,
        combos=[_combo(2.0, 10, 0.7)],
        current_rr_threshold=2.0, current_sweep_lookback=10, current_score=None,
    )
    assert g.improvement_vs_current is None


def test_grid_result_serialization_roundtrip():
    g = GridResult(
        instrument="BTC", iter_tf="5m", bars_used=1000,
        combos=[_combo(2.0, 10, 0.5)],
        current_rr_threshold=2.0, current_sweep_lookback=10, current_score=0.3,
    )
    d = g.to_json()
    assert d["instrument"] == "BTC"
    assert d["best"]["rr_threshold"] == 2.0
    assert d["improvement_vs_current"] is not None


# ── Tuner-History Persistenz ────────────────────────────────────────────


def test_new_run_creates_unique_ids():
    r1 = new_run(["BTC"], "5m", 500, "manual")
    r2 = new_run(["BTC"], "5m", 500, "manual")
    assert r1.id != r2.id
    assert r1.id.startswith("t_")
    assert r1.status == "running"


def test_save_load_history_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tuner.state.TUNER_FILE", tmp_path / "tuner.json")

    h = TunerHistory(runs=[new_run(["BTC", "DE40"], "5m", 800, "manual")])
    h.runs[0].status = "completed"
    h.runs[0].finished_at = "2026-05-28T03:30:00+00:00"
    h.runs[0].proposal_ids = ["p_abc"]
    save_history(h)

    loaded = load_history()
    assert len(loaded.runs) == 1
    assert loaded.runs[0].proposal_ids == ["p_abc"]
    assert loaded.runs[0].status == "completed"


def test_save_history_caps_at_50(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tuner.state.TUNER_FILE", tmp_path / "tuner.json")
    h = TunerHistory(runs=[new_run(["BTC"], "5m", 500, "manual") for _ in range(60)])
    save_history(h)
    loaded = load_history()
    assert len(loaded.runs) == 50  # älteste gedroppt
