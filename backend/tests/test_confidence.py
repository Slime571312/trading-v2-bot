"""Tests für Signal-Confidence-Score (Vault/Bot/Signal-Confidence-Scoring.md).

4 Faktoren (MTF / RR / Conviction / Regime), 4 Size-Tiers (skip/reduced/normal/high).
"""
from src.strategy_core.confidence import (
    ConfidenceContext,
    compute_confidence,
    size_tier_label,
)


def _ctx(**overrides) -> ConfidenceContext:
    defaults = dict(
        mtf_size_factor=1.0,
        rr=2.5,
        conviction_score=7,
        adx_regime="trending",
        atr_regime="normal",
    )
    defaults.update(overrides)
    return ConfidenceContext(**defaults)


# ── Score-Berechnung ─────────────────────────────────────────────────────


def test_confidence_perfect_setup_maxes_out():
    """Bester Fall — alle Faktoren ≈ 1.0."""
    ctx = _ctx(mtf_size_factor=1.0, rr=3.5, conviction_score=10,
               adx_regime="trending", atr_regime="normal")
    conf, breakdown, size_mult = compute_confidence(ctx)
    assert conf >= 0.95
    # Tier-High-Boost auf 1.0 zurückgenommen (Live-Analyse 2026-06-24: high zeigte
    # 90% WR aber nur 0.18R avg — kein R-Edge → kein Multiplikator gerechtfertigt).
    assert size_mult == 1.0
    assert breakdown["mtf"] == 1.0
    assert breakdown["rr"] == 1.0


def test_confidence_minimal_setup_skips():
    """Schlechtester Fall — kontradiktorisches Setup → < 0.50, Skip."""
    ctx = _ctx(mtf_size_factor=0.3, rr=1.2, conviction_score=2,
               adx_regime="ranging", atr_regime="high")
    conf, _, size_mult = compute_confidence(ctx)
    assert conf < 0.50
    assert size_mult == 0.0      # Skip


def test_confidence_solid_normal_tier():
    """Solides B-Setup mit H4-Retrace → normal-tier (1.0×)."""
    ctx = _ctx(mtf_size_factor=0.75, rr=2.0, conviction_score=7,
               adx_regime="trending", atr_regime="normal")
    conf, _, size_mult = compute_confidence(ctx)
    assert 0.70 <= conf < 0.85
    assert size_mult == 1.0


def test_confidence_reduced_tier_50_70():
    """Mittlere Confidence → 0.7× Size."""
    ctx = _ctx(mtf_size_factor=0.75, rr=1.8, conviction_score=5,
               adx_regime="uncertain", atr_regime="normal")
    conf, _, size_mult = compute_confidence(ctx)
    assert 0.50 <= conf < 0.70
    assert size_mult == 0.7


# ── Einzelne Faktoren ────────────────────────────────────────────────────


def test_rr_factor_brackets():
    """RR-Faktor: ≥3 →1.0, ≥2 →0.8, ≥1.5 →0.6, sonst 0.3."""
    assert compute_confidence(_ctx(rr=3.5))[1]["rr"] == 1.0
    assert compute_confidence(_ctx(rr=2.1))[1]["rr"] == 0.8
    assert compute_confidence(_ctx(rr=1.6))[1]["rr"] == 0.6
    assert compute_confidence(_ctx(rr=1.2))[1]["rr"] == 0.3


def test_conviction_factor_brackets():
    """Conviction-Faktor: ≥9 →1.0, ≥7 →0.85, ≥5 →0.6, sonst 0.3."""
    assert compute_confidence(_ctx(conviction_score=10))[1]["conviction"] == 1.0
    assert compute_confidence(_ctx(conviction_score=8))[1]["conviction"] == 0.85
    assert compute_confidence(_ctx(conviction_score=5))[1]["conviction"] == 0.6
    assert compute_confidence(_ctx(conviction_score=3))[1]["conviction"] == 0.3


def test_regime_alignment_best_case():
    """Trending + Normal-Vol = Best-Case (1.0)."""
    _, br, _ = compute_confidence(_ctx(adx_regime="trending", atr_regime="normal"))
    assert br["regime"] == 1.0


def test_regime_alignment_ranging_high_vol_worst():
    """Ranging + High-Vol = Worst-Case."""
    _, br, _ = compute_confidence(_ctx(adx_regime="ranging", atr_regime="high"))
    # ADX-Anteil ist 0, ATR-Anteil ist 0.2 → 0.2 gesamt
    assert br["regime"] < 0.3


def test_mtf_factor_uses_size_factor_directly():
    """MTF-Faktor = Pass-Through der bias.size_factor."""
    assert compute_confidence(_ctx(mtf_size_factor=1.0))[1]["mtf"] == 1.0
    assert compute_confidence(_ctx(mtf_size_factor=0.75))[1]["mtf"] == 0.75
    assert compute_confidence(_ctx(mtf_size_factor=0.5))[1]["mtf"] == 0.5


# ── Tier-Labels ──────────────────────────────────────────────────────────


def test_tier_labels():
    assert size_tier_label(0.40) == "skip"
    assert size_tier_label(0.60) == "reduced"
    assert size_tier_label(0.78) == "normal"
    assert size_tier_label(0.90) == "high"


# ── Gewichts-Summe ───────────────────────────────────────────────────────


def test_weights_sum_to_one():
    """Confidence muss bei Max-Faktoren genau 1.0 erreichen."""
    ctx = _ctx(mtf_size_factor=1.0, rr=4.0, conviction_score=10,
               adx_regime="trending", atr_regime="normal")
    conf, _, _ = compute_confidence(ctx)
    assert abs(conf - 1.0) < 1e-6
