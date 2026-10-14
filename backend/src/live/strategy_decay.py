"""Strategy-Decay-Detection via CUSUM — bidirektional.

Vault: Bot/Strategy-Decay-Detection.md.

**Klassisches CUSUM** mit zwei Akkumulatoren:
- `S+` läuft hoch wenn Trades besser als Baseline laufen → IMPROVEMENT-Signal
- `S-` läuft hoch wenn Trades schlechter als Baseline → DECAY-Signal

**Vorteil ggü. einfacher Rolling-WR:**
Random Variance (45 % WR bei 20 Trades) crashed nicht, nur eine echte
Verschiebung (45 % WR bei 60 Trades) → kein false alarm.

**Bidirektional (User-Wunsch 2026-06-26):**
- DECAY (S- > h) → Adaptive-Budget reduziert / pausiert (bereits via 7d-WR)
- IMPROVEMENT (S+ > h) → Adaptive-Budget bekommt **Decay-Bonus** dazu:
  Win-Streak-Bonus wird statt +3 auf +5 ausgedehnt, base_max +1

**Algorithmus:**
```
k = 0.5 × σ            # reference value (typisch halbe Standardabweichung)
S+_n = max(0, S+_{n-1} + (x_n - μ - k))
S-_n = max(0, S-_{n-1} + (μ - x_n - k))
Alarm wenn S+ > h × σ  oder  S- > h × σ
```

Hier x_n = trade_result als R-Multiple, μ = baseline_avg_r (Default 0.3 = Long-Term-Edge).
Schwelle h=4 (typisch für SPC) — bei kleineren Samples konservativ.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Literal


DecayState = Literal["stable", "decay", "improvement"]


@dataclass
class CusumResult:
    """Output von compute_cusum."""
    state: DecayState
    s_plus: float       # akkumulierte Up-Abweichung (positive R-Streak)
    s_minus: float      # akkumulierte Down-Abweichung (negative R-Streak)
    n_trades: int
    baseline_r: float   # μ
    sample_std: float   # σ (geschätzt)
    threshold_h: float
    reason: str


# Defaults aus Vault-Spec
_DEFAULT_BASELINE_R = 0.3      # ICT-Edge: ~+0.3R pro Trade langfristig
_DEFAULT_K_FACTOR = 0.5        # k = 0.5 × σ
_DEFAULT_H_FACTOR = 4.0        # Alarm wenn S > 4 × σ
_MIN_SAMPLE = 10               # < 10 Trades → kein CUSUM, "stable"
_FALLBACK_STD = 1.0            # Fallback wenn Sample-σ zu klein


def compute_cusum(
    closed_trades: Iterable,
    baseline_r: float = _DEFAULT_BASELINE_R,
    k_factor: float = _DEFAULT_K_FACTOR,
    h_factor: float = _DEFAULT_H_FACTOR,
    min_sample: int = _MIN_SAMPLE,
) -> CusumResult:
    """Returns CUSUM-Status über die übergebene Trade-Liste.

    `closed_trades` muss chronologisch sortiert sein (älteste zuerst).
    """
    trades = list(closed_trades)
    n = len(trades)

    if n < min_sample:
        return CusumResult(
            state="stable",
            s_plus=0.0, s_minus=0.0,
            n_trades=n,
            baseline_r=baseline_r,
            sample_std=_FALLBACK_STD,
            threshold_h=_DEFAULT_H_FACTOR * _FALLBACK_STD,
            reason=f"insufficient_sample:{n}<{min_sample}",
        )

    r_values = [t.r_multiple for t in trades]
    mean_r = sum(r_values) / n
    variance = sum((r - mean_r) ** 2 for r in r_values) / n
    sample_std = max(variance ** 0.5, _FALLBACK_STD * 0.1)
    k = k_factor * sample_std
    h_threshold = h_factor * sample_std

    s_plus = 0.0
    s_minus = 0.0
    for r in r_values:
        s_plus = max(0.0, s_plus + (r - baseline_r - k))
        s_minus = max(0.0, s_minus + (baseline_r - r - k))

    if s_minus > h_threshold:
        state = "decay"
        reason = (
            f"decay:S-={s_minus:.2f}>h={h_threshold:.2f} "
            f"(over {n} trades, mean_r={mean_r:+.2f})"
        )
    elif s_plus > h_threshold:
        state = "improvement"
        reason = (
            f"improvement:S+={s_plus:.2f}>h={h_threshold:.2f} "
            f"(over {n} trades, mean_r={mean_r:+.2f})"
        )
    else:
        state = "stable"
        reason = (
            f"stable:S+={s_plus:.2f} S-={s_minus:.2f} h={h_threshold:.2f}"
        )

    return CusumResult(
        state=state, s_plus=s_plus, s_minus=s_minus,
        n_trades=n, baseline_r=baseline_r,
        sample_std=sample_std,
        threshold_h=h_threshold,
        reason=reason,
    )


def cusum_for_instrument(
    now: datetime,
    instrument: str,
    closed_trades: Iterable,
    lookback_days: int = 14,
) -> CusumResult:
    """CUSUM über die letzten `lookback_days` Tage für ein Instrument."""
    cutoff = now - timedelta(days=lookback_days)
    inst_recent = sorted(
        [t for t in closed_trades
         if t.instrument == instrument and t.close_time >= cutoff],
        key=lambda t: t.close_time,
    )
    return compute_cusum(inst_recent)
