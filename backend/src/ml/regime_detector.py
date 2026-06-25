"""ML Regime Detection — Identify TRENDING, RANGING, CHOPPY regimes.

Spec: Trading/Bot/ML-Regime-Detection.md

Regimes:
  - TRENDING (ADX > 25): Clear direction, favor breakouts
  - RANGING (ADX < 20): Oscillating, favor mean-reversions
  - CHOPPY (Vol > 2× normal): Extreme volatility, reduce size

Model: Decision Tree (scikit-learn), trained on labeled historical bars
Features: ADX, ATR, RSI, EMA-spread, Correlation, Volume
Output: regime_type (str) + confidence (0..1) + signal_filter (dict)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

try:
    from sklearn.preprocessing import StandardScaler
    from sklearn.tree import DecisionTreeClassifier
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    log = logging.getLogger(__name__)
    log.warning("scikit-learn not available; regime detection disabled")

log = logging.getLogger(__name__)

RegimeType = Literal["TRENDING", "RANGING", "CHOPPY"]


@dataclass(slots=True)
class RegimeDetection:
    """Result of regime detection for current market state."""
    regime_type: RegimeType
    confidence: float  # 0..1
    features: dict[str, float]

    def signal_filter(self) -> dict[str, Any]:
        """
        Return signal-filter recommendations based on regime.

        Returns:
            {
                "accept_breakout": bool,
                "accept_reversal": bool,
                "position_size_multiplier": float,
                "sl_multiplier": float,
                "interpretation": str,
            }
        """
        if self.regime_type == "TRENDING":
            return {
                "accept_breakout": True,
                "accept_reversal": False,
                "position_size_multiplier": 1.0,
                "sl_multiplier": 1.0,
                "interpretation": "Strong trend detected. Favor breakout signals.",
            }
        elif self.regime_type == "RANGING":
            return {
                "accept_breakout": False,
                "accept_reversal": True,
                "position_size_multiplier": 1.2,
                "sl_multiplier": 1.0,
                "interpretation": "Range-bound market. Favor mean-reversion signals.",
            }
        elif self.regime_type == "CHOPPY":
            return {
                "accept_breakout": False,
                "accept_reversal": False,
                "position_size_multiplier": 0.5,
                "sl_multiplier": 0.75,
                "interpretation": "High volatility detected. Reduce size, tighten SL.",
            }
        else:
            return {
                "accept_breakout": True,
                "accept_reversal": True,
                "position_size_multiplier": 1.0,
                "sl_multiplier": 1.0,
                "interpretation": "Unknown regime; use defaults.",
            }


def extract_regime_features(
    bars_dict: dict[str, pd.DataFrame],
    ts: pd.Timestamp,
    sp500_bars: pd.DataFrame | None = None,
) -> dict[str, float]:
    """
    Extract regime-detection features from bars at given timestamp.

    Args:
        bars_dict: {"1h": df_1h, "4h": df_4h, ...}
        ts: Current timestamp
        sp500_bars: Optional SP500 bars for correlation

    Returns:
        {
            "atr": 0.012,
            "atr_percentile": 0.85,
            "adx": 28.5,
            "ema_spread": 0.0085,
            "rsi": 65.3,
            "price_bb_position": 0.72,
            "correlation_sp500_4h": 0.85,
            "volume_ratio": 1.2,
        }
    """
    features: dict[str, float] = {}

    # Extract 1h bars (primary)
    if "1h" not in bars_dict or bars_dict["1h"].empty:
        log.warning("1h bars not available for regime detection")
        return {}

    bars_1h = bars_dict["1h"]
    idx = bars_1h.index.searchsorted(ts, side="right")

    if idx < 1:
        log.warning(f"Insufficient 1h bars at {ts}")
        return {}

    current_bar = bars_1h.iloc[idx - 1]
    historical_bars_1h = bars_1h.iloc[:idx]

    close = float(current_bar["close"])
    high = float(current_bar["high"])
    low = float(current_bar["low"])
    volume = float(current_bar.get("volume", 0.0))

    # 1. ATR (14-period)
    atr_val = _calculate_atr(historical_bars_1h, period=14)
    if atr_val > 0:
        features["atr"] = atr_val / close
    else:
        features["atr"] = 0.0

    # ATR percentile (vs. last 100 bars)
    if len(historical_bars_1h) > 100:
        atrs_100 = [_calculate_atr(historical_bars_1h.iloc[:j], period=14)
                    for j in range(max(1, len(historical_bars_1h) - 100), len(historical_bars_1h))]
        if atrs_100:
            atr_percentile = (sum(1 for a in atrs_100 if a <= atr_val) / len(atrs_100))
            features["atr_percentile"] = atr_percentile
        else:
            features["atr_percentile"] = 0.5
    else:
        features["atr_percentile"] = 0.5

    # 2. ADX (14-period)
    adx_val = _calculate_adx(historical_bars_1h, period=14)
    features["adx"] = max(0.0, min(100.0, adx_val))

    # 3. EMA Spread
    ema_12 = _calculate_ema(historical_bars_1h["close"], period=12)
    ema_26 = _calculate_ema(historical_bars_1h["close"], period=26)
    if ema_26 > 0:
        features["ema_spread"] = abs(ema_12 - ema_26) / ema_26
    else:
        features["ema_spread"] = 0.0

    # 4. RSI (14-period)
    rsi_val = _calculate_rsi(historical_bars_1h["close"], period=14)
    features["rsi"] = max(0.0, min(100.0, rsi_val))

    # 5. Bollinger Band Position
    sma = _calculate_sma(historical_bars_1h["close"], period=20)
    std = historical_bars_1h["close"].tail(20).std()
    bb_upper = sma + 2 * std
    bb_lower = sma - 2 * std
    if bb_upper > bb_lower:
        bb_position = (close - bb_lower) / (bb_upper - bb_lower)
        features["price_bb_position"] = max(0.0, min(1.0, bb_position))
    else:
        features["price_bb_position"] = 0.5

    # 6. SP500 Correlation (if available)
    if sp500_bars is not None and not sp500_bars.empty:
        idx_sp = sp500_bars.index.searchsorted(ts, side="right")
        if idx_sp >= 14:  # Need 14 bars for correlation
            sp500_returns = sp500_bars["close"].iloc[:idx_sp].pct_change().tail(14)
            current_returns = historical_bars_1h["close"].pct_change().tail(14)
            if len(sp500_returns) == len(current_returns) and len(sp500_returns) > 1:
                corr = sp500_returns.corr(current_returns)
                features["correlation_sp500_4h"] = max(-1.0, min(1.0, corr))
            else:
                features["correlation_sp500_4h"] = 0.0
        else:
            features["correlation_sp500_4h"] = 0.0
    else:
        features["correlation_sp500_4h"] = 0.0

    # 7. Volume Ratio
    if len(historical_bars_1h) > 20:
        avg_volume_20 = historical_bars_1h["volume"].tail(20).mean()
        if avg_volume_20 > 0:
            features["volume_ratio"] = volume / avg_volume_20
        else:
            features["volume_ratio"] = 1.0
    else:
        features["volume_ratio"] = 1.0

    return features


def predict_regime(
    bars_dict: dict[str, pd.DataFrame],
    ts: pd.Timestamp,
    model: DecisionTreeClassifier | None = None,
    scaler: StandardScaler | None = None,
    sp500_bars: pd.DataFrame | None = None,
) -> RegimeDetection:
    """
    Predict current market regime using trained model (or rule-based fallback).

    Args:
        bars_dict: Historical OHLCV bars
        ts: Current timestamp
        model: Trained DecisionTreeClassifier
        scaler: Fitted StandardScaler
        sp500_bars: Optional SP500 data for correlation

    Returns:
        RegimeDetection with regime_type, confidence, features
    """
    features = extract_regime_features(bars_dict, ts, sp500_bars)

    if not features:
        # Fallback: Default to TRENDING
        return RegimeDetection(
            regime_type="TRENDING",
            confidence=0.5,
            features={}
        )

    # If model not available, use rule-based fallback
    if model is None or scaler is None or not SKLEARN_AVAILABLE:
        return _predict_regime_rulebased(features)

    # ML Prediction
    try:
        feature_names = ["atr", "atr_percentile", "adx", "ema_spread", "rsi",
                        "price_bb_position", "correlation_sp500_4h", "volume_ratio"]
        feature_vector = np.array([features.get(name, 0.0) for name in feature_names])
        feature_vector_scaled = scaler.transform([feature_vector])[0]

        # Predict
        prediction = model.predict([feature_vector_scaled])[0]
        probas = model.predict_proba([feature_vector_scaled])[0]

        # Confidence = max probability
        confidence = float(max(probas))

        return RegimeDetection(
            regime_type=prediction,
            confidence=confidence,
            features=features,
        )
    except Exception as e:
        log.warning(f"ML prediction failed: {e}; falling back to rule-based")
        return _predict_regime_rulebased(features)


def _predict_regime_rulebased(features: dict[str, float]) -> RegimeDetection:
    """Fallback rule-based regime detection."""
    adx = features.get("adx", 20.0)
    atr_pct = features.get("atr", 0.012)
    atr_pctl = features.get("atr_percentile", 0.5)

    # Rules
    if atr_pct > 0.025 or atr_pctl > 0.8:  # Extreme volatility
        regime = "CHOPPY"
        conf = 0.75
    elif adx > 25:
        regime = "TRENDING"
        conf = 0.85
    elif adx < 20:
        regime = "RANGING"
        conf = 0.80
    else:  # 20 <= ADX <= 25
        regime = "TRENDING"
        conf = 0.60

    return RegimeDetection(
        regime_type=regime,
        confidence=conf,
        features=features,
    )


def train_regime_model(
    historical_bars: dict[str, pd.DataFrame],
    labeled_data: list[dict],
    sp500_bars: pd.DataFrame | None = None,
) -> tuple[DecisionTreeClassifier, StandardScaler]:
    """
    Train Decision Tree regime classifier.

    Args:
        historical_bars: {"1h": df, "4h": df, ...}
        labeled_data: [{
            "ts": pd.Timestamp,
            "label": "TRENDING",  # Ground truth
        }, ...]
        sp500_bars: Optional SP500 for correlation features

    Returns:
        (trained_model, fitted_scaler)
    """
    if not SKLEARN_AVAILABLE:
        raise ImportError("scikit-learn required for regime training")

    # Extract features for each labeled point
    X = []
    y = []

    for point in labeled_data:
        ts = point["ts"]
        label = point["label"]

        features = extract_regime_features(historical_bars, ts, sp500_bars)
        if not features:
            continue

        # Vector in fixed order
        feature_names = ["atr", "atr_percentile", "adx", "ema_spread", "rsi",
                        "price_bb_position", "correlation_sp500_4h", "volume_ratio"]
        feature_vector = [features.get(name, 0.0) for name in feature_names]

        X.append(feature_vector)
        y.append(label)

    if not X:
        raise ValueError("No valid features extracted from labeled data")

    # Scaling
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Train Decision Tree
    model = DecisionTreeClassifier(
        max_depth=5,
        min_samples_leaf=10,
        random_state=42,
    )
    model.fit(X_scaled, y)

    log.info(f"Regime model trained on {len(X)} samples")
    log.info(f"Classes: {model.classes_}")
    log.info(f"Feature importances: {dict(zip(['atr', 'atr_pct', 'adx', 'ema', 'rsi', 'bb_pos', 'corr_sp500', 'vol'], model.feature_importances_))}")

    return model, scaler


# Helper calculations (simplified, production would use talib or similar)

def _calculate_atr(bars: pd.DataFrame, period: int = 14) -> float:
    """Calculate ATR (simple version)."""
    if len(bars) < period:
        return 0.0

    tr = []
    for i in range(1, len(bars)):
        h = float(bars.iloc[i]["high"])
        l = float(bars.iloc[i]["low"])
        c_prev = float(bars.iloc[i - 1]["close"])
        tr.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))

    if not tr:
        return 0.0

    return float(np.mean(tr[-period:]))


def _calculate_adx(bars: pd.DataFrame, period: int = 14) -> float:
    """Calculate ADX (simple approximation)."""
    if len(bars) < period:
        return 0.0

    # Simple ADX = DI+ - DI- (not exact, but good proxy)
    ups = 0
    downs = 0

    for i in range(1, len(bars)):
        c = float(bars.iloc[i]["close"])
        c_prev = float(bars.iloc[i - 1]["close"])
        if c > c_prev:
            ups += 1
        else:
            downs += 1

    # Ratio-based ADX
    if ups + downs > 0:
        di_plus = (ups / (ups + downs)) * 100
        di_minus = (downs / (ups + downs)) * 100
        adx = abs(di_plus - di_minus)
    else:
        adx = 0.0

    return float(adx)


def _calculate_ema(series: pd.Series, period: int) -> float:
    """Calculate EMA."""
    if len(series) < period:
        return float(series.iloc[-1]) if len(series) > 0 else 0.0
    return float(series.ewm(span=period, adjust=False).mean().iloc[-1])


def _calculate_sma(series: pd.Series, period: int) -> float:
    """Calculate SMA."""
    if len(series) < period:
        return float(series.mean()) if len(series) > 0 else 0.0
    return float(series.tail(period).mean())


def _calculate_rsi(series: pd.Series, period: int = 14) -> float:
    """Calculate RSI."""
    if len(series) < period:
        return 50.0

    deltas = series.diff()
    gains = deltas.clip(lower=0)
    losses = -deltas.clip(upper=0)

    avg_gain = gains.rolling(window=period).mean()
    avg_loss = losses.rolling(window=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return float(rsi.iloc[-1]) if not rsi.isna().all() else 50.0
