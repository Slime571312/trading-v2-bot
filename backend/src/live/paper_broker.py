"""Paper-Broker — öffnet/schließt virtuelle Positionen, identisches Risk-Modell
wie der Backtest-Runner.

- Slippage via `backtest/slippage.py` (1 Source of Truth)
- Position-Size: `equity × risk_pct / sl_distance` → SL-Hit = exakt −1R
- Intrabar SL/TP-Check: pessimistisch (SL hat Vorrang bei Ambiguität)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd

from src.backtest.slippage import apply_slippage_entry, apply_slippage_exit

from .state import ClosedTrade, ExitReason, OpenTrade, _new_trade_id, _now

if TYPE_CHECKING:
    from src.strategy_core._types import Signal

log = logging.getLogger(__name__)


# Min-SL-Distance pro Instrument als Prozent des Entry-Preises.
# Hintergrund (Vault/Bot/TP-Strategy.md): BTC-CFDs auf Capital haben einen
# Round-Trip-Spread ~0.25-0.5% — bei einem SL von 0.1% des Preises sind die
# Fees > 30% der R-Distanz, der EV kippt ins Negative. Wir blocken zu enge
# Setups direkt im Broker, bevor ein Trade aufgemacht wird.
MIN_SL_PCT: dict[str, float] = {
    "BTC":    0.005,   # ≥0.5% Entry — größter Spread/Slippage-Anteil
    "DE40":   0.0008,
    "NASDAQ": 0.0008,
    "SP500":  0.0008,
}

# Min-Position-Size pro Instrument — entspricht Capital.com Mindest-Lot.
# Wenn risk_pct × OPEX × Conviction × MTF die Position unter diesen Wert drückt,
# wird der Trade lieber komplett geskippt als mit unrealistisch kleinem Volumen
# eröffnet (das in Live nicht orderbar wäre).
MIN_SIZE: dict[str, float] = {
    "BTC":    0.01,
    "DE40":   0.5,
    "NASDAQ": 0.1,
    "SP500":  0.1,
}


def open_position(
    signal: "Signal",
    equity: float,
    risk_pct: float,
    current_bar: pd.Series,
) -> OpenTrade | None:
    """Öffne eine Paper-Position aus einem Signal. None wenn SL-Distanz ungültig.

    Multi-TP-Staffelung (Vault/Bot/TP-Staffelung.md):
    - Wenn Signal explizite tp1/tp2 hat → 3-Tier (40/40/20 oder 50/50/0).
    - Wenn Signal nur tp hat → Fallback auf alte 2-Tier-Logik (tp1=halbweg, 50/50).
    """
    if abs(signal.entry - signal.sl) <= 0:
        log.warning("open_position: degeneriertes Signal (entry==sl) für %s — skip",
                    signal.instrument)
        return None

    entry = apply_slippage_entry(
        signal.entry, signal.side,
        float(current_bar.high), float(current_bar.low),
        instrument=signal.instrument, ts=signal.time,
    )
    sl_distance = abs(entry - signal.sl)
    if sl_distance <= 0:
        log.warning("open_position: SL-Distanz 0 für %s — skip", signal.instrument)
        return None

    # Min-SL-Distance: bei BTC ≥ 0.5% Entry-Distanz, sonst frisst Slippage den Edge.
    min_sl_dist = entry * MIN_SL_PCT.get(signal.instrument, 0.0008)
    if sl_distance < min_sl_dist:
        log.info("open_position %s: SL-Distanz %.2f < min %.2f (%.3f%% Entry) — skip",
                 signal.instrument, sl_distance, min_sl_dist,
                 MIN_SL_PCT.get(signal.instrument, 0.0008) * 100)
        return None

    risk_amount = equity * risk_pct
    size = risk_amount / sl_distance

    # Min-Size: bei zu kleinem Lot (Risk-Multiplikatoren-Kombo zerlegt Position)
    # → skip statt mit Mikro-Volumen öffnen, das in Live nicht orderbar wäre.
    min_size = MIN_SIZE.get(signal.instrument, 0.01)
    if size < min_size:
        log.info("open_position %s: size %.4f < min %.4f (effective_risk=%.3f%%) — skip",
                 signal.instrument, size, min_size, risk_pct * 100)
        return None

    # Multi-TP-Levels aus dem Signal — Engine setzt tp1/tp2/pct1/pct2/pct3.
    # Fallback wenn Signal nur das alte `tp` hat: 2-Tier mit tp1=halbweg.
    tp1 = signal.tp1
    tp2 = signal.tp2
    pct1, pct2, pct3 = signal.pct1, signal.pct2, signal.pct3
    if tp1 is None:
        # Legacy / kein Multi-TP aus Engine → halbweg-Heuristik, 50/50
        tp1 = entry + 0.5 * (signal.tp - entry)
        tp2 = None
        pct1, pct2, pct3 = 0.5, 0.5, 0.0

    # open_time aus dem Signal übernehmen (= Bar-Zeit des LTF-BOS) — konsistent
    # zwischen Live und Backtest. Im Live ist signal.time ≈ jetzt (jüngste Bar),
    # im Backtest = die historische Bar. Das macht Trade-Holdtime-Analysen
    # konsistent statt sie zwischen den beiden Modi ~30-60s zu verschieben.
    from datetime import timezone as _tz
    _open_time = signal.time
    if isinstance(_open_time, pd.Timestamp):
        _open_time = (_open_time.to_pydatetime().replace(tzinfo=_tz.utc)
                      if _open_time.tzinfo is None else _open_time.to_pydatetime())

    # GSL-Decision (Vault/Bot/GSL-Manager.md): grobe Holdtime-Schätzung
    # 4 h für Indizes (Average der historischen Trades-Statistik), 6 h für BTC.
    # Live-Bot kann das später dynamisch aus AvgHoldTime ableiten.
    from src.strategy_core import gsl as gsl_mod
    expected_holdtime_h = 6.0 if signal.instrument == "BTC" else 4.0
    chosen_stop = gsl_mod.decide_stop_type(
        instrument=signal.instrument, side=signal.side,
        open_time_utc=_open_time, expected_holdtime_hours=expected_holdtime_h,
    )
    if chosen_stop != "regular":
        log.info("%s GSL-Decision: stop_type=%s (Holdtime≈%.0fh)",
                 signal.instrument, chosen_stop, expected_holdtime_h)

    return OpenTrade(
        id=_new_trade_id(),
        instrument=signal.instrument,
        side=signal.side,
        open_time=_open_time,
        entry=entry,
        sl=signal.sl,
        tp=signal.tp,
        size=size,
        variant=signal.variant or "primary",
        htf_used=signal.htf_used,
        ltf_used=signal.ltf_used,
        rr_at_open=signal.rr,
        tp1=tp1,
        tp2=tp2,
        tp1_hit=False,
        tp2_hit=False,
        pct1=pct1,
        pct2=pct2,
        pct3=pct3,
        original_size=size,
        original_sl=signal.sl,
        stop_type=chosen_stop,
        # Trade-Journal-Snapshot (vom Engine vorbefüllt)
        session_at_open=signal.session_label,
        atr_regime_at_open=signal.atr_regime,
        atr_pct_at_open=signal.atr_pct,
        vol_size_factor_at_open=signal.vol_size_factor,
        weekday_at_open=signal.weekday,
        conviction_score_at_open=signal.conviction_score,
        conviction_grade_at_open=signal.conviction_grade,
        adx_regime_at_open=signal.adx_regime,
        confidence_at_open=signal.confidence,
        confidence_tier_at_open=signal.confidence_tier,
    )


def _hit_tp1(trade: OpenTrade, bar: pd.Series) -> bool:
    """True wenn diese Bar TP1 erreicht hat (und TP1 noch nicht gehittet)."""
    if trade.tp1 is None or trade.tp1_hit:
        return False
    if trade.side == "long":
        return float(bar.high) >= trade.tp1
    return float(bar.low) <= trade.tp1


def _hit_tp2(trade: OpenTrade, bar: pd.Series) -> bool:
    """True wenn diese Bar TP2 erreicht hat (TP1 muss vorher gehittet sein)."""
    if trade.tp2 is None or trade.tp2_hit or not trade.tp1_hit:
        return False
    if trade.side == "long":
        return float(bar.high) >= trade.tp2
    return float(bar.low) <= trade.tp2


def _set_partial_time(trade: OpenTrade, ts) -> None:
    """Schreibt UTC-Timestamp in trade.partial_close_time (idempotent)."""
    import pandas as _pd
    if isinstance(ts, _pd.Timestamp):
        from datetime import timezone as _tz
        trade.partial_close_time = (
            ts.to_pydatetime().replace(tzinfo=_tz.utc)
            if ts.tzinfo is None else ts.to_pydatetime()
        )
    else:
        trade.partial_close_time = ts


def apply_tp1_partial(trade: OpenTrade, bar: pd.Series, ts) -> float:
    """Realisiert pct1 bei TP1 + zieht SL auf Entry (BE-Move).

    Mutiert trade in-place. Gibt realisierten PnL der Teilschließung zurück.
    Size wird auf Basis der ORIGINAL-Size berechnet, damit pct1/pct2/pct3
    konsistent zu 1.0 summieren (egal in welcher Reihenfolge sie ausgelöst werden).
    """
    if trade.tp1 is None or trade.tp1_hit:
        return 0.0

    exit_slipped = apply_slippage_exit(
        trade.tp1, trade.side, float(bar.high), float(bar.low),
        instrument=trade.instrument, ts=ts if isinstance(ts, pd.Timestamp) else None,
    )
    base_size = trade.original_size or trade.size
    closed_size = base_size * trade.pct1
    direction = 1 if trade.side == "long" else -1
    partial_pnl = (exit_slipped - trade.entry) * direction * closed_size

    trade.size = max(0.0, trade.size - closed_size)
    trade.tp1_hit = True
    trade.partial_pnl += partial_pnl
    trade.sl = trade.entry   # Break-Even
    _set_partial_time(trade, ts)
    return partial_pnl


def apply_tp2_partial(trade: OpenTrade, bar: pd.Series, ts) -> float:
    """Realisiert pct2 bei TP2 + zieht SL auf TP1 (Lock-Profit).

    Voraussetzung: tp1_hit==True. Mutiert trade in-place.
    """
    if trade.tp2 is None or trade.tp2_hit or not trade.tp1_hit:
        return 0.0

    exit_slipped = apply_slippage_exit(
        trade.tp2, trade.side, float(bar.high), float(bar.low),
        instrument=trade.instrument, ts=ts if isinstance(ts, pd.Timestamp) else None,
    )
    base_size = trade.original_size or trade.size
    closed_size = base_size * trade.pct2
    direction = 1 if trade.side == "long" else -1
    partial_pnl = (exit_slipped - trade.entry) * direction * closed_size

    trade.size = max(0.0, trade.size - closed_size)
    trade.tp2_hit = True
    trade.partial_pnl += partial_pnl
    # Lock-Profit: SL auf TP1 ziehen (Trail-Start für Runner)
    trade.sl = trade.tp1 if trade.tp1 is not None else trade.entry
    _set_partial_time(trade, ts)
    return partial_pnl


def trail_sl_behind_pivots(
    trade: OpenTrade,
    bars: pd.DataFrame,
    min_buffer_pct: float = 0.0005,
) -> bool:
    """Trail SL hinter den neuesten LTF-Pivot in Trade-Richtung (Vault/Bot/Dynamic-SL.md).

    Nur aktiv NACH TP2-Hit (Lock-Profit-Phase) — vorher übernimmt die feste
    SL-Mechanik (BE nach TP1, TP1-Lock nach TP2).

    Long-Trade: SL = jüngster bestätigter Pivot-LOW unter aktueller Bar - buffer.
                Nur trailen wenn neuer SL > aktueller SL (nie zurückziehen).
    Short-Trade: spiegelbildlich mit Pivot-HIGH.

    `min_buffer_pct`: 5 bps Puffer unter/über dem Pivot damit Wick-Sweeps
    nicht den nachgezogenen SL sofort wieder rauspressen.

    Returns True wenn SL tatsächlich nachgezogen wurde.
    """
    if not trade.tp2_hit:
        return False   # Trailing erst aktiv nach TP2
    if bars.empty or len(bars) < 5:
        return False

    from src.strategy_core.pivots import find_pivots

    # Pivots erst NACH dem letzten partial_close suchen, sonst trailen wir
    # auf Pivots VOR TP2-Hit (was den SL gegen die ursprüngliche TP1-Lock zurückziehen
    # könnte oder über den TP-Pfad hinaus).
    cutoff = trade.partial_close_time or trade.open_time
    bars_after = bars[bars.index > cutoff]
    if len(bars_after) < 3:
        return False

    pivots = find_pivots(bars_after, n_left=1, n_right=1)
    if not pivots:
        return False

    if trade.side == "long":
        lows = [p for p in pivots if p.kind == "low"]
        if not lows:
            return False
        newest = max(lows, key=lambda p: p.bar_idx)
        new_sl = newest.price * (1 - min_buffer_pct)
        if new_sl > trade.sl:
            trade.sl = new_sl
            return True
    else:  # short
        highs = [p for p in pivots if p.kind == "high"]
        if not highs:
            return False
        newest = max(highs, key=lambda p: p.bar_idx)
        new_sl = newest.price * (1 + min_buffer_pct)
        if new_sl < trade.sl:
            trade.sl = new_sl
            return True
    return False


def check_intrabar_exit(
    trade: OpenTrade,
    current_bar: pd.Series,
) -> tuple[float, ExitReason] | None:
    """Returns (exit_price_raw, reason) oder None falls weder SL noch TP getroffen.

    Konservativ: bei Ambiguität (Bar streift SL UND TP) gewinnt SL.
    """
    high = float(current_bar.high)
    low = float(current_bar.low)

    if trade.side == "long":
        if low <= trade.sl:
            return trade.sl, "sl"
        if high >= trade.tp:
            return trade.tp, "tp"
    else:  # short
        if high >= trade.sl:
            return trade.sl, "sl"
        if low <= trade.tp:
            return trade.tp, "tp"
    return None


def scan_exit_over_bars(
    trade: OpenTrade,
    bars: pd.DataFrame,
) -> tuple[pd.Series, float, ExitReason] | None:
    """Scannt eine Reihe von Bars chronologisch auf erstes SL/TP-Hit (ohne TP1-Logik).

    Für Trades die NICHT die Partial-Close-Mechanik nutzen (Legacy / Tests).
    Gibt (hit_bar, exit_price_raw, reason, bar_timestamp) zurück, oder None.
    """
    bars_after = bars[bars.index > trade.open_time]
    for ts, bar in bars_after.iterrows():
        result = check_intrabar_exit(trade, bar)
        if result is not None:
            exit_price, reason = result
            return bar, exit_price, reason, ts
    return None


def scan_with_partial_close(
    trade: OpenTrade,
    bars: pd.DataFrame,
) -> list[dict]:
    """3-Tier-Scan: TP1 + TP2 (NEU) + Final-Close (Vault/Bot/TP-Staffelung.md).

    Reihenfolge pro Bar (konservativ pessimistisch):
      1. SL-Check → wenn SL-getrieben (inkl. BE/Lock-Profit-SL): full_close
      2. TP1-Check → partial_tp1, SL → Entry (BE-Move)
      3. TP2-Check → partial_tp2, SL → TP1 (Lock-Profit)
      4. Final TP (tp3) → full_close für Rest
      → pct3 = 0 (B-Setup ohne Runner): TP2-Hit = full_close für Rest

    Events:
      - {kind: 'partial_tp1', bar, ts, pnl}
      - {kind: 'partial_tp2', bar, ts, pnl}
      - {kind: 'full_close',  bar, ts, exit, reason}

    Mutiert `trade` in-place (size, sl, tp1_hit, tp2_hit, partial_pnl).
    """
    events: list[dict] = []
    cutoff = trade.partial_close_time or trade.open_time
    bars_after = bars[bars.index > cutoff]
    no_runner = trade.pct3 <= 0.0

    for ts, bar in bars_after.iterrows():
        # 1. SL-Check zuerst — wenn diese Bar den (ggf. nachgezogenen) SL reißt,
        # gewinnt SL gegen jeden gleichzeitigen TP-Hit (konservativ).
        sl_first = check_intrabar_exit(trade, bar)
        if sl_first is not None and sl_first[1] == "sl":
            events.append({"kind": "full_close", "bar": bar, "ts": ts,
                           "exit": sl_first[0], "reason": "sl"})
            return events

        # 2. TP1-Check
        if not trade.tp1_hit and trade.tp1 is not None and _hit_tp1(trade, bar):
            partial_pnl = apply_tp1_partial(trade, bar, ts)
            events.append({"kind": "partial_tp1", "bar": bar, "ts": ts, "pnl": partial_pnl})
            # Nach BE-Move: gleiche Bar nochmal gegen den neuen SL=Entry prüfen.
            be_hit = check_intrabar_exit(trade, bar)
            if be_hit is not None:
                events.append({"kind": "full_close", "bar": bar, "ts": ts,
                               "exit": be_hit[0], "reason": be_hit[1]})
                return events

        # 3. TP2-Check (nur wenn TP1 schon gehittet und TP2 existiert)
        if trade.tp1_hit and not trade.tp2_hit and trade.tp2 is not None and _hit_tp2(trade, bar):
            partial_pnl = apply_tp2_partial(trade, bar, ts)
            events.append({"kind": "partial_tp2", "bar": bar, "ts": ts, "pnl": partial_pnl})
            # B-Setup (kein Runner): TP2-Hit = sofort voll schließen.
            if no_runner:
                # Rest-Position zum TP2-Preis schließen — Slippage übernimmt close_position.
                events.append({"kind": "full_close", "bar": bar, "ts": ts,
                               "exit": trade.tp2, "reason": "tp"})
                return events
            # A-Setup mit Runner: Lock-Profit-SL (=tp1) gegen dieselbe Bar prüfen.
            lock_hit = check_intrabar_exit(trade, bar)
            if lock_hit is not None:
                events.append({"kind": "full_close", "bar": bar, "ts": ts,
                               "exit": lock_hit[0], "reason": lock_hit[1]})
                return events

        # 3b. Dynamic-SL-Trailing (Vault/Bot/Dynamic-SL.md): in der Runner-Phase
        # (nach TP2) den SL hinter den jüngsten LTF-Pivot ziehen. Verkleinert
        # Drawdown vom Lock-Profit-Niveau wenn der Trend weiterläuft.
        if trade.tp2_hit and trade.pct3 > 0:
            # Wir füttern nur die Bars BIS einschließlich der aktuellen — Lookahead
            # vermeiden gegenüber der Funktion die find_pivots aufruft.
            bars_so_far = bars[bars.index <= ts]
            trail_sl_behind_pivots(trade, bars_so_far)

        # 4. Final-TP-Check (tp3 = trade.tp) — nur wenn TP1+TP2 schon gelaufen oder
        # kein Multi-TP-Schema (Legacy). Wir nutzen check_intrabar_exit, das den
        # aktuellen trade.tp prüft — was nach TP1/TP2-Hits weiterhin der finale TP ist.
        # Für Legacy-Trades ohne tp1/tp2 ist das einfach der 2-Tier-Final-Close.
        if trade.tp1_hit:
            result = check_intrabar_exit(trade, bar)
            if result is not None:
                events.append({"kind": "full_close", "bar": bar, "ts": ts,
                               "exit": result[0], "reason": result[1]})
                return events
        elif trade.tp1 is None:
            # Legacy: kein TP1 → direkt finaler TP/SL-Check
            result = check_intrabar_exit(trade, bar)
            if result is not None:
                events.append({"kind": "full_close", "bar": bar, "ts": ts,
                               "exit": result[0], "reason": result[1]})
                return events

    return events


def close_position(
    trade: OpenTrade,
    exit_price_raw: float,
    current_bar: pd.Series,
    reason: ExitReason,
    close_time=None,
) -> ClosedTrade:
    """Closed-Trade aus Open-Trade + Exit-Bar. PnL + R-Multiple berechnen.

    `close_time`: optionaler expliziter Schließzeitpunkt (für Catch-up nach
    Offline-Phase). Wenn None, wird _now() verwendet.
    """
    from datetime import datetime
    if close_time is None:
        close_time = _now()
    elif isinstance(close_time, pd.Timestamp):
        from datetime import timezone
        close_time = close_time.to_pydatetime().replace(tzinfo=timezone.utc) if close_time.tzinfo is None else close_time.to_pydatetime()

    exit_slipped = apply_slippage_exit(
        exit_price_raw, trade.side,
        float(current_bar.high), float(current_bar.low),
        instrument=trade.instrument,
        ts=close_time if isinstance(close_time, pd.Timestamp) else None,
    )
    direction = 1 if trade.side == "long" else -1
    # pnl_abs = nur Runner-Portion (equity-Buchung): bei B-Setup mit TP1+TP2-Hit
    # ist trade.size==0 → pnl_abs=0 ist hier korrekt, total_pnl deckt die Wahrheit.
    pnl_abs = (exit_slipped - trade.entry) * direction * trade.size
    pnl_pct = (exit_slipped - trade.entry) / trade.entry * 100.0 * direction
    total_pnl = pnl_abs + trade.partial_pnl

    # R-Multiple: gegen ursprüngliches Risiko (Original-SL × Original-Size), damit
    # BE-getrailter SL oder reduzierte Rest-Size das Ergebnis nicht verfälschen.
    risk_size = trade.original_size if trade.original_size else trade.size
    risk_sl = trade.original_sl if trade.original_sl else trade.sl
    risk_amount = abs(trade.entry - risk_sl) * risk_size
    r_multiple = total_pnl / risk_amount if risk_amount > 0 else 0.0

    return ClosedTrade(
        id=trade.id, instrument=trade.instrument, side=trade.side,
        open_time=trade.open_time, close_time=close_time,
        entry=trade.entry, exit=exit_slipped,
        sl=trade.sl, tp=trade.tp, size=trade.size,
        variant=trade.variant, htf_used=trade.htf_used, ltf_used=trade.ltf_used,
        rr_at_open=trade.rr_at_open,
        pnl_abs=pnl_abs, pnl_pct=pnl_pct, r_multiple=r_multiple,
        exit_reason=reason,
        partial_pnl=trade.partial_pnl, total_pnl=total_pnl,
        tp1=trade.tp1,
        tp2=trade.tp2,
        pct1=trade.pct1,
        pct2=trade.pct2,
        pct3=trade.pct3,
        original_sl=trade.original_sl,
        original_size=trade.original_size,
        session_at_open=trade.session_at_open,
        atr_regime_at_open=trade.atr_regime_at_open,
        atr_pct_at_open=trade.atr_pct_at_open,
        vol_size_factor_at_open=trade.vol_size_factor_at_open,
        weekday_at_open=trade.weekday_at_open,
        conviction_score_at_open=trade.conviction_score_at_open,
        conviction_grade_at_open=trade.conviction_grade_at_open,
        adx_regime_at_open=trade.adx_regime_at_open,
        confidence_at_open=trade.confidence_at_open,
        confidence_tier_at_open=trade.confidence_tier_at_open,
    )


def force_close(
    trade: OpenTrade,
    last_price: float,
    reason: ExitReason = "manual",
) -> ClosedTrade:
    """Synthetischer Exit zum letzten bekannten Preis (z.B. Bot-Stop)."""
    direction = 1 if trade.side == "long" else -1
    pnl_abs = (last_price - trade.entry) * direction * trade.size
    pnl_pct = (last_price - trade.entry) / trade.entry * 100.0 * direction
    total_pnl = pnl_abs + trade.partial_pnl
    risk_size = trade.original_size if trade.original_size else trade.size
    risk_sl = trade.original_sl if trade.original_sl else trade.sl
    risk_amount = abs(trade.entry - risk_sl) * risk_size
    r_multiple = total_pnl / risk_amount if risk_amount > 0 else 0.0
    return ClosedTrade(
        id=trade.id, instrument=trade.instrument, side=trade.side,
        open_time=trade.open_time, close_time=_now(),
        entry=trade.entry, exit=last_price,
        sl=trade.sl, tp=trade.tp, size=trade.size,
        variant=trade.variant, htf_used=trade.htf_used, ltf_used=trade.ltf_used,
        rr_at_open=trade.rr_at_open,
        pnl_abs=pnl_abs, pnl_pct=pnl_pct, r_multiple=r_multiple,
        exit_reason=reason,
        partial_pnl=trade.partial_pnl, total_pnl=total_pnl,
        tp1=trade.tp1,
        tp2=trade.tp2,
        pct1=trade.pct1,
        pct2=trade.pct2,
        pct3=trade.pct3,
        original_sl=trade.original_sl,
        original_size=trade.original_size,
        session_at_open=trade.session_at_open,
        atr_regime_at_open=trade.atr_regime_at_open,
        atr_pct_at_open=trade.atr_pct_at_open,
        vol_size_factor_at_open=trade.vol_size_factor_at_open,
        weekday_at_open=trade.weekday_at_open,
        conviction_score_at_open=trade.conviction_score_at_open,
        conviction_grade_at_open=trade.conviction_grade_at_open,
        adx_regime_at_open=trade.adx_regime_at_open,
        confidence_at_open=trade.confidence_at_open,
        confidence_tier_at_open=trade.confidence_tier_at_open,
    )
