"""
Dynamic Slippage Calibration Module

Analyzes recent fills to calibrate slippage assumptions dynamically.
Used by paper_broker to simulate more realistic fill prices.

Author: Claude Code
Date: 2026-06-25
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class SlippageCalibration:
    """Result of slippage calibration"""

    slippage_pct: float  # Estimated slippage as percentage (0.01 = 0.01%)
    sample_size: int  # Number of recent trades used
    percentile_95: float  # 95th percentile slip
    mean_slip: float  # Mean slip
    last_updated: datetime


class SlippageCalibrator:
    """
    Dynamically calibrates slippage based on recent trade fills.

    Logic:
    - Load recent N trades (default 100)
    - Calculate actual_fill - planned_price for each
    - Take 95th percentile of slippage
    - Add 10% buffer
    - Clamp to [0.05%, 0.50%]

    Usage:
        calibrator = SlippageCalibrator(db_client, lookback_trades=100)
        calib = calibrator.calibrate()
        print(f"Recommended slippage: {calib.slippage_pct}%")
    """

    MIN_SLIPPAGE_PCT = 0.05  # Don't assume less than 0.05% slip
    MAX_SLIPPAGE_PCT = 0.50  # Cap at 0.50% even if empirical says higher
    BUFFER_MULTIPLIER = 1.10  # Add 10% to empirical percentile
    MIN_SAMPLE_SIZE = 20  # Need at least 20 trades to calibrate

    def __init__(self, db_client, lookback_trades: int = 100):
        """
        Initialize calibrator.

        Args:
            db_client: Database client with access to closed_trades
            lookback_trades: Number of recent trades to analyze (default 100)
        """
        self.db_client = db_client
        self.lookback_trades = lookback_trades
        self.last_calibration: Optional[SlippageCalibration] = None

    def calibrate(self) -> SlippageCalibration:
        """
        Perform slippage calibration.

        Returns:
            SlippageCalibration object with recommended slippage_pct

        Raises:
            ValueError: If fewer than MIN_SAMPLE_SIZE trades available
        """
        # Fetch recent closed trades
        trades = self._fetch_recent_trades()

        if len(trades) < self.MIN_SAMPLE_SIZE:
            logger.warning(
                f"Only {len(trades)} trades available, need {self.MIN_SAMPLE_SIZE}. "
                f"Using default slippage assumption."
            )
            # Fallback to conservative default
            return SlippageCalibration(
                slippage_pct=0.15,  # Conservative middle ground
                sample_size=len(trades),
                percentile_95=0.15,
                mean_slip=0.10,
                last_updated=datetime.utcnow(),
            )

        # Calculate slippage for each trade
        slips = self._calculate_slips(trades)

        # Compute statistics
        mean_slip = float(np.mean(slips))
        percentile_95 = float(np.percentile(slips, 95))

        # Apply buffer and clamp
        calibrated = percentile_95 * self.BUFFER_MULTIPLIER
        calibrated = np.clip(calibrated, self.MIN_SLIPPAGE_PCT, self.MAX_SLIPPAGE_PCT)

        result = SlippageCalibration(
            slippage_pct=float(calibrated),
            sample_size=len(trades),
            percentile_95=percentile_95,
            mean_slip=mean_slip,
            last_updated=datetime.utcnow(),
        )

        self.last_calibration = result

        logger.info(
            f"Slippage calibration complete: "
            f"slippage_pct={result.slippage_pct:.3f}%, "
            f"95th_percentile={result.percentile_95:.4f}%, "
            f"mean={result.mean_slip:.4f}%, "
            f"sample_size={result.sample_size}"
        )

        return result

    def _fetch_recent_trades(self) -> list:
        """
        Fetch most recent N closed trades from database.

        Returns:
            List of trade dicts with 'entry_price', 'fill_price', 'direction'
        """
        # Pseudo-code; replace with actual DB query
        # Expected schema: trades.entry_price, trades.fill_price, trades.status='closed'
        trades = self.db_client.query_trades(
            status='closed',
            limit=self.lookback_trades,
            order_by='close_time DESC'
        )
        return trades

    def _calculate_slips(self, trades: list) -> np.ndarray:
        """
        Calculate slippage for each trade.

        Slippage = |actual_fill - planned_price| / planned_price * 100

        Args:
            trades: List of trade dicts

        Returns:
            numpy array of slippage percentages
        """
        slips = []

        for trade in trades:
            planned_price = trade.get('entry_price')
            fill_price = trade.get('fill_price')

            if planned_price is None or fill_price is None:
                continue

            # Avoid division by zero
            if planned_price == 0:
                continue

            # Slippage in percentage points
            slip = abs(fill_price - planned_price) / planned_price * 100.0
            slips.append(slip)

        return np.array(slips)

    def should_recalibrate(self, hours_since_last: int = 6) -> bool:
        """
        Check if recalibration is needed (stale calibration).

        Args:
            hours_since_last: Recalibrate if older than this many hours

        Returns:
            True if should recalibrate
        """
        if self.last_calibration is None:
            return True

        age = datetime.utcnow() - self.last_calibration.last_updated
        return age > timedelta(hours=hours_since_last)


def example_usage():
    """Example of how to use SlippageCalibrator."""

    # Simulated DB client (in production, use actual DB)
    class MockDB:
        def query_trades(self, status, limit, order_by):
            # Return mock trades with entry/fill prices
            return [
                {
                    'entry_price': 63000.00,
                    'fill_price': 63005.50,  # Slipped 0.0087%
                    'direction': 'long'
                },
                {
                    'entry_price': 63100.00,
                    'fill_price': 63108.75,  # Slipped 0.0139%
                    'direction': 'long'
                },
                # ... more trades
            ]

    db = MockDB()
    calibrator = SlippageCalibrator(db, lookback_trades=100)

    # Perform calibration
    calib = calibrator.calibrate()
    print(f"Recommended slippage: {calib.slippage_pct:.3f}%")
    print(f"95th percentile: {calib.percentile_95:.4f}%")
    print(f"Mean slip: {calib.mean_slip:.4f}%")
    print(f"Sample size: {calib.sample_size}")

    # Use in paper_broker
    # paper_broker.slippage_pct = calib.slippage_pct


if __name__ == "__main__":
    example_usage()
