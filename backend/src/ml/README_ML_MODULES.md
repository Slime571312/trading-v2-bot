# ML Modules — Regime Detection & Exit Optimization

## Overview

Two machine learning modules for intelligent trading decisions:

1. **Regime Detector** (`regime_detector.py`)
   - Classify market regime: TRENDING, RANGING, CHOPPY
   - Adapt signal filters & position sizing dynamically
   - Features: ADX, ATR, RSI, EMA-spread, Correlation, Volume

2. **Exit Optimizer** (`exit_optimizer.py`)
   - Recommend optimal exit action: EXIT_EARLY, EXIT_NORMAL, EXIT_TRAIL
   - Context-aware decisions based on position state + market
   - Features: Time in trade, P&L, RSI-slope, structure, volatility

---

## Quick Start

### Regime Detection (Rule-Based Fallback)

```python
from src.ml.regime_detector import predict_regime

# No model? Use rule-based prediction
regime = predict_regime(
    bars_dict={"1h": df_1h, "4h": df_4h},
    ts=pd.Timestamp("2026-06-25 14:30:00"),
    model=None,  # Falls back to rules
    scaler=None,
)

print(f"Regime: {regime.regime_type}")  # "TRENDING"
print(f"Confidence: {regime.confidence:.0%}")  # 85%

# Get signal filter recommendations
filters = regime.signal_filter()
print(f"Accept breakout? {filters['accept_breakout']}")  # True
print(f"Size multiplier: {filters['position_size_multiplier']}")  # 1.0
print(f"Interpretation: {filters['interpretation']}")
# "Strong uptrend detected. Favor breakout signals."
```

### Exit Optimization (Rule-Based Fallback)

```python
from src.ml.exit_optimizer import extract_position_features, predict_exit_action

# Get position features
features = extract_position_features(
    time_in_trade_minutes=15,
    pnl_pct=1.5,
    pnl_r=1.2,
    current_atr_pct=0.012,
    rsi=68.0,
    distance_to_resistance=0.3,
    adx=35.0,
)

# Predict exit action
action, confidence, importances = predict_exit_action(
    features=features,
    model=None,  # Falls back to rules
    scaler=None,
)

print(f"Action: {action}")  # "EXIT_TRAIL"
print(f"Confidence: {confidence:.0%}")  # 75%
print(f"Reason: Strong trend + P&L +1.2R + RSI extreme (68). Enable trailing stop.")

# Get execution command
cmd = ExitRecommendation(action, confidence, features, importances, "...").execute_command()
print(cmd)
# {"action": "trail", "trailing_stop_distance_r": 0.5, "reasoning": "..."}
```

---

## ML Training & Deployment

### Train Regime Model

```python
from src.ml.regime_detector import train_regime_model

# Collect labeled data (200+ bars with manual labels)
labeled_data = [
    {"ts": pd.Timestamp("2024-01-01 10:00"), "label": "TRENDING"},
    {"ts": pd.Timestamp("2024-01-01 11:00"), "label": "RANGING"},
    {"ts": pd.Timestamp("2024-01-01 12:00"), "label": "CHOPPY"},
    # ... 200 more
]

# Train
model, scaler = train_regime_model(
    historical_bars={"1h": df_1h, "4h": df_4h},
    labeled_data=labeled_data,
)

# Save for later use
import pickle
pickle.dump((model, scaler), open("regime_model.pkl", "wb"))
```

### Use Trained Model

```python
import pickle
from src.ml.regime_detector import predict_regime

# Load model
model, scaler = pickle.load(open("regime_model.pkl", "rb"))

# Predict on live bars
regime = predict_regime(
    bars_dict={"1h": df_1h_live, "4h": df_4h_live},
    ts=pd.Timestamp.now(),
    model=model,
    scaler=scaler,
)

print(f"Regime: {regime.regime_type} ({regime.confidence:.0%})")
```

### Train Exit Optimizer Model

```python
from src.ml.exit_optimizer import train_exit_model

# Collect labeled positions (500+ historical exits)
historical_positions = [
    {
        "features": {
            "time_in_trade_minutes": 15,
            "pnl_r": 1.2,
            "rsi": 68.0,
            "distance_to_resistance": 0.3,
            # ... other features
        },
        "label": "EXIT_TRAIL",  # Ground truth
    },
    # ... 500 more
]

# Train
model, scaler = train_exit_model(historical_positions)

# Save
pickle.dump((model, scaler), open("exit_model.pkl", "wb"))
```

---

## Integration with Live Loop

### In `~/trading-v2/backend/src/live/loop.py`

