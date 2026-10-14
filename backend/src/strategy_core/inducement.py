"""Inducement-Detection — bewertet die Qualität eines erkannten Sweeps.

**Konzept:** Nicht jeder Wick durch einen Liquidity-Level ist ein echter Sweep.
ICT unterscheidet:
- **Echter Sweep**: Wick weit über/unter Level, starkes Body-Reversal, Follow-Through
  in den nächsten Bars (Displacement)
- **Inducement** (Stop-Hunt): Wick knapp über Level, kein Follow-Through,
  Preis driftet weiter in die ursprüngliche Richtung oder seitwärts

**Bidirektional als Confidence-Faktor:**
- Score ≥ 0.75 → +Boost (klare Liquidity-Grab, hohe Trade-Conviction)
- Score < 0.40 → Penalty (wahrscheinlich Inducement, Setup wackelig)
- Dazwischen: linear-skalierte Position-Size

**Drei Komponenten:**
1. **Wick-Strength** — wie weit ist der Wick über/unter dem Level relativ zur Bar-Range?
2. **Body-Reversal** — wie weit kommt der Body relativ zum Wick zurück?
3. **Displacement** — bewegt sich der Preis in den nächsten 2-3 Bars klar weg vom Level?

Spec: ICT-Konzept "Inducement vs. True Sweep". Vault hat keine separate Note,
aber das Konzept ist im Playbook erwähnt.
"""
from __future__ import annotations

import pandas as pd

from ._types import Sweep


_DISPLACEMENT_BARS = 3      # Wie viele Bars nach dem Sweep schauen wir?
_FLOOR = 0.0
_CEIL = 1.0


def wick_strength(sweep: Sweep, df: pd.DataFrame) -> float:
    """Wie weit ging der Wick über/unter dem Level relativ zur Bar-Range?

    BSL: (high - level) / (high - low).
    SSL: (level - low) / (high - low).

    1.0 = Wick ist 100% der Bar (extrem) → klarer Sweep
    0.0 = Wick gerade noch das Level erreicht → Inducement-Verdacht
    """
    if sweep.bar_idx >= len(df):
        return 0.5
    bar = df.iloc[sweep.bar_idx]
    high = float(bar["high"])
    low = float(bar["low"])
    bar_range = high - low
    if bar_range <= 0:
        return 0.5
    if sweep.direction == "bsl":
        wick = high - sweep.level
    else:
        wick = sweep.level - low
    return max(_FLOOR, min(_CEIL, wick / bar_range))


def body_reversal(sweep: Sweep, df: pd.DataFrame) -> float:
    """Wie weit kam der Close vom Wick-Extrem zurück?

    BSL: Wick endete bei high, close sollte deutlich darunter sein.
    Reversal = (high - close) / wick_size.
    - close = high (am Wick-Extrem) → 0
    - close auf Level → 1.0 (Wick komplett negiert)
    - close darunter → > 1 (gecappt)
    """
    if sweep.bar_idx >= len(df):
        return 0.5
    bar = df.iloc[sweep.bar_idx]
    high = float(bar["high"])
    low = float(bar["low"])
    close = float(bar["close"])

    if sweep.direction == "bsl":
        wick_size = high - sweep.level
        reversal = high - close
    else:
        wick_size = sweep.level - low
        reversal = close - low

    if wick_size <= 0:
        return 0.5
    # Normalisierung: close komplett zurück zum Level = 1.0
    return max(_FLOOR, min(_CEIL, reversal / wick_size))


def displacement_after(sweep: Sweep, df: pd.DataFrame,
                        bars: int = _DISPLACEMENT_BARS) -> float:
    """Wie stark bewegt sich der Preis nach dem Sweep weg vom Level?

    BSL-Sweep (Short erwartet): Preis sollte runter gehen.
    SSL-Sweep (Long erwartet): Preis sollte hoch.

    Misst displacement relativ zur Wick-Größe:
    1.0 = Preis bewegt sich um 2× Wick weg → klare Displacement
    0.0 = Preis steht oder geht zurück über Level → schwach
    """
    if sweep.bar_idx + bars >= len(df):
        return 0.5
    sweep_bar = df.iloc[sweep.bar_idx]
    next_bars = df.iloc[sweep.bar_idx + 1 : sweep.bar_idx + 1 + bars]

    if sweep.direction == "bsl":
        wick_size = float(sweep_bar["high"]) - sweep.level
        # Niedrigster Low in den Folge-Bars
        min_low = float(next_bars["low"].min())
        displacement = sweep.level - min_low
    else:
        wick_size = sweep.level - float(sweep_bar["low"])
        max_high = float(next_bars["high"].max())
        displacement = max_high - sweep.level

    if wick_size <= 0:
        return 0.5
    return max(_FLOOR, min(_CEIL, displacement / (2 * wick_size)))


def sweep_quality_score(sweep: Sweep, df: pd.DataFrame) -> tuple[float, dict]:
    """Gesamt-Score eines Sweeps. Returns (score, breakdown_dict).

    Gewichtung:
    - Wick-Strength: 30 %
    - Body-Reversal: 30 %
    - Displacement: 40 % (wichtigstes Kriterium — ohne Follow-Through kein echter Sweep)

    Score-Skala:
    - ≥ 0.75 → "strong" (echter Sweep)
    - 0.40–0.75 → "weak" (möglicher Sweep, aber unsicher)
    - < 0.40 → "inducement" (vermutlich Stop-Hunt, kein echter Move)
    """
    w_str = wick_strength(sweep, df)
    b_rev = body_reversal(sweep, df)
    disp = displacement_after(sweep, df)
    score = 0.30 * w_str + 0.30 * b_rev + 0.40 * disp
    breakdown = {
        "wick_strength": round(w_str, 3),
        "body_reversal": round(b_rev, 3),
        "displacement": round(disp, 3),
        "total": round(score, 3),
    }
    return score, breakdown


def classify_sweep(score: float) -> str:
    """Klartext-Label für Score."""
    if score >= 0.75:
        return "strong"
    if score >= 0.40:
        return "weak"
    return "inducement"
