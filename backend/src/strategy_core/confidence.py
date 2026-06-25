"""Signal-Confidence-Score 0–100 % nach Bot/Signal-Confidence-Scoring.md.

Komplementär zum existierenden Conviction-Score (10-Punkte-Hard-Gate):
- Conviction klassifiziert A/B/skip
- Confidence verfeinert die Position-Size in einer kontinuierlichen Skala

Vier Faktoren — Liquidity (aus Vault-Note 20 %) ist im Capital-Paper-Mode
nicht messbar (Market-Maker, kein Order-Book) und wird weggelassen; die
20 % werden auf MTF (+5 %) und Conviction (+5 %) verteilt, weil das in der
Forschung die zwei stärksten Prädiktoren sind.

| Faktor | Gewicht | Quelle |
|---|---|---|
| MTF-Confluence | 0.30 | bias.multi_tf_confluence size_factor |
| RR-Quality | 0.25 | signal.rr |
| Conviction | 0.25 | conviction_score / 10 |
| Regime-Alignment | 0.20 | adx_regime + atr_regime |

Output: tuple (confidence_pct, breakdown_dict, size_multiplier).
size_multiplier: <50 % → 0.0 (skip), 50–70 % → 0.7, 70–85 % → 1.0, ≥85 % → 1.25.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# Gewichte — summieren zu 1.0
_W_MTF = 0.30
_W_RR = 0.25
_W_CONVICTION = 0.25
_W_REGIME = 0.20

# Confidence-Tiers für Position-Size-Modulation
_TIER_SKIP_BELOW = 0.50      # < 50 % → kein Trade
_TIER_REDUCED_BELOW = 0.70   # 50–70 % → 0.7×
_TIER_NORMAL_BELOW = 0.85    # 70–85 % → 1.0×
                              # ≥ 85 % → 1.25× (Overcommit auf High-Conviction)
_SIZE_MULT_REDUCED = 0.7
_SIZE_MULT_NORMAL = 1.0
# Tier-High-Boost auf 1.0 zurückgenommen (Live-Analyse 2026-06-24): high zeigte 90%
# WR aber nur +0.18R avg — Confidence-Score korreliert mit Win-Probability nicht mit
# R-Multiple. Position × 1.25 vergrößert daher nur Mini-Wins, keine Big-Wins.
# Wenn künftig ein R-Forecast in den Score kommt, Boost wieder aktivieren.
_SIZE_MULT_HIGH = 1.0


@dataclass
class ConfidenceContext:
    """Inputs für die 4 Faktoren — alle bereits in der Engine berechnet."""
    mtf_size_factor: float       # bias.multi_tf_confluence → 0.0/0.5/0.75/1.0
    rr: float                    # signal.rr
    conviction_score: int        # 0–10 aus compute_conviction_score
    adx_regime: str              # "trending" | "uncertain" | "ranging"
    atr_regime: str              # "low" | "normal" | "high"


def _factor_mtf(mtf_size_factor: float) -> float:
    """MTF-Confluence — direkt aus bias.multi_tf_confluence übernommen.

    aligned=1.0, h4_neutral=0.75, retrace=0.5, no_daily=0.0.
    """
    return max(0.0, min(1.0, mtf_size_factor))


def _factor_rr(rr: float) -> float:
    """RR-Quality — höheres RR = besseres Setup.

    Schwellen aus Vault: ≥3 →1.0, ≥2 →0.8, ≥1.5 →0.6, sonst →0.3.
    """
    if rr >= 3.0:
        return 1.0
    if rr >= 2.0:
        return 0.8
    if rr >= 1.5:
        return 0.6
    return 0.3


def _factor_conviction(conviction_score: int) -> float:
    """Conviction-Score 0–10 → 0.0–1.0 mit leichter S-Kurve.

    Ein Score von 5 ist schon „B-Setup tauglich" → Mid-Range.
    Schwellen: ≥9→1.0, ≥7→0.85, ≥5→0.6, sonst →0.3.
    """
    if conviction_score >= 9:
        return 1.0
    if conviction_score >= 7:
        return 0.85
    if conviction_score >= 5:
        return 0.6
    return 0.3


def _factor_regime(adx_regime: str, atr_regime: str) -> float:
    """Regime-Alignment — Trending+Normal-Vol ist Best-Case für ICT-Sweeps.

    Trending-Markt mit normaler Vol → 1.0 (Setup-Pattern funktioniert sauber).
    Ranging oder High-Vol → 0.5 (Setups werden False-Breakouts oder choppy).
    """
    score = 0.0
    if adx_regime == "trending":
        score += 0.6
    elif adx_regime == "uncertain":
        score += 0.4
    # ranging → 0.0 hinzu (im Ranging-Markt sind ICT-Setups schwach)
    if atr_regime == "normal":
        score += 0.4
    elif atr_regime == "low":
        score += 0.3       # ruhig — okay aber Runner unwahrscheinlich
    else:                  # high — Setups breit, mehr False-Breaks
        score += 0.2
    return max(0.0, min(1.0, score))


def compute_confidence(ctx: ConfidenceContext) -> tuple[float, dict, float]:
    """Returns (confidence_pct, breakdown, size_multiplier).

    confidence_pct ∈ [0.0, 1.0].
    size_multiplier ∈ {0.0, 0.7, 1.0, 1.25}. 0.0 bedeutet Skip.
    """
    f_mtf = _factor_mtf(ctx.mtf_size_factor)
    f_rr = _factor_rr(ctx.rr)
    f_conv = _factor_conviction(ctx.conviction_score)
    f_regime = _factor_regime(ctx.adx_regime, ctx.atr_regime)

    confidence = (
        f_mtf * _W_MTF
        + f_rr * _W_RR
        + f_conv * _W_CONVICTION
        + f_regime * _W_REGIME
    )
    breakdown = {
        "mtf": round(f_mtf, 3),
        "rr": round(f_rr, 3),
        "conviction": round(f_conv, 3),
        "regime": round(f_regime, 3),
        "weighted_sum": round(confidence, 3),
    }

    if confidence < _TIER_SKIP_BELOW:
        size_mult = 0.0
    elif confidence < _TIER_REDUCED_BELOW:
        size_mult = _SIZE_MULT_REDUCED
    elif confidence < _TIER_NORMAL_BELOW:
        size_mult = _SIZE_MULT_NORMAL
    else:
        size_mult = _SIZE_MULT_HIGH

    return confidence, breakdown, size_mult


def size_tier_label(confidence: float) -> Literal["skip", "reduced", "normal", "high"]:
    """Lesbares Label für Logs/Dashboard."""
    if confidence < _TIER_SKIP_BELOW:
        return "skip"
    if confidence < _TIER_REDUCED_BELOW:
        return "reduced"
    if confidence < _TIER_NORMAL_BELOW:
        return "normal"
    return "high"
