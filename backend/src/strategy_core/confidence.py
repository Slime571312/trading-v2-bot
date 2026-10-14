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
# R-Forecast 2026-06-27 reaktiviert: 25 % aus Conviction/RR/Regime abgezogen
_W_MTF = 0.30
_W_RR = 0.20
_W_CONVICTION = 0.20
_W_REGIME = 0.15
_W_R_FORECAST = 0.15

# Confidence-Tiers für Position-Size-Modulation
# ERHÖHT 2026-10-14: 50% → 60% (zu viele losing trades mit niedrig confidence)
_TIER_SKIP_BELOW = 0.60      # < 60 % → kein Trade (ERHÖHT von 50%)
_TIER_REDUCED_BELOW = 0.75   # 60–75 % → 0.7× (SHIFTED up)
_TIER_NORMAL_BELOW = 0.87    # 75–87 % → 1.0× (SHIFTED up)
                              # ≥ 87 % → 1.25× (mit R-Forecast)
_SIZE_MULT_REDUCED = 0.7
_SIZE_MULT_NORMAL = 1.0
# Tier-High-Boost am 27.06. wieder auf 1.25 angehoben, weil der R-Forecast als
# 5. Faktor jetzt mit reinläuft. Ein Tier "high" mit niedrigem R-Forecast (Mini-Win
# erwartet) erreicht 85 % gar nicht erst → kein blindes Boosting mehr.
_SIZE_MULT_HIGH = 1.25


@dataclass
class ConfidenceContext:
    """Inputs für die Faktoren — alle bereits in der Engine berechnet."""
    mtf_size_factor: float       # bias.multi_tf_confluence → 0.0/0.5/0.75/1.0
    rr: float                    # signal.rr
    conviction_score: int        # 0–10 aus compute_conviction_score
    adx_regime: str              # "trending" | "uncertain" | "ranging"
    atr_regime: str              # "low" | "normal" | "high"
    # R-Forecast (Live-Analyse 2026-06-27): erwartete R-Multiple bei TP1-Hit.
    # = sl_distance / tp1_distance. Hohe Werte → erwarteter Big-Win.
    # tp1_distance_from_entry < 0.5 × sl_distance → Mini-Win-Setup → niedriger Faktor.
    tp1_distance: float | None = None    # |tp1 - entry|; None → R-Forecast ignoriert
    sl_distance: float | None = None     # |sl - entry|


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


def _factor_r_forecast(tp1_distance: float | None, sl_distance: float | None) -> float:
    """R-Forecast — wie viel R kann TP1 realistisch bringen?

    Verhindert das Mini-Win-Problem (Live-Analyse 2026-06-24): Trades mit R<0.3
    trotz TP-Hit waren der Hauptgrund warum Tier-High-Boost rausgenommen wurde.
    TP1 zu nah am Entry → R-Forecast niedrig → Tier-High wird nicht erreicht
    → Position-Boost geht nicht auf Mini-Win-Setups.

    Schwellen:
    - tp1_R ≥ 1.5 → 1.0 (sehr gut, Edge-Setup mit echtem Wachstumspotenzial)
    - tp1_R ≥ 1.0 → 0.8
    - tp1_R ≥ 0.7 → 0.6 (akzeptabel)
    - tp1_R ≥ 0.5 → 0.4 (knapp über Min-Distance)
    - sonst        → 0.2 (Mini-Win-Risiko)

    None → 0.5 (neutral, falls TP1 nicht gesetzt = Legacy-Pfad).
    """
    if tp1_distance is None or sl_distance is None or sl_distance <= 0:
        return 0.5
    tp1_r = tp1_distance / sl_distance
    if tp1_r >= 1.5:
        return 1.0
    if tp1_r >= 1.0:
        return 0.8
    if tp1_r >= 0.7:
        return 0.6
    if tp1_r >= 0.5:
        return 0.4
    return 0.2


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
    f_r_forecast = _factor_r_forecast(ctx.tp1_distance, ctx.sl_distance)

    confidence = (
        f_mtf * _W_MTF
        + f_rr * _W_RR
        + f_conv * _W_CONVICTION
        + f_regime * _W_REGIME
        + f_r_forecast * _W_R_FORECAST
    )
    breakdown = {
        "mtf": round(f_mtf, 3),
        "rr": round(f_rr, 3),
        "conviction": round(f_conv, 3),
        "regime": round(f_regime, 3),
        "r_forecast": round(f_r_forecast, 3),
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
    """[LEGACY] Label aus RAW confidence-Wert. Behalte für Tests, nicht neu nutzen."""
    if confidence < _TIER_SKIP_BELOW:
        return "skip"
    if confidence < _TIER_REDUCED_BELOW:
        return "reduced"
    if confidence < _TIER_NORMAL_BELOW:
        return "normal"
    return "high"


# ── Neue Label-Funktion basierend auf finalem size_mult (Bug-Fix 2026-08-10) ──
# Bisher wurde tier_label aus dem post-Modifier-Wert von _confidence berechnet,
# was inkonsistent zur Skip-Entscheidung war (die vor Modifikation greift).
# Konsequenz: Trades mit ursprünglich starkem Setup, die durch Ensemble+Sweep
# gedämpft wurden, landeten mit Tag "skip" im Journal — wurden aber tatsächlich
# ausgeführt. Fix: Label aus dem finalen Size-Multiplier ableiten.

_LABEL_VERY_REDUCED_BELOW = 0.55  # < 0.55× → sehr klein
_LABEL_REDUCED_BELOW = 0.85       # 0.55-0.85 → reduced
_LABEL_NORMAL_BELOW = 1.10        # 0.85-1.10 → normal
                                   # ≥ 1.10 → high

# Wenn nach allen Multiplikatoren die effektive Size unter dieser Schwelle
# liegt, wird der Trade final geskippt (auch wenn die RAW-Confidence-Klasse
# durchgekommen wäre). Verhindert Micro-Trades die den Spread nicht schlagen.
FINAL_SKIP_SIZE_MULT_BELOW = 0.30


def size_tier_from_mult(size_mult: float) -> Literal["skip", "very_reduced", "reduced", "normal", "high"]:
    """Label aus dem FINALEN Size-Multiplier (nach Ensemble + Sweep + andere)."""
    if size_mult < FINAL_SKIP_SIZE_MULT_BELOW:
        return "skip"
    if size_mult < _LABEL_VERY_REDUCED_BELOW:
        return "very_reduced"
    if size_mult < _LABEL_REDUCED_BELOW:
        return "reduced"
    if size_mult < _LABEL_NORMAL_BELOW:
        return "normal"
    return "high"
