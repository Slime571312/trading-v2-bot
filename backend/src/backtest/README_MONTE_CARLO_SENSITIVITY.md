# Monte Carlo & Sensitivity Analysis Modules

## Overview

Two research modules for advanced backtest analysis:

1. **Monte Carlo Backtesting** (`monte_carlo.py`)
   - Assess strategy robustness via statistical simulation
   - Methods: Shuffle (1000 scenarios), Resample (100 scenarios)
   - Output: Worst-case metrics, confidence intervals

2. **Parameter Sensitivity Analysis** (`sensitivity.py`)
   - Find optimal parameter ranges
   - Detect brittle vs. robust parameters
   - Generate robustness scores & recommendations

---

## Quick Start

### Monte Carlo Shuffle (Fastest)

```python
from src.backtest.monte_carlo import monte_carlo_shuffle

# Run after historical backtest
result = monte_carlo_shuffle(
    trades=backtest_result.trades,
    n_simulations=1000,
    initial_capital=10_000.0,
    risk_pct=0.01,
)

print(f"Worst-case DD: {result['worst_case_drawdown_pct']:.1f}%")
print(f"Prob-Profit: {result['prob_profit']:.0%}")
print(f"Return CI: {result['ci_return']}")
# Output: Worst-case DD: -15.3%, Prob-Profit: 72%, Return CI: (5.0, 28.0)
```

### Parameter Sensitivity Grid

```python
from src.backtest.sensitivity import grid_search_2d

def backtest_with_params(rsi_threshold, stoploss_pct):
    # Run backtest with these params, return BacktestMetrics
    return run_backtest(
        strategy=MyStrategy(rsi_threshold=rsi_threshold),
        stoploss_pct=stoploss_pct,
    )

result = grid_search_2d(
    param1_name="RSI_THRESHOLD",
    param1_range=[25, 27.5, 30, 32.5, 35],
    param2_name="STOPLOSS_PCT",
    param2_range=[3.0, 4.0, 5.0, 6.0, 7.0],
    backtest_fn=backtest_with_params,
)

# Get heatmap
heatmap_wr = result.heatmap["win_rate"]
# [45, 48, 52, 49, 42]
# [50, 54, 58, 55, 48]
# [52, 56, 60, 57, 50]  ← Row 2 (baseline)
# [48, 52, 54, 51, 44]
# [42, 46, 48, 45, 38]

# Get recommendations
for rec in result.recommendations:
    print(rec)
# Output:
# ✓ RSI_THRESHOLD is robust (score 72). Can vary ±10% safely.
# ✗ STOPLOSS_PCT is FRAGILE (score 38). Must be precisely set.
```

---

## Integration with Backtest API

### Proposed Endpoint: Monte Carlo

```bash
curl -X POST http://localhost:8000/bot/BTC/backtest/monte-carlo \
  -H "Content-Type: application/json" \
  -d '{
    "method": "shuffle",
    "n_simulations": 1000,
    "risk_pct": 0.01
  }'
```

Response:
```json
{
  "method": "shuffle",
  "n_simulations": 1000,
  "total_trades": 55,
  "worst_case_drawdown_pct": -15.3,
  "prob_profit": 0.72,
  "ci_return": [5.0, 28.0],
  "ci_dd": [-2.0, -18.0],
  "message": "72% of scenarios profitable, worst-case DD: -15.3%"
}
```

### Proposed Endpoint: Sensitivity

```bash
curl -X POST http://localhost:8000/bot/BTC/backtest/sensitivity \
  -H "Content-Type: application/json" \
  -d '{
    "param1_name": "RSI_THRESHOLD",
    "param1_range": [25, 27.5, 30, 32.5, 35],
    "param2_name": "STOPLOSS_PCT",
    "param2_range": [3.0, 4.0, 5.0, 6.0, 7.0]
  }'
```

Response:
```json
{
  "heatmap": {
    "win_rate": [[45, 48, 52, 49, 42], ...],
    "return_pct": [[8, 10, 12, 11, 7], ...]
  },
  "robustness_scores": {
    "RSI_THRESHOLD": 72,
    "STOPLOSS_PCT": 38
  },
  "optimal_region": {
    "safe_zone": "RSI: [28, 32], SL: [4.5%, 5.5%]",
    "region_performance": {"avg_win_rate": 0.57, "std_win_rate": 0.015}
  },
  "recommendations": [
    "RSI is robust (±10% OK)",
    "StopLoss is FRAGILE (must be precise)"
  ]
}
```

---

## Features & Outputs

### Monte Carlo Shuffle

**Input:**
- `trades`: List of BacktestTrade objects
- `n_simulations`: Default 1000
- `initial_capital`: Default 10,000
- `risk_pct`: Default 0.01 (1%)
- `seed`: Optional for reproducibility

