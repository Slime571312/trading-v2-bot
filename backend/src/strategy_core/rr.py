"""RR-Berechnung + Stop-Loss-/Take-Profit-Ableitung.

Quellen:
- Trading/SL und TP.md
- Trading/Bot/Playbook.md (Schritt 5)
"""
from __future__ import annotations

import pandas as pd

from ._types import Side, Sweep, Swing


def compute_sl(side: Side, sweep: Sweep, htf_df: pd.DataFrame, buffer_pct: float = 0.001) -> float:
    """SL jenseits der Sweep-Wick + Spread-Buffer.

    `buffer_pct` ist relativ zum Wick-Preis (Default 0.1 % — robust für CFDs;
    broker-spezifischer Wert kommt in Etappe 3 als `slippage`-Modell dazu).
    """
    if side == "long":
        wick = float(htf_df["low"].iloc[sweep.bar_idx])
        return wick * (1 - buffer_pct)
    else:
        wick = float(htf_df["high"].iloc[sweep.bar_idx])
        return wick * (1 + buffer_pct)


def find_tp_target(side: Side, entry: float, htf_pivots: list[Swing]) -> float | None:
    """TP = **entferntester** gegenüberliegender Pivot (= größtes verfügbares RR).

    Aus dem Magnet-Modell in LQS.md: der größte Liquidity-Pool zieht den Preis,
    nicht jeder Mini-Wackler dazwischen. Die nächstgelegenen Pivots sind oft nur
    Range-Noise — der **prominente** Counter-Pool sitzt weiter weg. In v2 wählen
    wir konsistent den entferntesten Pivot in Trade-Richtung; Partial-TPs (TP1/
    TP2/TP3 mit Cuts dazwischen) sind eine Etappe-3-Erweiterung.

    - Long  → höchster Pivot-High über Entry.
    - Short → tiefster Pivot-Low unter Entry.
    """
    if side == "long":
        candidates = [p for p in htf_pivots if p.kind == "high" and p.price > entry]
        return max(candidates, key=lambda p: p.price).price if candidates else None
    else:
        candidates = [p for p in htf_pivots if p.kind == "low" and p.price < entry]
        return min(candidates, key=lambda p: p.price).price if candidates else None


def rr_ratio(side: Side, entry: float, sl: float, tp: float) -> float:
    """(TP - Entry) / (Entry - SL) für Long, gespiegelt für Short.

    Returns 0.0 bei ungültiger Konstellation (SL falsche Seite, TP falsche Seite).
    """
    if side == "long":
        if entry <= sl or tp <= entry:
            return 0.0
        return (tp - entry) / (entry - sl)
    else:
        if entry >= sl or tp >= entry:
            return 0.0
        return (entry - tp) / (sl - entry)
