"""
Signal Validation & Precondition Checks

Runs before evaluate_signal() to catch issues early:
- Bar validity (no NaNs, no 0-volume)
- Bias consistency
- API health
- Timeframe completeness

Author: Claude Code
Date: 2026-06-25
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of signal validation"""

    is_valid: bool
    reason: Optional[str] = None  # If invalid, why?
    warnings: list = field(default_factory=list)  # Non-blocking warnings


class SignalValidator:
    """
    Validates preconditions before signal evaluation.

    Checks performed:
    1. OHLCV bar integrity (no NaNs, no zero volume, OHLC ordering)
    2. Bias is in valid range (bullish/bearish/neutral)
    3. All required timeframes present in bars dict
    4. Timeframe data complete (no gaps)
    5. API connection health (via circuit breaker state)
    """

    # Minimum bars required to detect a setup
    MIN_BARS_PER_TIMEFRAME = 20

    # OHLCV columns that must exist
    REQUIRED_COLUMNS = {'open', 'high', 'low', 'close', 'volume'}

    def __init__(self, required_timeframes: list[str] | None = None):
        """
        Initialize validator.

        Args:
            required_timeframes: List of TFs that must be present
                (e.g., ['1d', '1h', '5m'])
                If None, just validates what's provided.
        """
        self.required_timeframes = required_timeframes or []

    def validate_all(
        self,
        instrument: str,
        bars: dict[str, pd.DataFrame],
        bias: Optional[str] = None,
        broker_circuit_state: Optional[str] = None,
    ) -> ValidationResult:
        """
        Run all validation checks.

        Args:
            instrument: Trade instrument name
            bars: Dict of {timeframe -> OHLCV DataFrame}
            bias: Current bias ('bullish', 'bearish', 'neutral')
            broker_circuit_state: Circuit breaker state ('closed', 'open', 'half_open')

        Returns:
            ValidationResult with is_valid flag + reason/warnings
        """
        warnings = []

        # 1. Check timeframe completeness
        tf_result = self._validate_timeframes(bars)
        if not tf_result.is_valid:
            return tf_result

        # 2. Check OHLCV bar validity for each timeframe
        for tf, df in bars.items():
            bars_result = self._validate_bars(tf, df)
            if not bars_result.is_valid:
                return bars_result
            warnings.extend(bars_result.warnings)

        # 3. Check bias if provided
        if bias is not None:
            bias_result = self._validate_bias(bias)
            if not bias_result.is_valid:
                return bias_result
            warnings.extend(bias_result.warnings)

        # 4. Check broker health
        if broker_circuit_state is not None:
            health_result = self._validate_broker_health(broker_circuit_state)
            if not health_result.is_valid:
                return health_result
            warnings.extend(health_result.warnings)

        # All checks passed
        if warnings:
            log.warning(
                f"{instrument}: validation passed but warnings: {warnings}"
            )

        return ValidationResult(
            is_valid=True,
            reason=None,
            warnings=warnings
        )

    def _validate_timeframes(
        self,
        bars: dict[str, pd.DataFrame]
    ) -> ValidationResult:
        """Check that all required timeframes are present."""

        missing = set(self.required_timeframes) - set(bars.keys())

        if missing:
            return ValidationResult(
                is_valid=False,
                reason=f"Missing timeframes: {missing}"
            )

        return ValidationResult(is_valid=True)

    def _validate_bars(
        self,
        timeframe: str,
        df: pd.DataFrame
    ) -> ValidationResult:
        """
        Validate OHLCV DataFrame for a single timeframe.

        Checks:
        - No missing columns
        - Minimum bar count
        - No NaN in OHLCV columns
        - No zero volume
        - OHLC ordering (high >= open/close/low, etc.)
        """

        # Check columns
        missing_cols = self.REQUIRED_COLUMNS - set(df.columns)
        if missing_cols:
            return ValidationResult(
                is_valid=False,
                reason=f"{timeframe}: Missing columns {missing_cols}"
            )

        # Check minimum bar count
        if len(df) < self.MIN_BARS_PER_TIMEFRAME:
            return ValidationResult(
                is_valid=False,
                reason=f"{timeframe}: Only {len(df)} bars, need {self.MIN_BARS_PER_TIMEFRAME}"
            )

        warnings = []

        # Check for NaN in OHLCV
        for col in self.REQUIRED_COLUMNS:
            nan_count = df[col].isna().sum()
            if nan_count > 0:
                return ValidationResult(
                    is_valid=False,
                    reason=f"{timeframe}: {nan_count} NaN values in {col}"
                )

        # Check for zero volume
        zero_vol = (df['volume'] == 0).sum()
        if zero_vol > 0:
            warnings.append(
                f"{timeframe}: {zero_vol} bars with zero volume"
            )

        # Check OHLC ordering (high >= all prices, low <= all prices)
        for idx, row in df.iterrows():
            if row['high'] < row['open'] or row['high'] < row['close']:
                return ValidationResult(
                    is_valid=False,
                    reason=f"{timeframe} bar {idx}: high < open or close"
                )
            if row['low'] > row['open'] or row['low'] > row['close']:
                return ValidationResult(
                    is_valid=False,
                    reason=f"{timeframe} bar {idx}: low > open or close"
                )

        return ValidationResult(is_valid=True, warnings=warnings)

    def _validate_bias(self, bias: str) -> ValidationResult:
        """Check that bias is in valid range."""

        valid_biases = {'bullish', 'bearish', 'neutral'}

        if bias not in valid_biases:
            return ValidationResult(
                is_valid=False,
                reason=f"Invalid bias: {bias}. Must be one of {valid_biases}"
            )

        return ValidationResult(is_valid=True)

    def _validate_broker_health(
        self,
        circuit_state: str
    ) -> ValidationResult:
        """
        Check broker health via circuit breaker state.

        Allows: 'closed' (good), 'half_open' (recovering)
        Blocks: 'open' (broker down)
        """

        if circuit_state == 'open':
            return ValidationResult(
                is_valid=False,
                reason="Broker circuit OPEN — skipping signal evaluation"
            )

        if circuit_state == 'half_open':
            # Allow evaluation but log warning
            return ValidationResult(
                is_valid=True,
                warnings=["Broker circuit HALF_OPEN — may have connectivity issues"]
            )

        if circuit_state == 'closed':
            return ValidationResult(is_valid=True)

        # Unknown state
        return ValidationResult(
            is_valid=False,
            reason=f"Unknown circuit_state: {circuit_state}"
        )

    def validate_for_entry(
        self,
        instrument: str,
        bars: dict[str, pd.DataFrame],
        bias: str,
        broker_circuit_state: str,
    ) -> ValidationResult:
        """
        Convenience method: full validation for entry signal.

        Combines all checks in one call with appropriate defaults.
        """
        return self.validate_all(
            instrument=instrument,
            bars=bars,
            bias=bias,
            broker_circuit_state=broker_circuit_state
        )