**Output:**
```python
{
    "method": "shuffle",
    "n_simulations": 1000,
    "total_trades": 55,
    "scenarios": [ScenarioResult, ...],
    "worst_case_drawdown_pct": -15.3,
    "prob_profit": 0.72,
    "ci_return": (5.0, 28.0),       # 5th & 95th percentile
    "ci_dd": (-2.0, -18.0),
    "statistics": {
        "avg_return": 15.3,
        "std_return": 7.2,
        "avg_dd": -8.5,
        "std_dd": 3.2,
    }
}
```

### Parameter Sensitivity

**Input:**
- `param1_name`, `param1_range`: First parameter
- `param2_name`, `param2_range`: Second parameter
- `backtest_fn`: Callable that returns BacktestMetrics

**Output:**
```python
SensitivityResult(
    param1_name="RSI_THRESHOLD",
    param1_range=[25, 27.5, 30, 32.5, 35],
    param2_name="STOPLOSS_PCT",
    param2_range=[3.0, 4.0, 5.0, 6.0, 7.0],
    results_matrix=[[BacktestMetrics, ...], ...],  # 5×5 matrix
    baseline_idx=(2, 2),  # Center point
    heatmap={
        "win_rate": [[45.0, 48.0, ...], ...],  # % format
        "return_pct": [[8.0, 10.0, ...], ...],
        "max_dd": [[-5.2, -6.1, ...], ...],
    },
    robustness_scores={
        "RSI_THRESHOLD": 72,
        "STOPLOSS_PCT": 38,
        "overall": 55,
    },
    robustness_bool={
        "RSI_THRESHOLD_robust": True,
        "STOPLOSS_PCT_robust": False,
    },
    optimal_region={...},
    recommendations=[
        "✓ RSI is robust (score 72)",
        "✗ StopLoss is FRAGILE (score 38)",
        ...
    ],
)
```

---

## Interpretation Guide

### Robustness Scores (0–100)

| Score | Interpretation | Action |
|-------|----------------|--------|
| 80–100 | Very robust | Can vary ±20% without issues |
| 60–80 | Robust | Can vary ±10% safely |
| 40–60 | Moderately sensitive | Keep within ±5% |
| < 40 | Fragile | Must be precisely set; consider dynamic tuning (ATR/ADR) |

### Worst-Case Drawdown

- **MC Output:** 95th percentile drawdown across all scenarios
- **Interpretation:** "95% chance DD won't exceed this value"
- **Action:** Set risk management stop at this level + buffer

### Probability of Profit

- **Definition:** % of scenarios with positive return
- **Threshold:** ≥ 65% = acceptable, < 50% = not tradeable
- **Action:** If < 65%, revisit strategy logic

---

## Testing & Validation

### Unit Tests (Ready to add)

```python
def test_monte_carlo_shuffle():
    trades = [
        BacktestTrade(r_multiple=1.0, ...),
        BacktestTrade(r_multiple=-1.0, ...),
    ]
    result = monte_carlo_shuffle(trades, n_simulations=10, seed=42)
    assert result["prob_profit"] >= 0.0
    assert result["worst_case_drawdown_pct"] <= 0.0

def test_sensitivity_grid_search():
    def mock_backtest(p1, p2):
        return BacktestMetrics(
            win_rate=0.5 + p1 * 0.01,  # p1 affects WR
            ...
        )
    
    result = grid_search_2d(
        "P1", [0.0, 0.5, 1.0],
        "P2", [0.0, 0.5, 1.0],
        mock_backtest
    )
    assert result.robustness_scores["P1"] > 0
    assert len(result.recommendations) > 0
```

---

## Limitations & Caveats

### Monte Carlo Shuffle

**Assumption:** Trades are independent (not true in reality)
- Strategy may change behavior after losses
- Correlation between trades ignored

**Mitigation:** Use Resample method for more realistic scenarios

### Parameter Sensitivity

**Assumption:** Linear sensitivity (not always true)
- Some parameters have non-linear effects
- Interaction effects between parameters ignored

**Mitigation:** Use finer grid around optimal region; test 3D grids

**Overfitting Risk:**
- Parameters optimal in 2024 may not work in 2025
- Regime changes cause performance shifts

**Mitigation:** Backtest across multiple market regimes (bull, bear, range)

---

## References

- **Monte Carlo:** Efron (Bootstrap), Markowitz (Modern Portfolio Theory)
- **Sensitivity:** Sobol indices (Global Sensitivity Analysis)
- **Trading:** Peng (Walk-Forward Optimization), Sharpe (Asset Allocation)

---

## Future Enhancements

1. **3D Grid Search:** Add third parameter dimension
2. **Parallel Execution:** Speed up grid search with multiprocessing
3. **Adaptive Grid:** Dynamic refinement around optimal regions
4. **Stress Testing:** Add market crash scenarios to MC
5. **Portfolio Sensitivity:** Optimize multiple instruments simultaneously

---

**Module Location:** `/Users/simonzernahle/trading-v2/backend/src/backtest/`
**Vault Spec:** `~/Desktop/tradingbot/Trading/Bot/Monte-Carlo-Backtesting.md`
