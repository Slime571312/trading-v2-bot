"""RR-Berechnung + Stop-Loss-/Take-Profit-Ableitung.

Quellen:
- Trading/SL und TP.md
- Trading/Bot/SL-Placement.md (ATR-Buffer, Multi-Method)
- Trading/Bot/TP-Strategy.md (Min-RR-Filter)
- Trading/Bot/Playbook.md (Schritt 5)
"""
from __future__ import annotations

import pandas as pd

from ._types import Side, Sweep, Swing


# Aus SL-Placement.md: per-Instrument kalibrierte ATR-Multiplier.
# ATR×Multiplier statt Pip-Buffer skaliert mit der Volatilität.
# SP500 von 0.12 → 0.15 nach Live-Analyse 2026-06-24: SP500 in high-Vol-Regime
# zeigte 57% WR aber -0.27R avg → SLs wurden zu eng gerissen. Weiterer Buffer
# soll Whipsaws bei volatilen Sessions reduzieren.
ATR_MULT: dict[str, float] = {
    "BTC":    0.15,
    "DE40":   0.10,
    "NASDAQ": 0.10,
    "SP500":  0.15,
}

# Aus TP-Strategy.md: Setup wird verworfen wenn RR1 < MIN_RR
MIN_RR = 1.5


def _atr(htf_df: pd.DataFrame, period: int = 14) -> float:
    """True Range Average — robuster als reine close-Differenz."""
    if len(htf_df) < period + 1:
        return 0.0
    high = htf_df["high"].astype(float)
    low = htf_df["low"].astype(float)
    close = htf_df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_val = tr.tail(period).mean()
    return float(atr_val) if pd.notna(atr_val) else 0.0


def compute_sl(
    side: Side,
    sweep: Sweep,
    htf_df: pd.DataFrame,
    instrument: str = "DE40",
) -> float:
    """SL jenseits der Sweep-Wick + ATR-basierter Buffer.

    Aus SL-Placement.md: ATR×0.10–0.15 statt prozentual.
    Pip-Buffer (10–20 Pips) ist cross-Instrument unsinnig — ATR skaliert mit Volatilität.
    """
    atr = _atr(htf_df, period=14)
    mult = ATR_MULT.get(instrument, 0.12)
    buffer = atr * mult

    if side == "long":
        wick = float(htf_df["low"].iloc[sweep.bar_idx])
        # Wenn ATR=0 (zu wenig Bars), Fallback auf 0.1% proportional
        return wick - buffer if buffer > 0 else wick * (1 - 0.001)
    else:
        wick = float(htf_df["high"].iloc[sweep.bar_idx])
        return wick + buffer if buffer > 0 else wick * (1 + 0.001)


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


_MIN_TP1_R = 0.5    # TP1 muss mind. 0.5R Distanz vom Entry haben


