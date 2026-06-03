"""TradeLimitsGate — Gate 0 vor Signal-Eval (aus Bot/TradeGate.md + Trade-Limits).

Hard-Gates die VOR jedem Signal-Eval prüfen:

  1. Daily Loss Limit (Risk.md: max 1-3% pro Tag) → 2% Default
  2. Weekly Loss Limit                           → 5% Default
  3. Consecutive-Loss-Pause (3 SLs in Folge)    → Pause bis morgen
  4. Revenge-Trade-Cooldown (60min nach SL)     → emotionaler Schutz
  5. Max-Trades-per-Day (6)                     → Overtrading-Schutz
  6. Max-Trades-per-Session (2)                 → pro London/NY-Block
  7. Max-Trades-per-Pair-per-Day (2)            → diversifikations-Pflicht

Aufruf: `gate.can_trade(now, instrument, equity, closed_trades) → (bool, reason)`
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class TradeLimitsConfig:
    """Alle Schwellen konfigurierbar — Defaults aus Risk.md + TradeGate.md."""
    max_trades_per_day: int = 6
    max_trades_per_session: int = 2
    max_trades_per_pair_per_day: int = 2
    daily_loss_limit_pct: float = 2.0    # in % von initial_capital
    weekly_loss_limit_pct: float = 5.0
    consecutive_loss_pause: int = 3      # nach N SLs in Folge: Pause bis morgen
    revenge_cooldown_minutes: int = 60   # nach jedem SL: keine Trades für N Min
    initial_capital: float = 10_000.0


def _session_key(ts: datetime) -> str:
    """Welche Session ist der Timestamp? london | ny | offhours."""
    # ts kommt UTC; London = 07:00-10:00 UTC, NY = 13:30-16:00 UTC (CEST-Sommerzeit)
    h, m = ts.hour, ts.minute
    if 7 <= h < 10:
        return "london"
    if (h == 13 and m >= 30) or (14 <= h < 16):
        return "ny"
    return "offhours"


def can_trade(
    now: datetime,
    instrument: str,
    equity: float,
    closed_trades: list,  # list[ClosedTrade] — duck-typed (.close_time, .pnl_abs, .exit_reason)
    cfg: TradeLimitsConfig | None = None,
) -> tuple[bool, str | None]:
    """Returns (allowed, reason_if_blocked).

    `closed_trades` muss die volle Historie des Instruments sein (für Tages/Wochen-Lookback).
    """
    if cfg is None:
        cfg = TradeLimitsConfig()
    today = now.date()
    week_start = today - timedelta(days=today.weekday())

    # ── Lookback-Schnitte ──────────────────────────────────────────────
    todays = [t for t in closed_trades if t.close_time.date() == today]
    weeks = [t for t in closed_trades if t.close_time.date() >= week_start]
    sl_today = [t for t in todays if t.exit_reason == "sl"]

    # ── 1. Daily Loss Limit ─────────────────────────────────────────────
    pnl_today = sum(t.pnl_abs for t in todays)
    daily_loss_eur = cfg.initial_capital * cfg.daily_loss_limit_pct / 100.0
    if pnl_today <= -daily_loss_eur:
        return False, f"daily_loss_limit:{pnl_today:+.2f}€ ≤ -{daily_loss_eur:.0f}€"

    # ── 2. Weekly Loss Limit ────────────────────────────────────────────
    pnl_week = sum(t.pnl_abs for t in weeks)
    weekly_loss_eur = cfg.initial_capital * cfg.weekly_loss_limit_pct / 100.0
    if pnl_week <= -weekly_loss_eur:
        return False, f"weekly_loss_limit:{pnl_week:+.2f}€ ≤ -{weekly_loss_eur:.0f}€"

    # ── 3. Consecutive Loss Pause ──────────────────────────────────────
    # Letzte N closed_trades alle SL → Pause bis morgen
    if len(closed_trades) >= cfg.consecutive_loss_pause:
        last_n = closed_trades[-cfg.consecutive_loss_pause:]
        if all(t.exit_reason == "sl" for t in last_n):
            # Nur wenn letzter SL HEUTE war (morgen ist Reset)
            if last_n[-1].close_time.date() == today:
                return False, f"consecutive_loss_pause:{cfg.consecutive_loss_pause}_SLs_in_Folge"

    # ── 4. Revenge-Cooldown ────────────────────────────────────────────
    if sl_today:
        last_sl = max(sl_today, key=lambda t: t.close_time)
        cooldown_end = last_sl.close_time + timedelta(minutes=cfg.revenge_cooldown_minutes)
        if now < cooldown_end:
            remaining = (cooldown_end - now).total_seconds() / 60
            return False, f"revenge_cooldown:{remaining:.0f}min_remaining"

    # ── 5. Max-Trades-per-Day ──────────────────────────────────────────
    if len(todays) >= cfg.max_trades_per_day:
        return False, f"max_trades_per_day:{len(todays)}≥{cfg.max_trades_per_day}"

    # ── 6. Max-Trades-per-Session ──────────────────────────────────────
    session = _session_key(now)
    if session != "offhours":
        session_trades = [t for t in todays if _session_key(t.close_time) == session]
        if len(session_trades) >= cfg.max_trades_per_session:
            return False, f"max_trades_per_session:{session}:{len(session_trades)}"

    # ── 7. Max-Trades-per-Pair-per-Day ─────────────────────────────────
    pair_today = [t for t in todays if t.instrument == instrument]
    if len(pair_today) >= cfg.max_trades_per_pair_per_day:
        return False, f"max_trades_per_pair_per_day:{instrument}:{len(pair_today)}"

    return True, None
