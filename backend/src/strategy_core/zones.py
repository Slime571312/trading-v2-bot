"""Order Blocks / Fair Value Gaps / Equilibrium — Retracement-Zonen.

Quellen:
- Trading/Order Blocks.md
- Trading/FVG.md
- Trading/Equilibrium.md

In dieser Etappe 2 implementieren wir die Standard-Varianten:
- **OB:** Volle Range der letzten Gegen-Kerze vor dem Sweep (konservative Box).
  Die feinere Wick+Body-Start-Box aus Order Blocks.md ist Etappe-3-Verfeinerung.
- **FVG:** Klassisches 3-Kerzen-Pattern (Wicks der 1./3. Kerze überlappen nicht).
- **Equilibrium:** Exakt 50 % zwischen Sweep-Extreme und BOS-Extreme.
"""
from __future__ import annotations

import pandas as pd

from ._types import Equilibrium, FVG, OrderBlock, Side, StructureBreak, Sweep


def find_fvgs(
    df: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    direction: Side,
) -> list[FVG]:
    """Findet FVGs in `direction` zwischen `start_idx` und `end_idx` (inklusive).

    **Bullish FVG** (3-Kerzen, mittlere Kerze ist der impulsive Move nach oben):
      `low[i+1] > high[i-1]`  →  Gap-Range `(high[i-1], low[i+1])`

    **Bearish FVG:** gespiegelt, `high[i+1] < low[i-1]`.
    """
    if start_idx < 1:
        start_idx = 1
    if end_idx > len(df) - 2:
        end_idx = len(df) - 2
    if start_idx > end_idx:
        return []

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    times = list(df.index)
    fvgs: list[FVG] = []

    for i in range(start_idx, end_idx + 1):
        if direction == "long":
            if lows[i + 1] > highs[i - 1]:
                fvgs.append(FVG(
                    time=times[i], bar_idx=i,
                    upper=float(lows[i + 1]),
                    lower=float(highs[i - 1]),
                    direction="long",
                ))
        else:
            if highs[i + 1] < lows[i - 1]:
                fvgs.append(FVG(
                    time=times[i], bar_idx=i,
                    upper=float(lows[i - 1]),
                    lower=float(highs[i + 1]),
                    direction="short",
                ))
    return fvgs


def find_order_block(
    df: pd.DataFrame,
    sweep: Sweep,
    bos: StructureBreak,
) -> OrderBlock | None:
    """Letzte Gegen-Bewegung vor dem Sweep, der zum BOS führt.

    - Bullish-BOS (`direction='long'`)  →  suche die **letzte bearishe** Kerze
      (close < open) zwischen Sweep-Bar und ein paar Bars davor.
    - Bearish-BOS → analog, letzte bullishe Kerze.
    """
    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    times = list(df.index)

    # Vom Sweep-Bar rückwärts; max 20 Bars zurück suchen
    counter_is_down = bos.direction == "long"
    scan_end = max(0, sweep.bar_idx - 20)

    for i in range(sweep.bar_idx, scan_end - 1, -1):
        if i < 0:
            break
        is_down = closes[i] < opens[i]
        is_up = closes[i] > opens[i]
        if (counter_is_down and is_down) or (not counter_is_down and is_up):
            return OrderBlock(
                time=times[i], bar_idx=i,
                upper=float(highs[i]),
                lower=float(lows[i]),
                direction=bos.direction,
            )
    return None


def compute_equilibrium(
    sweep: Sweep,
    bos: StructureBreak,
    htf_df: pd.DataFrame,
) -> Equilibrium:
    """50 %-Marke zwischen Sweep-Extreme und BOS-Extreme.

    Bullish: tiefster Punkt der Sweep-Wick → BOS-Body-Close.
    Bearish: höchster Punkt der Sweep-Wick → BOS-Body-Close.
    """
    if bos.direction == "long":
        sweep_low = float(htf_df["low"].iloc[sweep.bar_idx])
        return Equilibrium(
            swing_high=bos.body_extreme,
            swing_low=sweep_low,
            eq=(bos.body_extreme + sweep_low) / 2.0,
        )
    else:
        sweep_high = float(htf_df["high"].iloc[sweep.bar_idx])
        return Equilibrium(
            swing_high=sweep_high,
            swing_low=bos.body_extreme,
            eq=(sweep_high + bos.body_extreme) / 2.0,
        )


def in_eq_zone(price: float, eq: Equilibrium, side: Side) -> bool:
    """Long: Preis < EQ (Discount). Short: Preis > EQ (Premium)."""
    if side == "long":
        return price < eq.eq
    return price > eq.eq
