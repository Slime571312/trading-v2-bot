"""
Risk-Parity Position Sizing

Calculates position sizes so each instrument has equal risk exposure.

Logic:
  For each instrument i:
    size[i] = portfolio_risk / (atr[i] * price[i])

Example:
  Portfolio: 10,000 USD
  Risk per trade: 1% = 100 USD

  BTC/USDT: price=63,000, ATR=2,000 → size = 100/(2000) ≈ 0.05 BTC
  DE40: price=17,850, ATR=500 → size = 100/(500) ≈ 0.20 contracts
  SP500: price=5,432, ATR=100 → size = 100/(100) ≈ 1.0 shares

Result: each instrument risks ~100 USD per SL hit, regardless of volatility.

Author: Claude Code
Date: 2026-06-25
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class PositionSize:
    """Position sizing result for a single instrument"""

    instrument: str
    size: float  # Number of units (shares, contracts, coins)
    risk_usd: float  # Dollar risk at SL
    notional_usd: float  # Size * Price in USD
    atr: float  # ATR used for calculation


@dataclass
class RiskParityAllocation:
    """Complete risk-parity allocation for portfolio"""

    positions: Dict[str, PositionSize]
    portfolio_risk_usd: float
    total_notional_usd: float


class RiskParityPositionSizer:
    """
    Allocates position sizes based on risk parity.

    All instruments should have approximately equal risk exposure
    (in USD) based on their ATR and current price.

    Usage:
        sizer = RiskParityPositionSizer(portfolio_value=10000, risk_pct=1.0)
        alloc = sizer.calculate(
            instruments=['BTCUSD', 'DE40', 'SP500'],
            prices={'BTCUSD': 63000, 'DE40': 17850, 'SP500': 5432},
            atrs={'BTCUSD': 2000, 'DE40': 500, 'SP500': 100}
        )
        for inst, pos in alloc.positions.items():
            print(f"{inst}: {pos.size} units ({pos.risk_usd} USD risk)")
    """

    def __init__(
        self,
        portfolio_value: float,
        risk_pct: float = 1.0,
        min_size: float = 0.001,
    ):
        """
        Initialize sizer.

        Args:
            portfolio_value: Total portfolio value in USD
            risk_pct: Risk per trade as % of portfolio (e.g., 1.0 = 1%)
            min_size: Minimum position size to allow (avoid microsizes)
        """
        self.portfolio_value = portfolio_value
        self.risk_pct = risk_pct
        self.min_size = min_size

    def calculate(
        self,
        instruments: list[str],
        prices: Dict[str, float],
        atrs: Dict[str, float],
    ) -> RiskParityAllocation:
        """
        Calculate risk-parity position sizes.

        Args:
            instruments: List of instrument names
            prices: Dict {instrument -> current_price}
            atrs: Dict {instrument -> ATR value}

        Returns:
            RiskParityAllocation with sizes for each instrument

        Raises:
            ValueError: If any instrument missing price/ATR, or invalid inputs
        """

        # Validate inputs
        for inst in instruments:
            if inst not in prices:
                raise ValueError(f"Missing price for {inst}")
            if inst not in atrs:
                raise ValueError(f"Missing ATR for {inst}")
            if prices[inst] <= 0:
                raise ValueError(f"Invalid price for {inst}: {prices[inst]}")
            if atrs[inst] <= 0:
                raise ValueError(f"Invalid ATR for {inst}: {atrs[inst]}")

        # Calculate per-instrument risk
        risk_per_trade = self.portfolio_value * self.risk_pct / 100.0

        positions = {}
        total_notional = 0

        for inst in instruments:
            price = prices[inst]
            atr = atrs[inst]

            # size = risk / atr (in absolute price units, not percentage)
            # This assumes ATR is in the same currency/unit as price
            size = risk_per_trade / atr

            # Apply minimum size threshold
            if size < self.min_size:
                log.warning(
                    f"{inst}: calculated size {size:.6f} < minimum {self.min_size}, "
                    f"clamping to minimum"
                )
                size = self.min_size

            # Notional value
            notional = size * price

            # Dollar risk at 1 ATR SL
            risk_usd = size * atr

            pos = PositionSize(
                instrument=inst,
                size=float(size),
                risk_usd=float(risk_usd),
                notional_usd=float(notional),
                atr=float(atr),
            )

            positions[inst] = pos
            total_notional += notional

            log.info(
                f"{inst}: size={size:.6f}, notional=${notional:.2f}, "
                f"risk=${risk_usd:.2f}"
            )

        return RiskParityAllocation(
            positions=positions,
            portfolio_risk_usd=risk_per_trade * len(instruments),
            total_notional_usd=total_notional,
        )

    def rebalance(
        self,
        current_allocation: RiskParityAllocation,
        new_prices: Dict[str, float],
        new_atrs: Dict[str, float],
        rebalance_threshold_pct: float = 10.0,
    ) -> Optional[RiskParityAllocation]:
        """
        Rebalance positions if drift exceeds threshold.

        Args:
            current_allocation: Last calculated allocation
            new_prices: Updated prices
            new_atrs: Updated ATRs
            rebalance_threshold_pct: Rebalance if any instrument drifts >this %

        Returns:
            New allocation if rebalance needed, else None
        """

        instruments = list(current_allocation.positions.keys())

        # Calculate new allocation
        new_alloc = self.calculate(instruments, new_prices, new_atrs)

        # Check drift for each instrument
        max_drift_pct = 0
        for inst in instruments:
            old_size = current_allocation.positions[inst].size
            new_size = new_alloc.positions[inst].size

            if old_size > 0:
                drift_pct = abs(new_size - old_size) / old_size * 100.0
                if drift_pct > max_drift_pct:
                    max_drift_pct = drift_pct

        if max_drift_pct > rebalance_threshold_pct:
            log.info(
                f"Rebalance needed: max drift {max_drift_pct:.2f}% "
                f"(threshold {rebalance_threshold_pct}%)"
            )
            return new_alloc

        return None


def example_usage():
    """Example of risk-parity sizing."""

    # Portfolio
    portfolio_value = 10_000  # 10k USD
    risk_pct = 1.0  # 1% risk per trade

    sizer = RiskParityPositionSizer(
        portfolio_value=portfolio_value,
        risk_pct=risk_pct
    )

    # Current market data
    instruments = ['BTCUSD', 'DE40', 'SP500']
    prices = {
        'BTCUSD': 63_000,
        'DE40': 17_850,
        'SP500': 5_432,
    }
    atrs = {
        'BTCUSD': 2_000,      # Bitcoin: $2k ATR
        'DE40': 500,          # DAX: 500 pts ATR
        'SP500': 100,         # S&P: 100 pts ATR
    }

    # Calculate allocation
    alloc = sizer.calculate(instruments, prices, atrs)

    print("Risk-Parity Allocation:")
    print(f"Portfolio: ${portfolio_value:,.0f}")
    print(f"Risk per trade: {risk_pct}% = ${alloc.portfolio_risk_usd / len(instruments):,.2f} per instrument")
    print()

    for inst, pos in alloc.positions.items():
        print(
            f"{inst:10} | "
            f"Size: {pos.size:10.6f} | "
            f"Notional: ${pos.notional_usd:>10,.2f} | "
            f"Risk (1 ATR): ${pos.risk_usd:>8,.2f}"
        )

    print()
    print(f"Total Notional: ${alloc.total_notional_usd:,.2f}")
    print(f"Total Risk Exposure: ${alloc.portfolio_risk_usd:,.2f}")

    # Test rebalance
    print("\n--- After price/ATR update ---")
    new_prices = {
        'BTCUSD': 64_000,  # BTC up
        'DE40': 17_900,    # DAX up
        'SP500': 5_450,    # S&P up
    }
    new_atrs = {
        'BTCUSD': 2_200,   # Volatility increased
        'DE40': 480,       # DAX volatility decreased
        'SP500': 105,      # S&P vol up
    }

    rebalanced = sizer.rebalance(
        alloc,
        new_prices,
        new_atrs,
        rebalance_threshold_pct=10.0
    )

    if rebalanced:
        print("Rebalanced allocation:")
        for inst, pos in rebalanced.positions.items():
            old_size = alloc.positions[inst].size
            drift = (pos.size - old_size) / old_size * 100.0 if old_size > 0 else 0
            print(
                f"{inst:10} | Old: {old_size:.6f} → New: {pos.size:.6f} "
                f"(drift: {drift:+.1f}%)"
            )
    else:
        print("No rebalance needed (drift < 10%)")


if __name__ == "__main__":
    example_usage()