```python
from src.ml.regime_detector import predict_regime
from src.ml.exit_optimizer import predict_exit_action, format_recommendation
import pickle

# Load models (on startup)
regime_model, regime_scaler = pickle.load(open("regime_model.pkl", "rb"))
exit_model, exit_scaler = pickle.load(open("exit_model.pkl", "rb"))

def main_iteration():
    # 1. Determine market regime
    regime = predict_regime(
        bars_dict=bars_dict,
        ts=now_ts,
        model=regime_model,
        scaler=regime_scaler,
    )
    
    # 2. Evaluate signals with regime filter
    signals = evaluate(bars_dict, now_ts, ...)
    
    # 3. Filter signals based on regime
    filtered_signals = []
    filters = regime.signal_filter()
    for signal in signals:
        if signal.variant == "breakout" and not filters["accept_breakout"]:
            continue
        if signal.variant == "reversal" and not filters["accept_reversal"]:
            continue
        
        # Adjust size & SL
        signal.size *= filters["position_size_multiplier"]
        signal.sl = signal.entry - (abs(signal.entry - signal.sl) * filters["sl_multiplier"])
        
        filtered_signals.append(signal)
    
    # 4. Check open positions for ML-exit recommendations
    for trade in open_positions:
        features = extract_position_features(
            time_in_trade_minutes=trade.time_in_trade_minutes,
            pnl_r=trade.pnl_r,
            # ... other features
        )
        
        action, confidence, importances = predict_exit_action(
            features=features,
            model=exit_model,
            scaler=exit_scaler,
        )
        
        # Execute if high confidence
        if confidence > 0.75:
            rec = format_recommendation(action, confidence, features, importances)
            
            if action == "EXIT_EARLY":
                close_position(trade, reason=f"ML EXIT_EARLY ({confidence:.0%})")
            elif action == "EXIT_TRAIL":
                trade.trailing_stop_enabled = True
                trade.trailing_stop_distance = trade.risk_amount * 0.5
            
            # Log for analysis
            log_ml_decision({
                "trade_id": trade.id,
                "regime": regime.regime_type,
                "ml_action": action,
                "ml_confidence": confidence,
                "features": features,
            })
```

---

## API Endpoints (Proposed)

### GET /bot/<inst>/regime

Current market regime + signal filters

**Response:**
```json
{
  "regime": "TRENDING",
  "confidence": 0.92,
  "features": {
    "atr": 0.012,
    "adx": 32.5,
    "ema_spread": 0.0085,
    "rsi": 68.0,
    "volume_ratio": 1.2
  },
  "signal_filter": {
    "accept_breakout": true,
    "accept_reversal": false,
    "position_size_multiplier": 1.0,
    "sl_multiplier": 1.0
  },
  "interpretation": "Strong uptrend with clear direction. Favor breakout signals, ignore reversals."
}
```

### GET /bot/<inst>/positions/<trade_id>/ml-exit-recommendation

ML-recommended exit action for open position

**Response:**
```json
{
  "trade_id": "BTC_1234",
  "open_time": "2026-06-25T14:30:00Z",
  "current_pnl_r": 1.2,
  "ml_recommendation": {
    "action": "EXIT_EARLY",
    "confidence": 0.82,
    "reasoning": "Momentum collapse (RSI↓ 72→62), distance to support narrowing (0.3R). Safe +0.2R vs. risk of drawdown.",
    "feature_importance": {
      "time_in_trade_minutes": 0.45,
      "distance_to_resistance": 0.25,
      "rsi_slope": 0.15,
      "atr_vs_entry": 0.10,
      "current_atr_pct": 0.05
    }
  }
}
```

---

## Regime Classification

### TRENDING (ADX > 25)

**Characteristics:**
- Strong directional bias (up or down)
- Low mean-reversion (price extends)
- Trending indicators strong (EMA-spread large)
- RSI often extreme (< 20 or > 80)

**Signal Filter:**
- ✓ Accept breakout signals (BOS, HL/LL, FVG-fills)
- ✗ Reject reversal signals (rejections, pins)
- Position sizing: 1.0× (normal)
- SL multiplier: 1.0× (no change)

**Example:** BTC breaking above 43,200 with ADX=32, EMA-12 > EMA-26

### RANGING (ADX < 20)

**Characteristics:**
- Oscillating price (no clear direction)
- Strong mean-reversion
- RSI cycles 40–60
- Bollinger Bands tight

**Signal Filter:**
- ✗ Reject breakout signals (false breaks)
- ✓ Accept reversal signals (RSI extremes, rejection wicks)
- Position sizing: 1.2× (higher, more entries)
- SL multiplier: 1.0× (no change)

**Example:** BTC bouncing 42,000–42,900 range, ADX=12

### CHOPPY (Vol > 2× normal)

**Characteristics:**
- Extreme volatility (ATR ≥ 2× median)
- Large wicks, quick reversals
- Often post-news (CPI, FOMC, Earnings)
- Unclear direction

**Signal Filter:**
- ✗ Reject both breakout & reversal (too risky)
- Position sizing: 0.5× (reduced, defensive)
- SL multiplier: 0.75× (tighter, less room)

**Example:** BTC ±3% swings intrabar post-CPI

---

## Exit Actions

### EXIT_EARLY

**When:** Momentum lost, drawdown risk increasing
- P&L < +0.5R + time > 40min (stuck trade)
- P&L > +0.5R but resistance very close (< 0.4R)
- RSI slope negative (was 72, now 60) + no progress

