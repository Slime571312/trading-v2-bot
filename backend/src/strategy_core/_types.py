"""Strategy-Core Dataclasses — gemeinsames Typ-Vokabular für alle Module.

Slots-frozen Dataclasses für Werte (Swing/Sweep/FVG/OB/Equilibrium/Bias/BOS),
mutable Dataclass nur für Signal (das Endprodukt).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

Side = Literal["long", "short"]
BiasDir = Literal["long", "short", "neutral"]
SignalVariant = Literal["primary", "ob_retest", "fvg_retest", "ultimate", "ote_ob_entry"]
SwingKind = Literal["high", "low"]
SweepDir = Literal["bsl", "ssl"]


@dataclass(slots=True, frozen=True)
class Swing:
    """Ein Pivot-High oder Pivot-Low."""

    time: pd.Timestamp
    price: float
    kind: SwingKind
    bar_idx: int


@dataclass(slots=True, frozen=True)
class Bias:
    """Daily-Bias = Richtung des jüngsten Daily-BoS."""

    direction: BiasDir
    last_bos_time: pd.Timestamp | None = None
    last_bos_level: float | None = None
    last_bos_index: int | None = None


@dataclass(slots=True, frozen=True)
class Sweep:
    """Wick durchsticht Level, Body schließt zurück innerhalb der Range."""

    time: pd.Timestamp
    bar_idx: int
    level: float
    swing: Swing
    direction: SweepDir  # bsl = sweep über High, ssl = sweep unter Low
    tf: str


@dataclass(slots=True, frozen=True)
class StructureBreak:
    """LTF-BOS in Bias-Richtung nach einem Sweep."""

    time: pd.Timestamp
    bar_idx: int
    body_extreme: float  # max(open,close) bei bullish-BOS, min bei bearish
    broken_swing: Swing
    direction: Side


@dataclass(slots=True, frozen=True)
class FVG:
    """Fair Value Gap — 3-Kerzen-Imbalance."""

    time: pd.Timestamp
    bar_idx: int  # Index der mittleren Kerze (impulsiver Move)
    upper: float
    lower: float
    direction: Side  # long = bullish FVG (wird in Discount gesucht)


@dataclass(slots=True, frozen=True)
class OrderBlock:
    """Order Block — letzte Gegen-Bewegung vor dem Sweep."""

    time: pd.Timestamp
    bar_idx: int
    upper: float
    lower: float
    direction: Side


@dataclass(slots=True, frozen=True)
class Equilibrium:
    """50%-Marke zwischen Sweep-Extreme und BOS-Extreme."""

    swing_high: float
    swing_low: float
    eq: float


@dataclass(slots=True)
class Signal:
    """Output des Engine-Orchestrators — das, was Backtest und Live-Bot konsumieren."""

    time: pd.Timestamp
    instrument: str
    side: Side
    entry: float
    sl: float
    tp: float           # finales (= weitestes) TP — Backward-Compat
    rr: float
    variant: SignalVariant
    htf_used: str
    ltf_used: str
    bias: Bias
    sweep: Sweep
    structure_break: StructureBreak
    ob: OrderBlock | None = None
    fvg: FVG | None = None
    equilibrium: Equilibrium | None = None
    # Conviction-Score (Bot/Conviction-Score.md)
    conviction_score: int | None = None
    conviction_grade: str | None = None   # "A" | "B" | "skip" (skip wird gar nicht returned)
    size_multiplier: float = 1.0          # 1.0 für A, 0.5 für B, 0.0 für skip (filtered)
    adx_regime: str | None = None         # "trending" | "uncertain" | "ranging"
    # Multi-TP-Staffelung (Vault/Bot/TP-Staffelung.md, TP-Strategy.md)
    # tp1 = nächste IRL (FVG/Pivot), tp2 = ERL (BSL/SSL-Cluster), tp = HTF-Draw
    # Wenn tp1/tp2 None → Engine konnte keine 3-Tier-Struktur bauen, Loop fällt
    # auf alte 2-Tier-Logik (halbweg + voll) zurück.
    tp1: float | None = None
    tp2: float | None = None
    # Splits müssen zu 1.0 summieren. Default 50/50/0 = B-Setup ohne Runner.
    # A-Setup mit HTF-Draw setzt 40/40/20.
    pct1: float = 0.5
    pct2: float = 0.5
    pct3: float = 0.0
    # Trade-Journal-Tags (Vault/Bot/Trade-Journal.md): Snapshot der Kontext-Variablen
    # zum Zeitpunkt des Entries — ermöglicht später Performance-Attribution nach
    # Session, Vol-Regime, Wochentag, Conviction-Score etc.
    session_label: str | None = None      # london / ny / crypto-hours / closed / weekend / holiday
    atr_regime: str | None = None         # low / normal / high (für regime-Bucketing)
    atr_pct: float | None = None          # rohe ATR%/Close für quantitative Auswertung
    vol_size_factor: float | None = None  # kontinuierlicher Vol-Faktor (z.B. 0.85)
    weekday: int | None = None            # 0=Montag, 6=Sonntag (Power-of-3 Bucketing)
    # Confidence-Score (Vault/Bot/Signal-Confidence-Scoring.md)
    confidence: float | None = None       # 0.0–1.0 (4-Faktoren-Score)
    confidence_tier: str | None = None    # "skip" | "reduced" | "normal" | "high"