def compute_multi_tp_targets(
    side: Side,
    entry: float,
    htf_pivots: list[Swing],
    irl_fvgs: list | None = None,
    sl_distance: float | None = None,
) -> tuple[float | None, float | None, float | None]:
    """Liefert (tp1, tp2, tp3) — 3-Tier-Targets nach ERL/IRL-Modell.

    Spec: Vault/Bot/TP-Staffelung.md + TP-Strategy.md + ERL-IRL.md.

    **Mit `irl_fvgs` (ERL/IRL-Modell):** TP1 = nächste FVG-Fill (IRL),
    TP2 = nächster Pivot jenseits davon (ERL = Dealing-Range-Boundary),
    TP3 = mittlerer/entferntester Pivot (HTF-Draw).

    **Ohne `irl_fvgs` (Legacy-Fallback):** TP1/TP2/TP3 = nächster/mittlerer/
    entferntester Pivot — die alte Heuristik bleibt rückwärtskompatibel.

    **Mit `sl_distance`**: FVGs die näher als `_MIN_TP1_R × sl_distance` am Entry
    liegen werden ignoriert → verhindert Mini-Wins (R < 0.5) bei volatilen Märkten
    wo FVGs alle paar Punkte entstehen. Live-Analyse 2026-06-24: SP500 06-23 R=0.14
    war genau dieser Fall.

    Returns:
      - (tp1, tp2, tp3) wenn alle 3 Tiers gefüllt
      - (tp1, None, tp3) wenn nur 2 Tiers
      - (None, None, tp3) wenn 1 Pivot
      - (None, None, None) wenn 0 Pivots
    """
    if side == "long":
        candidates = sorted(
            [p for p in htf_pivots if p.kind == "high" and p.price > entry],
            key=lambda p: p.price,  # aufsteigend = nähester zuerst
        )
    else:
        candidates = sorted(
            [p for p in htf_pivots if p.kind == "low" and p.price < entry],
            key=lambda p: -p.price,  # absteigend = nähester zuerst
        )

    if not candidates:
        return (None, None, None)

    nearest_pivot = candidates[0].price
    # Mindest-Distance für TP1 (verhindert FVG zu nah am Entry)
    min_tp1_dist = (sl_distance or 0.0) * _MIN_TP1_R

    # ERL/IRL-Pfad: wenn FVG zwischen Entry und nächstem Pivot liegt → IRL-Fill als TP1
    # (Vault/Bot/ERL-IRL.md: „Zwischen zwei ERL-Sweeps läuft Preis durch IRL").
    irl_tp1: float | None = None
    if irl_fvgs:
        # Wähle FVG mit Mittelpunkt am nähesten am Entry (= erste IRL die getroffen wird)
        # — aber mind. _MIN_TP1_R × sl_distance entfernt, sonst R wird zu klein.
        valid_fvgs = []
        for f in irl_fvgs:
            fvg_mid = (f.upper + f.lower) / 2.0
            if side == "long":
                # FVG muss komplett zwischen Entry und Pivot liegen
                if entry < f.lower and f.upper < nearest_pivot:
                    dist = fvg_mid - entry
                    if dist >= min_tp1_dist:
                        valid_fvgs.append((fvg_mid, dist))
            else:
                if nearest_pivot < f.lower and f.upper < entry:
                    dist = entry - fvg_mid
                    if dist >= min_tp1_dist:
                        valid_fvgs.append((fvg_mid, dist))
        if valid_fvgs:
            # Nächste FVG (kleinste Distanz zu Entry)
            irl_tp1 = min(valid_fvgs, key=lambda x: x[1])[0]

    if irl_tp1 is not None:
        # ERL/IRL-Struktur: tp1=FVG-Fill, tp2=nächster Pivot (ERL),
        # tp3=entferntester Pivot wenn vorhanden, sonst tp2 = TP3
        if len(candidates) >= 2:
            return (irl_tp1, nearest_pivot, candidates[-1].price)
        return (irl_tp1, None, nearest_pivot)

    # Legacy-Fallback (keine FVG im Pfad)
    if len(candidates) == 1:
        return (None, None, nearest_pivot)
    if len(candidates) == 2:
        return (nearest_pivot, None, candidates[1].price)
    # ≥3 Pivots: tp1 = nähester, tp3 = entferntester, tp2 = mittlerer Median
    mid_idx = len(candidates) // 2
    return (nearest_pivot, candidates[mid_idx].price, candidates[-1].price)


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


# ── OTE — Optimal Trade Entry ────────────────────────────────────────────────

from dataclasses import dataclass


@dataclass(frozen=True)
class OTEZone:
    """Fibonacci 62–79% Retracement des Dealing-Range-Legs."""

    ote_low:  float  # 79%-Level für Bullish / 62%-Level für Bearish
    ote_705:  float  # 70.5%-Sweet-Spot
    ote_high: float  # 62%-Level für Bullish / 79%-Level für Bearish
    sl_ref:   float  # 1.0-Level — SL-Basis jenseits der Dealing-Range


def calculate_ote_zone(
    swing_low: float,
    swing_high: float,
    bias: int,  # 1 = bullish (Leg Low→High), -1 = bearish (Leg High→Low)
) -> OTEZone:
    """OTE 62–79% Retracement des Impulse-Legs (nach Sweep + BOS).

    Bullish: OTE liegt unter swing_high (Retrace nach unten in Discount).
    Bearish: OTE liegt über swing_low (Retrace nach oben in Premium).
    sl_ref = 1.0-Level = Dealing-Range-Basis = Invalidierungspunkt.
    """
    leg = swing_high - swing_low
    if bias == 1:
        return OTEZone(
            ote_low=swing_high - leg * 0.79,
            ote_705=swing_high - leg * 0.705,
            ote_high=swing_high - leg * 0.62,
            sl_ref=swing_low,
        )
    return OTEZone(
        ote_low=swing_low + leg * 0.62,
        ote_705=swing_low + leg * 0.705,
        ote_high=swing_low + leg * 0.79,
        sl_ref=swing_high,
    )


def is_in_ote_zone(price: float, zone: OTEZone) -> bool:
    """True wenn price innerhalb der OTE-Zone liegt (ote_low ≤ price ≤ ote_high)."""
    return zone.ote_low <= price <= zone.ote_high


def fib_tp_extension(
    swing_low: float,
    swing_high: float,
    level: float,
    bias: int,
) -> float:
    """Std-Dev-Extension als TP-Kandidat für OTE-Entries.

    level=-0.27 → TP1 (127% des Legs, konservativ)
    level=-0.62 → TP2 (162% des Legs, aggressiv)
    """
    extension = abs(level) * (swing_high - swing_low)
    return (swing_high + extension) if bias == 1 else (swing_low - extension)


def ote_confluence_entry(
    price: float,
    zone: OTEZone,
    ob_low: float,
    ob_high: float,
) -> bool:
    """Zone-in-Zone: Preis liegt in OTE (62–79%) UND in OB/FVG — höchste Confluence."""
    return is_in_ote_zone(price, zone) and (ob_low <= price <= ob_high)
