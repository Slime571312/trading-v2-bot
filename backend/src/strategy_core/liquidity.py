"""Liquiditäts-Marker auf HTF — bias-gefiltert, prominent-gefiltert.

Quelle: Trading/LQS.md + Trading/Bot/Playbook.md (Schritt 2)

- **Long-Bias**  → suche **SSL** (prominente Pivot-Lows = Sell-Side-Liquidität)
- **Short-Bias** → suche **BSL** (prominente Pivot-Highs = Buy-Side-Liquidität)

Prominent-Filter (LQS.md: "Hat es einen BOS ausgelöst?"):
  Ein Pivot-Low ist prominent, wenn danach der Markt ein Higher High bildete
  (d.h. das Low verankerte einen Aufwärtsleg — echte Stops liegen darunter).
  Analog für Highs in Short-Bias.

  Nicht-prominente Pivots (kleine Wobbler in Konsolidierung) werden gefiltert.
"""
from __future__ import annotations

import pandas as pd

from ._types import BiasDir, Swing
from .pivots import find_pivots


def _is_prominent(pivot: Swing, all_pivots: list[Swing]) -> bool:
    """Prominent = nach diesem Pivot machte die Gegenseite ein neues strukturelles Extrem.

    Aus LQS.md: 'Hat es einen BOS ausgelöst? → prominent'
    - Low prominent  → danach: Higher High (Low verankerte Aufwärtsleg, Stops darunter)
    - High prominent → danach: Lower Low  (High verankerte Abwärtsleg, Stops darüber)

    Das jüngste Pivot wird immer als prominent gezählt (noch nicht bestätigt aber aktuell).
    """
    opp_kind = "high" if pivot.kind == "low" else "low"

    prev_opp = [p for p in all_pivots if p.kind == opp_kind and p.bar_idx < pivot.bar_idx]
    after_opp = [p for p in all_pivots if p.kind == opp_kind and p.bar_idx > pivot.bar_idx]

    if not prev_opp:
        return True  # kein Kontext → include
    if not after_opp:
        return True  # jüngstes Pivot, noch kein bestätigender Move — conservative include

    ref = max(prev_opp, key=lambda p: p.bar_idx).price   # letztes Gegenteil vor dem Pivot
    nxt = min(after_opp, key=lambda p: p.bar_idx).price  # erstes Gegenteil nach dem Pivot

    if pivot.kind == "low":
        return nxt > ref   # Higher High nach dem Low → Low ist prominent SSL
    else:
        return nxt < ref   # Lower Low nach dem High → High ist prominent BSL


def liquidity_levels(
    df: pd.DataFrame,
    bias_direction: BiasDir,
    n_left: int = 3,
    n_right: int = 3,
    only_unswept: bool = False,
    prominent_only: bool = True,
) -> list[Swing]:
    """Liefert relevante Liquiditäts-Levels für die gegebene Bias-Richtung.

    Args:
        df: HTF-OHLCV (z.B. 1h/30m/15m).
        bias_direction: long → SSL (Lows), short → BSL (Highs), neutral → [].
        only_unswept: nur Levels deren Wick noch nicht durchbrochen wurde.
        prominent_only: nur strukturell prominente Pivots (BOS-getriggert).
    """
    if bias_direction == "neutral":
        return []

    pivots = find_pivots(df, n_left, n_right)
    target_kind = "low" if bias_direction == "long" else "high"
    relevant = [p for p in pivots if p.kind == target_kind]

    if prominent_only:
        relevant = [p for p in relevant if _is_prominent(p, pivots)]

    if not only_unswept:
        return relevant

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    unswept: list[Swing] = []
    for p in relevant:
        after = slice(p.bar_idx + 1, len(df))
        if target_kind == "low":
            swept = (lows[after] < p.price).any()
        else:
            swept = (highs[after] > p.price).any()
        if not swept:
            unswept.append(p)
    return unswept
