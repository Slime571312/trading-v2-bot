"""Parameter Sensitivity Analysis — Grid Search & Robustness Scoring.

Spec: Trading/Bot/Parameter-Sensitivity-Analysis.md

Methode: 2D/3D Grid Search
  Input: Parameter-Ranges, backtest_fn
  Process: Alle Kombinationen backtesten, Heatmap erzeugen
  Output: Robustness-Scores, Optimal-Region, Empfehlungen

Use Case:
  - "Wie empfindlich ist Strategie auf RSI_THRESHOLD ±10%?"
  - "Welche SL-Einstellung ist robust?"
  - Fragile Parameter identifizieren + tuning-Prioritäten
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from ._types import BacktestMetrics

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SensitivityResult:
    """Resultat einer Sensitivity-Analyse."""
    param1_name: str
    param1_range: list[float]
    param2_name: str
    param2_range: list[float]
    results_matrix: list[list[BacktestMetrics]]  # [i][j]
    baseline_idx: tuple[int, int]
    heatmap: dict[str, list[list[float]]]  # {win_rate, return_pct, max_dd}
    robustness_scores: dict[str, float]  # {param1, param2, overall}
    robustness_bool: dict[str, bool]  # {param1_robust, param2_robust}
    optimal_region: dict[str, Any]
    recommendations: list[str]


def grid_search_2d(
    param1_name: str,
    param1_range: list[float],
    param2_name: str,
    param2_range: list[float],
    backtest_fn: Callable[[float, float], BacktestMetrics],
) -> SensitivityResult:
    """
    2D-Grid Search: Teste alle Parameterkombinationen.

    Args:
        param1_name: z.B. "RSI_THRESHOLD"
        param1_range: z.B. [25, 27.5, 30, 32.5, 35]
        param2_name: z.B. "STOPLOSS_PCT"
        param2_range: z.B. [3.0, 4.0, 5.0, 6.0, 7.0]
        backtest_fn: lambda p1, p2 → BacktestMetrics

    Returns:
        SensitivityResult mit Heatmap, Scores, Recommendations
    """
    if not param1_range or not param2_range:
        log.error("Grid search: empty parameter ranges")
        raise ValueError("Parameter ranges must not be empty")

    log.info(f"Starting 2D Grid Search: {param1_name} × {param2_name} "
             f"({len(param1_range)} × {len(param2_range)} = {len(param1_range) * len(param2_range)} backtests)")

    # Build results matrix
    results_matrix: list[list[BacktestMetrics]] = []

    for i, p1 in enumerate(param1_range):
        row: list[BacktestMetrics] = []
        for j, p2 in enumerate(param2_range):
            try:
                metrics = backtest_fn(p1, p2)
                row.append(metrics)
                log.debug(f"  [{i},{j}] {param1_name}={p1}, {param2_name}={p2} "
                         f"→ WR={metrics.win_rate:.1%}")
            except Exception as e:
                log.error(f"Backtest failed at [{i},{j}]: {e}")
                # Fallback: Dummy-Metrics mit 0 Trades
                row.append(BacktestMetrics(
                    total_trades=0, wins=0, losses=0, win_rate=0.0,
                    total_return_pct=0.0, avg_win_r=0.0, avg_loss_r=0.0,
                    profit_factor=None, max_drawdown_pct=0.0, expectancy_r=0.0,
                    sharpe=None, exposure_pct=0.0, longs=0, shorts=0,
                ))
        results_matrix.append(row)

    # Baseline = center point
    baseline_i = len(param1_range) // 2
    baseline_j = len(param2_range) // 2
    baseline = results_matrix[baseline_i][baseline_j]

    # Build heatmaps (für JSON/Frontend)
    heatmap_wr = [[results_matrix[i][j].win_rate * 100.0
                    for j in range(len(param2_range))]
                   for i in range(len(param1_range))]
    heatmap_return = [[results_matrix[i][j].total_return_pct
                       for j in range(len(param2_range))]
                      for i in range(len(param1_range))]
    heatmap_dd = [[results_matrix[i][j].max_drawdown_pct
                   for j in range(len(param2_range))]
                  for i in range(len(param1_range))]

    heatmap = {
        "win_rate": heatmap_wr,
        "return_pct": heatmap_return,
        "max_dd": heatmap_dd,
    }

    # Robustness Scores
    scores = _compute_robustness_scores(
        results_matrix, baseline, (baseline_i, baseline_j),
        param1_name, param2_name
    )

    # Optimal Region
    optimal_region = _find_optimal_region(
        results_matrix, baseline, (baseline_i, baseline_j),
        param1_range, param2_range, param1_name, param2_name
    )

    # Recommendations
    recommendations = _generate_recommendations(
        scores, optimal_region, baseline, param1_name, param2_name
    )

    return SensitivityResult(
        param1_name=param1_name,
        param1_range=param1_range,
        param2_name=param2_name,
        param2_range=param2_range,
        results_matrix=results_matrix,
        baseline_idx=(baseline_i, baseline_j),
        heatmap=heatmap,
        robustness_scores=scores["scores"],
        robustness_bool=scores["robust_bool"],
        optimal_region=optimal_region,
        recommendations=recommendations,
    )


def _compute_robustness_scores(
    results_matrix: list[list[BacktestMetrics]],
    baseline: BacktestMetrics,
    baseline_idx: tuple[int, int],
    param1_name: str,
    param2_name: str,
) -> dict:
    """
    Berechne Robustness Scores für beide Parameter.

    Formel:
      robustness_score = 100 × (1 - range / baseline)

      range = max(results) - min(results)
      baseline = WR bei center point

    Interpretation:
      80–100: Sehr robust
      60–80:  Robust
      40–60:  Mittelmäßig
      < 40:   Fragil
    """
    baseline_wr = baseline.win_rate

    if baseline_wr == 0:
        return {"scores": {}, "robust_bool": {}}

    # Param1 Robustness: Variiere nur param1, halte param2 konstant
    param2_idx = baseline_idx[1]
    p1_wrs = [results_matrix[i][param2_idx].win_rate for i in range(len(results_matrix))]
    p1_range = max(p1_wrs) - min(p1_wrs)
    p1_score = max(0, 100 * (1 - p1_range / baseline_wr))

    # Param2 Robustness: Variiere nur param2, halte param1 konstant
    param1_idx = baseline_idx[0]
    p2_wrs = [results_matrix[param1_idx][j].win_rate for j in range(len(results_matrix[0]))]
    p2_range = max(p2_wrs) - min(p2_wrs)
    p2_score = max(0, 100 * (1 - p2_range / baseline_wr))

    # Overall Score (Mittelwert)
    overall_score = (p1_score + p2_score) / 2

    scores = {
        param1_name: p1_score,
        param2_name: p2_score,
        "overall": overall_score,
    }

    robust_bool = {
        f"{param1_name}_robust": p1_score >= 60,
        f"{param2_name}_robust": p2_score >= 60,
    }

    return {"scores": scores, "robust_bool": robust_bool}


def _find_optimal_region(
    results_matrix: list[list[BacktestMetrics]],
    baseline: BacktestMetrics,
    baseline_idx: tuple[int, int],
    param1_range: list[float],
    param2_range: list[float],
    param1_name: str,
    param2_name: str,
) -> dict:
    """
    Finde rechteckige Region mit Top-Performance.

    Threshold: Top 80% (90th percentile) der Win-Rate
    """
    # Alle WRs sammeln
    all_wrs = []
    for row in results_matrix:
        for metrics in row:
            all_wrs.append(metrics.win_rate)

    if not all_wrs:
        return {"region_bounds": None, "message": "No data"}

    threshold = np.percentile(all_wrs, 80)  # Top 20%

    # Finde min/max i, j wo WR >= threshold
    min_i, max_i = len(results_matrix), -1
    min_j, max_j = len(results_matrix[0]), -1

    for i in range(len(results_matrix)):
        for j in range(len(results_matrix[0])):
            if results_matrix[i][j].win_rate >= threshold:
                min_i = min(min_i, i)
                max_i = max(max_i, i)
                min_j = min(min_j, j)
                max_j = max(max_j, j)

    if min_i > max_i:
        return {"region_bounds": None, "message": "No optimal region found"}

    # Safe zone description
    p1_min, p1_max = param1_range[min_i], param1_range[max_i]
    p2_min, p2_max = param2_range[min_j], param2_range[max_j]

    # Stats für diese Region
    region_wrs = [results_matrix[i][j].win_rate
                  for i in range(min_i, max_i + 1)
                  for j in range(min_j, max_j + 1)]

    region_perf = {
        "avg_win_rate": float(np.mean(region_wrs)) if region_wrs else 0.0,
        "std_win_rate": float(np.std(region_wrs)) if region_wrs else 0.0,
        "count_points": len(region_wrs),
    }

    return {
        "region_bounds": [[min_i, max_i], [min_j, max_j]],
        "region_params": {
            param1_name: (p1_min, p1_max),
            param2_name: (p2_min, p2_max),
        },
        "safe_zone": f"{param1_name}: [{p1_min}, {p1_max}], {param2_name}: [{p2_min}, {p2_max}]",
        "region_performance": region_perf,
    }


def _generate_recommendations(
    scores: dict,
    optimal_region: dict,
    baseline: BacktestMetrics,
    param1_name: str,
    param2_name: str,
) -> list[str]:
    """
    Erzeugle menschenlesbare Recommendations basierend auf Scores.
    """
    recommendations: list[str] = []

    p1_score = scores["scores"].get(param1_name, 0)
    p2_score = scores["scores"].get(param2_name, 0)

    # Param1 Recommendations
    if p1_score >= 80:
        recommendations.append(f"✓ {param1_name} is very robust (score {p1_score:.0f}). "
                               "Can vary ±20% without issues.")
    elif p1_score >= 60:
        recommendations.append(f"✓ {param1_name} is robust (score {p1_score:.0f}). "
                               "Can vary ±10% safely.")
    elif p1_score >= 40:
        recommendations.append(f"⚠ {param1_name} is moderately sensitive (score {p1_score:.0f}). "
                               "Keep within ±5%.")
    else:
        recommendations.append(f"✗ {param1_name} is FRAGILE (score {p1_score:.0f}). "
                               "Must be precisely set. Consider re-design.")

    # Param2 Recommendations
    if p2_score >= 80:
        recommendations.append(f"✓ {param2_name} is very robust (score {p2_score:.0f}). "
                               "Can vary ±20% without issues.")
    elif p2_score >= 60:
        recommendations.append(f"✓ {param2_name} is robust (score {p2_score:.0f}). "
                               "Can vary ±10% safely.")
    elif p2_score >= 40:
        recommendations.append(f"⚠ {param2_name} is moderately sensitive (score {p2_score:.0f}). "
                               "Keep within ±5%.")
    else:
        recommendations.append(f"✗ {param2_name} is FRAGILE (score {p2_score:.0f}). "
                               "Must be precisely set. Consider dynamic tuning (ATR, ADR).")

    # Optimal region recommendation
    if optimal_region.get("region_bounds"):
        safe_zone = optimal_region.get("safe_zone", "")
        recommendations.append(f"💡 Optimal region found: {safe_zone}")

    return recommendations


def sensitivity_score(
    results_matrix: list[list[BacktestMetrics]],
    baseline_idx: tuple[int, int],
    metric_name: str = "win_rate",
) -> dict:
    """
    Compute robustness score per parameter for a given metric.

    Args:
        results_matrix: 2D array of BacktestMetrics
        baseline_idx: (i, j) center point
        metric_name: "win_rate", "total_return_pct", "max_drawdown_pct"

    Returns:
        {
            "param1_score": 85,
            "param2_score": 42,
            "overall_score": 64,
            "param1_robust": True,
            "param2_robust": False,
        }
    """
    # Extract metric values from matrix
    def get_metric(metrics: BacktestMetrics) -> float:
        if metric_name == "win_rate":
            return metrics.win_rate
        elif metric_name == "total_return_pct":
            return metrics.total_return_pct
        elif metric_name == "max_drawdown_pct":
            return abs(metrics.max_drawdown_pct)  # Absolute for scoring
        else:
            return 0.0

    baseline = get_metric(results_matrix[baseline_idx[0]][baseline_idx[1]])

    if baseline == 0:
        return {"overall": 0, "param1": 0, "param2": 0}

    # Param1 (vary row)
    param2_idx = baseline_idx[1]
    p1_values = [get_metric(results_matrix[i][param2_idx]) for i in range(len(results_matrix))]
    p1_range = max(p1_values) - min(p1_values)
    p1_score = max(0, 100 * (1 - p1_range / baseline)) if baseline else 0

    # Param2 (vary col)
    param1_idx = baseline_idx[0]
    p2_values = [get_metric(results_matrix[param1_idx][j]) for j in range(len(results_matrix[0]))]
    p2_range = max(p2_values) - min(p2_values)
    p2_score = max(0, 100 * (1 - p2_range / baseline)) if baseline else 0

    overall = (p1_score + p2_score) / 2

    return {
        "overall": int(overall),
        "param1": int(p1_score),
        "param2": int(p2_score),
        "param1_robust": p1_score >= 60,
        "param2_robust": p2_score >= 60,
    }


def find_optimal_region(
    results_matrix: list[list[BacktestMetrics]],
    metric_name: str = "win_rate",
    threshold_percentile: float = 0.8,
) -> dict:
    """
    Find rectangular region with top performance.

    Args:
        results_matrix: 2D matrix of BacktestMetrics
        metric_name: which metric to optimize
        threshold_percentile: e.g., 0.8 = top 20%

    Returns:
        {
            "region_bounds": [[i_min, i_max], [j_min, j_max]],
            "region_performance": {...},
            "count": 12  # how many points in region
        }
    """
    def get_metric(metrics: BacktestMetrics) -> float:
        if metric_name == "win_rate":
            return metrics.win_rate
        elif metric_name == "total_return_pct":
            return metrics.total_return_pct
        elif metric_name == "max_drawdown_pct":
            return abs(metrics.max_drawdown_pct)
        return 0.0

    # Collect all metric values
    all_values = [get_metric(results_matrix[i][j])
                  for i in range(len(results_matrix))
                  for j in range(len(results_matrix[0]))]

    if not all_values:
        return {"region_bounds": None}

    threshold = np.percentile(all_values, threshold_percentile * 100)

    # Find bounds
    min_i, max_i = len(results_matrix), -1
    min_j, max_j = len(results_matrix[0]), -1

    for i in range(len(results_matrix)):
        for j in range(len(results_matrix[0])):
            if get_metric(results_matrix[i][j]) >= threshold:
                min_i = min(min_i, i)
                max_i = max(max_i, i)
                min_j = min(min_j, j)
                max_j = max(max_j, j)

    if min_i > max_i:
        return {"region_bounds": None}

    region_values = [get_metric(results_matrix[i][j])
                     for i in range(min_i, max_i + 1)
                     for j in range(min_j, max_j + 1)]

    return {
        "region_bounds": [[min_i, max_i], [min_j, max_j]],
        "region_performance": {
            f"avg_{metric_name}": float(np.mean(region_values)),
            f"std_{metric_name}": float(np.std(region_values)),
        },
        "count": len(region_values),
    }