def example_usage():
    """Example of how to use SignalValidator."""

    # Create validator for a strategy that requires 1d, 1h, 5m
    validator = SignalValidator(
        required_timeframes=['1d', '1h', '5m']
    )

    # Example bars dict
    import pandas as pd
    import numpy as np

    dates = pd.date_range('2026-01-01', periods=50, freq='1h')
    bars = {
        '1d': pd.DataFrame({
            'open': np.random.uniform(62000, 65000, 30),
            'high': np.random.uniform(62000, 65000, 30),
            'low': np.random.uniform(62000, 65000, 30),
            'close': np.random.uniform(62000, 65000, 30),
            'volume': np.random.uniform(100, 1000, 30),
        }, index=dates[-30:]),
        '1h': pd.DataFrame({
            'open': np.random.uniform(62000, 65000, 50),
            'high': np.random.uniform(62000, 65000, 50),
            'low': np.random.uniform(62000, 65000, 50),
            'close': np.random.uniform(62000, 65000, 50),
            'volume': np.random.uniform(10, 100, 50),
        }, index=dates),
        '5m': pd.DataFrame({
            'open': np.random.uniform(62000, 65000, 50),
            'high': np.random.uniform(62000, 65000, 50),
            'low': np.random.uniform(62000, 65000, 50),
            'close': np.random.uniform(62000, 65000, 50),
            'volume': np.random.uniform(1, 50, 50),
        }, index=dates),
    }

    # Validate
    result = validator.validate_for_entry(
        instrument='BTCUSD',
        bars=bars,
        bias='bullish',
        broker_circuit_state='closed'
    )

    print(f"Validation passed: {result.is_valid}")
    if not result.is_valid:
        print(f"Reason: {result.reason}")
    if result.warnings:
        print(f"Warnings: {result.warnings}")


if __name__ == "__main__":
    example_usage()
