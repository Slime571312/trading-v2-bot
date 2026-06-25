"""ML Exit Optimization — Predict best exit action for open positions.

Spec: Trading/Bot/ML-Exit-Optimization.md

Exit Actions:
  - EXIT_EARLY: Close now, save remaining equity (momentum lost)
  - EXIT_NORMAL: Hold to TP1/TP2 (rule-based, standard)
  - EXIT_TRAIL: Activate trailing stop, let profit run (strong trend)

Model: XGBoost Classifier (or Decision Tree fallback)
Features: Time in trade, P&L, RSI-slope, distance to resistance, volatility
Output: exit_action (str) + confidence (0..1) + feature_importance (dict)
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
    log.warning("scikit-learn not available; exit optimization disabled")

log = logging.getLogger(__name__)

ExitAction = Literal["EXIT_EARLY", "EXIT_NORMAL", "EXIT_TRAIL"]


@dataclass(slots=True)
class ExitRecommendation:
    """Exit recommendation for open position."""
    action: ExitAction
    confidence: float  # 0..1
    features: dict[str, float]
    feature_importance: dict[str, float]
    reasoning: str

    def execute_command(self) -> dict[str, Any]:
        """
        Convert recommendation to execution command.

        Returns:
            {
                "action": "close" | "partial_close" | "trail",
                "close_pct": 1.0 | 0.5,
                "trailing_stop_distance_r": 0.5,  # optional
            }
        """
        if self.action == "EXIT_EARLY":
            return {
                "action": "close",
                "close_pct": 1.0,
                "reasoning": "ML detected momentum loss; exiting early",
            }
        elif self.action == "EXIT_TRAIL":
            return {
                "action": "trail",
                "trailing_stop_distance_r": 0.5,
                "reasoning": "ML detected strong trend; enabling trailing stop",
            }
        else:  # EXIT_NORMAL
            return {
                "action": "hold",
                "reasoning": "ML recommends rule-based TP/SL execution",
            }


def extract_position_features(
    time_in_trade_minutes: int,
    pnl_pct: float,
    pnl_r: float,
    current_atr_pct: float,
    entry_atr_pct: float | None = None,
    rsi: float | None = None,
    distance_to_resistance: float | None = None,
    distance_to_support: float | None = None,
    adx: float | None = None,
    session_time_pct: float | None = None,
    correlation_market: float | None = None,
) -> dict[str, float]:
    """
    Build feature vector for exit decision.

    Args:
        time_in_trade_minutes: How long position is open
        pnl_pct: P&L as percentage of equity
        pnl_r: P&L in R-multiples
        current_atr_pct: Current ATR as % of close
        entry_atr_pct: ATR at entry time
        rsi: Current RSI (0..100)
        distance_to_resistance: Next resistance in R-multiples
        distance_to_support: Next support in R-multiples
        adx: ADX (0..100)
        session_time_pct: % of trading session elapsed
        correlation_market: Correlation with broad market

    Returns:
        Feature dict for model
    """
    features = {
        "time_in_trade_minutes": float(time_in_trade_minutes),
        "pnl_pct": float(pnl_pct),
        "pnl_r": float(pnl_r),
        "current_atr_pct": float(current_atr_pct),
        "atr_vs_entry": float(entry_atr_pct / current_atr_pct) if entry_atr_pct and current_atr_pct > 0 else 1.0,
        "rsi": float(rsi) if rsi is not None else 50.0,
        "distance_to_resistance": float(distance_to_resistance) if distance_to_resistance is not None else 2.0,
        "distance_to_support": float(distance_to_support) if distance_to_support is not None else 2.0,
        "adx": float(adx) if adx is not None else 20.0,
        "session_time_pct": float(session_time_pct) if session_time_pct is not None else 0.5,
        "correlation_market": float(correlation_market) if correlation_market is not None else 0.5,
    }

    return features


def predict_exit_action(
    features: dict[str, float],
    model: DecisionTreeClassifier | None = None,
    scaler: StandardScaler | None = None,
) -> tuple[ExitAction, float, dict[str, float]]:
    """
    Predict best exit action using trained model (or rule-based fallback).

    Args:
        features: Feature dict (from extract_position_features)
        model: Trained DecisionTreeClassifier
        scaler: Fitted StandardScaler

    Returns:
        (exit_action, confidence, feature_importance)
    """
    if model is None or scaler is None or not SKLEARN_AVAILABLE:
        return _predict_exit_rulebased(features)

    # ML Prediction
    try:
        feature_names = [
            "time_in_trade_minutes", "pnl_pct", "pnl_r",
            "current_atr_pct", "atr_vs_entry", "rsi",
            "distance_to_resistance", "distance_to_support",
            "adx", "session_time_pct", "correlation_market",
        ]
        feature_vector = np.array([features.get(name, 0.0) for name in feature_names])
        feature_vector_scaled = scaler.transform([feature_vector])[0]

        # Predict
        prediction = model.predict([feature_vector_scaled])[0]
        probas = model.predict_proba([feature_vector_scaled])[0]

        # Confidence = max probability
        confidence = float(max(probas))

        # Feature importance (tree's feature_importances)
        importances = dict(zip(feature_names, model.feature_importances_))

        return prediction, confidence, importances
    except Exception as e:
        log.warning(f"ML prediction failed: {e}; falling back to rule-based")
        return _predict_exit_rulebased(features)


def _predict_exit_rulebased(features: dict[str, float]) -> tuple[ExitAction, float, dict[str, float]]:
    """Fallback rule-based exit decision."""
    time_in_trade = features.get("time_in_trade_minutes", 0)
    pnl_r = features.get("pnl_r", 0.0)
    rsi = features.get("rsi", 50.0)
    distance_to_res = features.get("distance_to_resistance", 2.0)
    atr_vs_entry = features.get("atr_vs_entry", 1.0)

    # Default feature importance (for rule-based)
    importances = {
        "time_in_trade_minutes": 0.30,
        "pnl_r": 0.20,
        "rsi": 0.15,
        "distance_to_resistance": 0.15,
        "atr_vs_entry": 0.10,
        "current_atr_pct": 0.05,
        "adx": 0.05,
    }

    # Rules
    if pnl_r < 0.5 and time_in_trade > 40:
        # Stuck trade, exit early
        action = "EXIT_EARLY"
        conf = 0.70
    elif pnl_r >= 1.5 and rsi > 65 and atr_vs_entry < 1.2:
        # Strong profit, momentum extreme, volatile shrunk → trail
        action = "EXIT_TRAIL"
        conf = 0.75
    elif pnl_r >= 1.0 and distance_to_res < 0.4:
        # Good profit but resistance close → exit early
        action = "EXIT_EARLY"
        conf = 0.65
    elif 0.5 <= pnl_r < 1.0 and 40 <= time_in_trade <= 60:
        # Progressing normally → hold
        action = "EXIT_NORMAL"
        conf = 0.80
    else:
        # Default: hold to TP
        action = "EXIT_NORMAL"
        conf = 0.60

    return action, conf, importances


def train_exit_model(
    historical_positions: list[dict],
) -> tuple[DecisionTreeClassifier, StandardScaler]:
    """
    Train Decision Tree exit optimizer.

    Args:
        historical_positions: [{
            "features": {time_in_trade: 15, pnl_r: 1.2, ...},
            "label": "EXIT_EARLY",  # Ground truth
        }, ...]

    Returns:
        (trained_model, fitted_scaler)
    """
    if not SKLEARN_AVAILABLE:
        raise ImportError("scikit-learn required for exit training")

    X = []
    y = []

    for pos in historical_positions:
        features_dict = pos.get("features", {})
        label = pos.get("label", "EXIT_NORMAL")

        # Build feature vector in fixed order
        feature_names = [
            "time_in_trade_minutes", "pnl_pct", "pnl_r",
            "current_atr_pct", "atr_vs_entry", "rsi",
            "distance_to_resistance", "distance_to_support",
            "adx", "session_time_pct", "correlation_market",
        ]
        feature_vector = [features_dict.get(name, 0.0) for name in feature_names]

        X.append(feature_vector)
        y.append(label)

    if not X:
        raise ValueError("No valid features extracted from historical positions")

    # Scaling
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Train Decision Tree
    model = DecisionTreeClassifier(
        max_depth=6,
        min_samples_leaf=5,
        random_state=42,
    )
    model.fit(X_scaled, y)

    log.info(f"Exit optimization model trained on {len(X)} positions")
    log.info(f"Classes: {model.classes_}")
    log.info(f"Feature importances: {dict(zip(feature_names, model.feature_importances_))}")

    return model, scaler


def format_recommendation(
    action: ExitAction,
    confidence: float,
    features: dict[str, float],
    importances: dict[str, float],
) -> ExitRecommendation:
    """
    Format recommendation with human-readable reasoning.

    Args:
        action: EXIT_EARLY, EXIT_NORMAL, or EXIT_TRAIL
        confidence: 0..1
        features: Feature dict
        importances: Feature importance dict

    Returns:
        ExitRecommendation
    """
    pnl_r = features.get("pnl_r", 0.0)
    time_in_trade = features.get("time_in_trade_minutes", 0)
    rsi = features.get("rsi", 50.0)
    distance_to_res = features.get("distance_to_resistance", 2.0)

    if action == "EXIT_EARLY":
        if pnl_r < 0.5 and time_in_trade > 40:
            reason = f"Stuck trade (+{pnl_r:.1f}R, {time_in_trade}min). Exit now, save equity."
        elif distance_to_res < 0.4:
            reason = f"Good profit (+{pnl_r:.1f}R) but resistance close ({distance_to_res:.1f}R). Take it."
        else:
            reason = f"Momentum loss detected. Early exit at +{pnl_r:.1f}R to preserve capital."

    elif action == "EXIT_TRAIL":
        reason = f"Strong trend + P&L +{pnl_r:.1f}R + RSI extreme ({rsi:.0f}). Enable trailing stop."

    else:  # EXIT_NORMAL
        reason = f"Normal progression (+{pnl_r:.1f}R, {time_in_trade}min). Hold to TP1/TP2."

    return ExitRecommendation(
        action=action,
        confidence=confidence,
        features=features,
        feature_importance=importances,
        reasoning=reason,
    )