**Action:** Close entire position, save remaining equity

**Expected Gain:** +0.2–0.5R vs. potential –0.5R if reversal

### EXIT_NORMAL

**When:** Normal trade progression, no urgency
- 0.5R < P&L < 1.5R
- Time in trade 15–60 min
- Trend intact, no momentum loss

**Action:** Hold to TP1/TP2 (rule-based partial closes)

**Expected Gain:** Standard TP ladder

### EXIT_TRAIL

**When:** Strong profit + trend very strong
- P&L ≥ +1.5R
- ADX > 30 (strong trend)
- Volatility shrinking (ATR < entry ATR)
- RSI extreme but not reversal

**Action:** Activate trailing stop, let profit run

**Expected Gain:** +0.5–2.0R extra (capture extended moves)

---

## Training Data Collection

### For Regime Detection

Collect 200+ labeled bars:

```python
labeled_data = [
    {
        "ts": pd.Timestamp("2024-03-15 10:00"),
        "label": "TRENDING",  # Manual assessment
    },
    # Criteria:
    # - TRENDING: ADX > 25, clear direction, EMA-spread > 0.5%
    # - RANGING: ADX < 20, price oscillating, BB tight
    # - CHOPPY: ATR > 2× normal, large wicks, news event
]
```

### For Exit Optimization

Collect 500+ historical position exits:

```python
historical_positions = [
    {
        "features": {
            "time_in_trade_minutes": 15,
            "pnl_r": 1.2,
            "rsi": 68.0,
            "distance_to_resistance": 0.3,
            "distance_to_support": 2.5,
            "adx": 35.0,
            "atr_vs_entry": 0.8,
            "correlation_market": 0.85,
            "session_time_pct": 0.7,
            "current_atr_pct": 0.010,
        },
        "label": "EXIT_EARLY",  # Actual exit decision
    },
]
```

---

## Testing & Validation

### Unit Tests

```python
def test_regime_features():
    features = extract_regime_features(bars_dict, ts)
    assert "atr" in features
    assert 0 <= features["rsi"] <= 100
    assert -1 <= features["correlation_sp500_4h"] <= 1

def test_regime_prediction_rulebased():
    regime = predict_regime(bars_dict, ts, model=None)
    assert regime.regime_type in ["TRENDING", "RANGING", "CHOPPY"]
    assert 0.5 <= regime.confidence <= 1.0

def test_exit_features():
    features = extract_position_features(
        time_in_trade_minutes=15,
        pnl_r=1.2,
        # ...
    )
    assert features["time_in_trade_minutes"] == 15
    assert features["pnl_r"] == 1.2
```

### Integration Tests

```python
def test_regime_on_live_bars():
    # Get real 1h bars
    bars = fetch_bars("BTC/USDT", "1h", lookback=100)
    regime = predict_regime({"1h": bars}, ts=bars.index[-1])
    
    # Should have consistent output
    assert regime.regime_type is not None
    assert regime.confidence > 0.5

def test_exit_recommendation_format():
    recommendation = format_recommendation(
        "EXIT_EARLY", 0.82, features, importances
    )
    
    assert recommendation.action == "EXIT_EARLY"
    assert 0 <= recommendation.confidence <= 1.0
    cmd = recommendation.execute_command()
    assert cmd["action"] in ["close", "trail", "hold"]
```

---

## Performance Expectations

### Regime Detection

- **Model Accuracy:** 80–85% (3-class classification)
- **Feature Importance:** ADX (40%), ATR (25%), EMA (15%), RSI (10%), others (10%)
- **Win-Rate Improvement:** +5–10% via better signal filtering

### Exit Optimization

- **Model Accuracy:** 75–80% (3-class classification)
- **Top Features:** Time in trade (45%), distance to resistance (25%), RSI-slope (15%)
- **Win-Rate Improvement:** +3–5% via early exits + extended profits

---

## Limitations

### Regime Detection

- **Assumption:** Market has stable regimes (not always true)
- **Drift:** Regime definitions change over time (need retraining every 3 months)
- **Simplification:** 3 classes may be too few (could add 5–7 regime types)

### Exit Optimization

- **Distribution Shift:** Position-exit distributions in 2024 ≠ 2026
- **Lookahead Bias:** Must never use future price in features
- **False Positives:** Model might suggest EXIT_EARLY too often

**Mitigation:**
1. Continuous retraining with fresh data
2. Confidence threshold: only auto-exit if > 75%
3. A/B testing: compare ML vs. rule-based exits

---

## References

- Technical Analysis: ADX (Wilder), Bollinger Bands (Bollinger)
- Machine Learning: Decision Trees, Scikit-Learn
- Trading: Context-aware execution (Lopez de Prado)

---

**Module Location:** `/Users/simonzernahle/trading-v2/backend/src/ml/`
**Vault Specs:**
- `~/Desktop/tradingbot/Trading/Bot/ML-Regime-Detection.md`
- `~/Desktop/tradingbot/Trading/Bot/ML-Exit-Optimization.md`
