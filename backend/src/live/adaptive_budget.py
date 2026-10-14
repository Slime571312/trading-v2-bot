"""Adaptive Trade-Budget — dynamisches Tageslimit basierend auf Performance.

**Konzept:**
- Standard-Limit: `max_trades_per_pair_per_day` aus TradeLimitsConfig (Default 2)
- Wenn die heutigen Trades gut liefen → mehr Trades erlauben (bis cap)
- Wenn ein extra-Trade verliert → sofort zurück auf Standard-Limit (= effektiv 0 weitere)

**Regeln (Vault-Konzept "Safety-Guardrails" + User-Wunsch 2026-06-26):**

| Bedingung | Effekt |
|---|---|
| 0 Trades heute | base_limit (2) |
| Alle heutigen Trades sind Wins UND avg R ≥ 0.5 | base_limit + 1 bonus (max 5) |
| Letzter Trade ist SL/Loss (R ≤ 0) | bonus = 0 → kein extra Trade mehr |
| 7d Win-Rate < 35% | base_limit - 1 (defensive: max 1/Tag) |
| 7d Win-Rate < 30% | base_limit = 0 (pause) |

Returns: effective_max_trades_per_day für DIESES Instrument.

Schnittstelle ist additiv zu trade_limits.can_trade — wird VOR dem `max_trades_per_pair_per_day`-Check als Override gesetzt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


@dataclass
class BudgetDecision:
    effective_max_trades: int
    reason: str           # "base" | "bonus_win_streak" | "decay_pause" | "weak_perf" | "cusum_improvement"
    base_max: int
    bonus: int
    today_n: int
    today_avg_r: float
    week_win_rate: float | None
    cusum_state: str = "stable"    # "stable" | "decay" | "improvement"


# Caps damit auch bei guten Streaks nicht unkontrolliert getradet wird.
_BONUS_CAP = 3                  # Standard-Bonus-Cap (also Default 2 → max 5)
_BONUS_CAP_IMPROVEMENT = 5      # Bei CUSUM-Improvement-Signal: Cap auf +5 erweitert
_GOOD_TRADE_R_THRESHOLD = 0.5   # Trade gilt als "richtig gut" bei R ≥ 0.5
_WEEK_WR_REDUCE = 0.35          # < 35% WR → defensiv
_WEEK_WR_PAUSE = 0.30           # < 30% WR → pause
_MIN_WEEK_SAMPLE = 5            # weniger als 5 Trades → kein WR-Gate (zu unsicher)


def compute_effective_budget(
    now: datetime,
    instrument: str,
    closed_trades: Iterable,    # list[ClosedTrade] — heute + Historie
    base_max: int,
) -> BudgetDecision:
    """Liefert das aktuelle Tages-Limit für dieses Instrument."""
    today = now.date()
    week_start = today - timedelta(days=7)

    # Heute geclosed
    todays = [
        t for t in closed_trades
        if t.instrument == instrument and t.close_time.date() == today
    ]
    # Letzte 7 Tage (rolling, nicht ISO-week)
    weeks = [
        t for t in closed_trades
        if t.instrument == instrument and t.close_time.date() >= week_start
    ]

    # 7d Win-Rate (nur wenn Sample > _MIN_WEEK_SAMPLE)
    week_wr: float | None = None
    if len(weeks) >= _MIN_WEEK_SAMPLE:
        wins = sum(1 for t in weeks if t.r_multiple > 0)
        week_wr = wins / len(weeks)

    # CUSUM-Status über 14 Tage (statistisch saubere Decay/Improvement-Erkennung).
    # Gibt "stable"/"decay"/"improvement" zurück. Wirkt bidirektional auf das Limit.
    from .strategy_decay import cusum_for_instrument
    cusum = cusum_for_instrument(now, instrument, closed_trades)

    # ── Defensiv-Gates: 7d-WR niedrig OR CUSUM-Decay → reduzieren ─────────
    if (week_wr is not None and week_wr < _WEEK_WR_PAUSE) or cusum.state == "decay":
        reason = (
            f"decay_pause:cusum_S-={cusum.s_minus:.2f}>h={cusum.threshold_h:.2f}"
            if cusum.state == "decay"
            else f"decay_pause:wr_{week_wr:.0%}<{_WEEK_WR_PAUSE:.0%}_7d"
        )
        return BudgetDecision(
            effective_max_trades=0, reason=reason,
            base_max=base_max, bonus=0,
            today_n=len(todays), today_avg_r=_avg_r(todays),
            week_win_rate=week_wr, cusum_state=cusum.state,
        )
    if week_wr is not None and week_wr < _WEEK_WR_REDUCE:
        # Defensiv: nur 1 Trade pro Tag, kein Bonus
        return BudgetDecision(
            effective_max_trades=max(1, base_max - 1),
            reason=f"weak_perf:wr_{week_wr:.0%}<{_WEEK_WR_REDUCE:.0%}_7d",
            base_max=base_max, bonus=0,
            today_n=len(todays), today_avg_r=_avg_r(todays),
            week_win_rate=week_wr, cusum_state=cusum.state,
        )

    # CUSUM-Improvement → erweiterter base_max + erweiterter Bonus-Cap
    # Bei statistisch belegter positiver Streak: das ist KEIN Glück, sondern Edge.
    effective_base = base_max
    effective_cap = _BONUS_CAP
    if cusum.state == "improvement":
        effective_base = base_max + 1     # +1 zur Basis (statt 2 → 3)
        effective_cap = _BONUS_CAP_IMPROVEMENT   # +5 statt +3 als Cap

    # ── Standard: bei weniger als effective_base trades → effective_base ──
    if len(todays) < effective_base:
        reason = "cusum_improvement_base+1" if cusum.state == "improvement" else "base"
        return BudgetDecision(
            effective_max_trades=effective_base,
            reason=reason,
            base_max=base_max, bonus=effective_base - base_max,
            today_n=len(todays), today_avg_r=_avg_r(todays),
            week_win_rate=week_wr, cusum_state=cusum.state,
        )

    # ── Bonus-Phase: effective_base erreicht, prüfe ob Streak ─────────────
    # Bedingung 1: Letzter Trade darf NICHT verlieren (sonst sofort Schluss)
    last_trade = todays[-1]
    if last_trade.r_multiple <= 0:
        return BudgetDecision(
            effective_max_trades=effective_base,    # = bereits erreicht → kein weiterer
            reason="last_trade_loss_no_bonus",
            base_max=base_max, bonus=effective_base - base_max,
            today_n=len(todays), today_avg_r=_avg_r(todays),
            week_win_rate=week_wr, cusum_state=cusum.state,
        )

    # Bedingung 2: Alle heutigen Trades sind Wins mit gutem avg R
    all_wins = all(t.r_multiple > 0 for t in todays)
    avg_r = _avg_r(todays)
    if not all_wins or avg_r < _GOOD_TRADE_R_THRESHOLD:
        return BudgetDecision(
            effective_max_trades=effective_base,
            reason=f"no_bonus:wins_only_avg_r={avg_r:.2f}",
            base_max=base_max, bonus=effective_base - base_max,
            today_n=len(todays), today_avg_r=avg_r,
            week_win_rate=week_wr, cusum_state=cusum.state,
        )

    # Bonus berechnen: pro extra-Trade über effective_base → bonus-Step
    # Cap variiert nach CUSUM-State: stable=3, improvement=5
    bonus_steps = min(effective_cap, len(todays) - effective_base + 1)
    base_for_bonus = effective_base - base_max + bonus_steps
    reason_prefix = "cusum_improvement_" if cusum.state == "improvement" else ""
    return BudgetDecision(
        effective_max_trades=effective_base + bonus_steps,
        reason=f"{reason_prefix}bonus_win_streak:{len(todays)}wins_avg_r={avg_r:.2f}_bonus={base_for_bonus}",
        base_max=base_max, bonus=base_for_bonus,
        today_n=len(todays), today_avg_r=avg_r,
        week_win_rate=week_wr, cusum_state=cusum.state,
    )


def _avg_r(trades: list) -> float:
    if not trades:
        return 0.0
    return sum(t.r_multiple for t in trades) / len(trades)
