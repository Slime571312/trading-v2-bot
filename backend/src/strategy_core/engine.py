"""Engine-Orchestrator — bündelt die 6 Playbook-Schritte zu einer Funktion.

Spec-Quelle: ~/Desktop/tradingbot/Trading/Bot/Playbook.md

Eingabe: ein Dictionary `bars: dict[tf -> DataFrame]` mit OHLCV pro TF (mindestens
1d, einer der HTFs aus 1h/30m/15m, einer der LTFs aus 5m/1m).

Ausgabe: ein `Signal` oder `None` — Backtest und Live-Bot teilen exakt diese
Funktion. Stateless. Determinstisch.
"""
from __future__ import annotations

import logging

import pandas as pd

from . import bias as bias_mod
from . import confidence as confidence_mod
from . import conviction as conviction_mod
from . import crypto_sentiment as crypto_mod
from . import ensemble as ensemble_mod
from . import liquidity as liquidity_mod
from . import news as news_mod
from . import rr as rr_mod
from . import sessions as sessions_mod
from . import structure as structure_mod
from . import sweep as sweep_mod
from . import smt as smt_mod
from . import volatility as vol_mod
from . import zones as zones_mod
from ._types import Signal, SignalVariant
from .pivots import find_pivots
from .rr import compute_multi_tp_targets

log = logging.getLogger(__name__)


_TF_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


def _drop_inprogress_bar(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Wirft die letzte Bar weg, wenn sie noch im aktuellen TF-Intervall liegt.

    Live-Datenfeeds liefern die noch laufende Bar mit (open=Close-vor-x-Sek,
    high/low/close = bisheriger Verlauf). Die Engine würde sonst Indikatoren und
    Setups auf einer Bar berechnen deren Schlusskurs in echt noch unbekannt ist —
    klassischer Lookahead-Bias.
    """
    if df.empty:
        return df
    tf_s = _TF_SECONDS.get(tf, 60)
    last_ts = df.index[-1]
    if not isinstance(last_ts, pd.Timestamp):
        return df
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize("UTC")
    if (pd.Timestamp.now(tz="UTC") - last_ts).total_seconds() < tf_s:
        return df.iloc[:-1]
    return df


def evaluate(
    instrument: str,
    bars: dict[str, pd.DataFrame],
    rr_threshold: float = 2.0,
    sweep_lookback_bars: int = 10,
    now_ts: pd.Timestamp | None = None,
    skip_external_filters: bool = False,
    ref_bars: dict[str, pd.DataFrame] | None = None,
    stage_tracker: list[str] | None = None,
) -> Signal | None:
    """Vollständige Playbook-Auswertung. Liefert ein Signal oder None.

    `bars`-Keys erwartet: '1d' + mindestens einer von {'1h','30m','15m'} +
    mindestens einer von {'5m','1m'}.

    `now_ts` (optional): wird für Session-/News-/OPEX-/Silver-Bullet-Filter
    benutzt. Live: None lassen → echte Wall-Clock-Zeit. Backtest: die Bar-Zeit
    des aktuellen Steps übergeben — sonst werden historische Setups gegen die
    *heutige* Session/News-Lage gefiltert (klassischer Lookahead).

    `skip_external_filters` (für Backtests): überspringt News-Window-Check
    und BTC-Funding-Kontradiktion. Hintergrund: News/Funding-Modul liefert nur
    *aktuelle* Daten — bei historischen Backtests würde das die heutige Lage
    auf historische Trades anwenden (Lookahead). Im Live-Mode False lassen.
    """
    # Setup-Stage-Tracker (Vault/Bot/Setup-State-Machine.md): höchster
    # erreichter Stage-Name wird vom Caller (loop.py) als FSM-Zustand
    # in der Tick-Log + im Dashboard angezeigt.
    def _stage(name: str) -> None:
        if stage_tracker is not None:
            stage_tracker.append(name)

    _stage("init")

    # Lookahead-Schutz: laufende (unfertige) Bar pro TF rauswerfen.
    bars = {tf: _drop_inprogress_bar(df, tf) for tf, df in bars.items()}

    if "1d" not in bars or len(bars["1d"]) < 10:
        return None
    _stage("daily_bars_ok")

    # ── Schritt 1: Daily-Bias + Multi-TF-Confluence ──────────────────────
    bias = bias_mod.compute_bias(bars["1d"])
    if bias.direction == "neutral":
        log.debug("evaluate %s: neutral bias → no signal", instrument)
        return None
    _stage("bias_set")

    # H4-Confluence (Bot/Bias-Detection.md): kein neuer API-Call, resample 1h → 4h
    _h4_bias_dir = bias_mod.compute_h4_bias(bars["1h"]) if "1h" in bars else "neutral"
    _mtf = bias_mod.multi_tf_confluence(bias.direction, _h4_bias_dir)
    log.debug("evaluate %s: daily=%s h4=%s confluence=%s",
              instrument, bias.direction, _h4_bias_dir, _mtf["label"])

    # ── Schritt 2+3: HTF-LQ + Sweep — höchster TF gewinnt ────────────────
    htf_order = ["1h", "30m", "15m"]
    htf_used: str | None = None
    detected_sweep = None
    df_htf: pd.DataFrame | None = None

    # Session-Filter: Live = Wall-Clock, Backtest = übergebener Bar-Timestamp.
    # `now_ts=None` → echtzeit (Live). Backtest reicht die historische Bar-Zeit
    # rein, damit der Filter gegen die *damals* gültige Session prüft.
    if now_ts is None:
        _now_utc = pd.Timestamp.now(tz="UTC")
    else:
        _now_utc = now_ts if now_ts.tzinfo else now_ts.tz_localize("UTC")
    if not sessions_mod.is_in_session(_now_utc, instrument):
        log.debug("evaluate %s: outside session at %s", instrument, _now_utc.isoformat())
        return None
    _stage("in_session")

    # News-Filter (Bot/News-Integration.md): kein Entry ±30/+15 Min um High-Impact-Events.
    # Im Backtest übersprungen — Modul liefert nur live-aktuelle Lage, nicht historisch.
    if not skip_external_filters:
        in_news, event_title = news_mod.is_news_window(instrument, now=_now_utc.to_pydatetime())
        if in_news:
            log.info("evaluate %s: News-Window aktiv (%s) — skip", instrument, event_title)
            return None

    # OPEX-Close-Window (Bot/OPEX-Calendar.md): letzte 30 Min am OPEX-Tag = Multi-Mrd Pin-Flows
    if instrument not in sessions_mod.CRYPTO_INSTRUMENTS and sessions_mod.is_opex_close_window(_now_utc):
        log.info("evaluate %s: OPEX-Close-Window — kein Entry", instrument)
        return None

    # Silver-Bullet (Bot/Silver-Bullet.md): strenger MIN_RR (2.0) außerhalb der NY-AM-Zone.
    # Innerhalb der SB-Fenster bleibt MIN_RR = 1.5 (Standard) → Window gibt Edge, niedrigere Hürde.
    # Für Indizes greift SB-NY-AM (15:00-16:00 UTC), für BTC alle drei Fenster (24/7).
    if instrument in sessions_mod.CRYPTO_INSTRUMENTS:
        _sb_active = sessions_mod.is_any_silver_bullet_window(_now_utc)
    else:
        _sb_active = sessions_mod.is_silver_bullet_window(_now_utc, "ny_am") or \
                     sessions_mod.is_silver_bullet_window(_now_utc, "london_open")
    _effective_min_rr = rr_mod.MIN_RR if _sb_active else 2.0

    for tf in htf_order:
        df = bars.get(tf)
        if df is None or len(df) < 20:
            continue
        levels = liquidity_mod.liquidity_levels(df, bias.direction)
        sweeps = sweep_mod.detect_sweeps(df, levels, tf=tf, lookback=sweep_lookback_bars)
        if sweeps:
            detected_sweep = sweeps[-1]  # jüngster
            htf_used = tf
            df_htf = df
            break

    if detected_sweep is None or htf_used is None or df_htf is None:
        return None
    _stage("sweep_detected")

    # ── Schritt 4: LTF-BOS in Bias-Richtung ──────────────────────────────
    ltf_order = ["5m", "1m"]
    bos = None
    ltf_used: str | None = None
    df_ltf: pd.DataFrame | None = None
    sweep_ltf_idx: int = -1

    for tf in ltf_order:
        df = bars.get(tf)
        if df is None or len(df) < 10:
            continue
        # Finde LTF-Bar der den HTF-Sweep enthält bzw. direkt danach kommt
        ltf_after = df[df.index >= detected_sweep.time]
        if len(ltf_after) < 3:
            continue
        first_after = ltf_after.index[0]
        idx = df.index.get_loc(first_after)
        # idx kann Slice sein bei Duplikaten — wir nehmen den ersten Treffer
        if isinstance(idx, slice):
            idx = idx.start
        # Vault (CHoCH.md): CHoCH ist der frühere, präzisere LTF-Trigger nach HTF-Sweep.
        # BOS als Fallback wenn CHoCH nichts liefert (mehr Bestätigung, späteren Entry).
        candidate = structure_mod.find_choch_after(df, bias.direction, int(idx))
        if candidate is None:
            candidate = structure_mod.find_bos_after(df, bias.direction, int(idx))
        if candidate is not None:
            bos = candidate
            ltf_used = tf
            df_ltf = df
            sweep_ltf_idx = int(idx)
            break

    if bos is None or ltf_used is None or df_ltf is None:
        return None
    _stage("bos_confirmed")

    # ── Schritt 5: RR-Check ──────────────────────────────────────────────
    entry_primary = float(df_ltf["close"].iloc[bos.bar_idx])
    # SL mit ATR-Buffer (per-Instrument kalibriert — SL-Placement.md)
    sl = rr_mod.compute_sl(bias.direction, detected_sweep, df_htf, instrument=instrument)
    htf_pivots = find_pivots(df_htf)
    tp = rr_mod.find_tp_target(bias.direction, entry_primary, htf_pivots)
    if tp is None:
        return None

    rr_primary = rr_mod.rr_ratio(bias.direction, entry_primary, sl, tp)
    variant: SignalVariant | None = None
    entry = entry_primary
    rr = rr_primary
    ob = None
    fvg = None
    eq_data = None

    if rr_primary >= rr_threshold:
        variant = "primary"
    else:
        # ── Schritt 6: Retracement-Suche — OB > FVG, mit OTE-Confluence-Check ──
        # Dealing-Range für OTE: Impulse-Leg von Sweep-Extrem zu BOS-Extrem
        if bias.direction == "long":
            _dr_low = float(df_htf["low"].iloc[detected_sweep.bar_idx])
            _dr_high = bos.body_extreme
        else:
            _dr_low = bos.body_extreme
            _dr_high = float(df_htf["high"].iloc[detected_sweep.bar_idx])
        _ote_bias = 1 if bias.direction == "long" else -1
        _ote_zone = (
            rr_mod.calculate_ote_zone(_dr_low, _dr_high, _ote_bias)
            if _dr_low < _dr_high
            else None
        )

        # OB auf HTF suchen (wo der Sweep passierte, dort sitzt der echte OB)
        ob = zones_mod.find_order_block(df_htf, detected_sweep, bos)
        if ob is not None:
            entry = (ob.upper + ob.lower) / 2.0
            rr = rr_mod.rr_ratio(bias.direction, entry, sl, tp)
            eq_data = zones_mod.compute_equilibrium(detected_sweep, bos, df_htf)
            in_eq = zones_mod.in_eq_zone(entry, eq_data, bias.direction)
            # OTE ∩ OB = höchste Confluence (Rang 1 in Signal-Hierarchie)
            if _ote_zone is not None and rr_mod.ote_confluence_entry(
                entry, _ote_zone, ob.lower, ob.upper
            ):
                variant = "ote_ob_entry"
            elif in_eq:
                variant = "ultimate"
            else:
                variant = "ob_retest"
        else:
            # FVG auf LTF zwischen Sweep und BOS
            fvgs = zones_mod.find_fvgs(
                df_ltf, sweep_ltf_idx, bos.bar_idx, bias.direction,
            )
            if fvgs:
                fvg = fvgs[-1]  # neuester FVG zwischen Sweep und BOS
                entry = (fvg.upper + fvg.lower) / 2.0
                rr = rr_mod.rr_ratio(bias.direction, entry, sl, tp)
                eq_data = zones_mod.compute_equilibrium(detected_sweep, bos, df_htf)
                in_eq = zones_mod.in_eq_zone(entry, eq_data, bias.direction)
                variant = "ultimate" if in_eq else "fvg_retest"
            else:
                return None  # weder OB noch FVG → Setup verworfen

    # Hard-Floor MIN_RR (TP-Strategy.md + Silver-Bullet.md):
    # in SB-Fenster: 1.5 (Standard). Außerhalb: 2.0 (strenger, weil Edge fehlt).
    if variant is None or rr < _effective_min_rr:
        log.debug("evaluate %s: RR %.2f < effective_MIN_RR %.2f (SB_active=%s) — skip",
                  instrument, rr, _effective_min_rr, _sb_active)
        return None
    _stage("rr_ok")

    # ── Conviction-Score (Bot/Conviction-Score.md) ────────────────────────
    # 10-Punkte Score + ADX-Regime → A/B/skip mit Size-Multiplikator
    htf_tfs = {"1h", "30m", "15m"}
    _ctx = conviction_mod.ConvictionContext(
        instrument=instrument,
        bias_clear=(bias.direction != "neutral"),
        sweep_is_htf=(htf_used in htf_tfs),
        bos_confirmed=True,   # ohne BOS wäre Code hier nie erreicht
        active_zone=(ob is not None or fvg is not None),
        is_kill_zone=_sb_active,
        in_ote_zone=(variant == "ote_ob_entry"),
        weekday_weight=conviction_mod.weekday_entry_weight(_now_utc),
    )
    # BTC-only: Funding-Rate als Kontradiktion (Bot/Crypto-Sentiment.md).
    # Im Backtest übersprungen — Funding-Modul liefert nur live-aktuelle Rate.
    if instrument == "BTC" and not skip_external_filters:
        _ctx.funding_contra = crypto_mod.is_funding_contra(bias.direction)
        if _ctx.funding_contra:
            log.info("evaluate BTC: Funding-Kontradiktion gegen %s — penalty -1",
                     bias.direction)

    # SMT-Divergenz (Vault/Bot/Conviction-Score.md): wenn Reference-Asset-Bars
    # vorhanden sind, vergleiche die jüngsten Extremes mit dem Main-Instrument.
    # Loop.py lädt das automatisch für Main-Instruments mit gemapptem Reference.
    if ref_bars is not None and htf_used in ref_bars:
        smt_result = smt_mod.detect_smt_divergence(
            df_htf, ref_bars[htf_used], bias.direction, lookback=sweep_lookback_bars,
        )
        if smt_result == "confirms":
            _ctx.smt_confirms = True
            log.debug("evaluate %s: SMT confirms (%s vs %s)",
                      instrument, htf_used, smt_mod.get_reference_instrument(instrument))
        elif smt_result == "diverges":
            _ctx.smt_diverges = True
            log.info("evaluate %s: SMT diverges (%s vs %s) — Kontradiktion",
                     instrument, htf_used, smt_mod.get_reference_instrument(instrument))

    _score, _breakdown = conviction_mod.compute_conviction_score(_ctx)
    _adx = conviction_mod.compute_adx(bars.get("1h", bars["1d"]), period=14)
    _contradictions = int(_ctx.smt_diverges) + int(_ctx.funding_contra)
    _grade, _size_mult, _regime = conviction_mod.classify_setup(_score, _adx, _contradictions)
    # Multi-TF-Confluence reduziert size_mult zusätzlich (aligned 1.0×, retrace 0.5×)
    _size_mult *= _mtf["size_factor"]
    if _grade == "skip" or _size_mult <= 0:
        log.info("evaluate %s: skip (score=%d, regime=%s, adx=%.1f, mtf=%s)",
                 instrument, _score, _regime, _adx, _mtf["label"])
        return None
    # BTC-Hardening: kein B-Setup — Crypto-Spreads (~0.25%) fressen den Edge bei
    # mittlerer Conviction. Nur A-Setups (Score ≥ 7-9 je Regime) für BTC.
    if instrument == "BTC" and _grade != "A":
        log.info("evaluate BTC: B-Setup gefiltert (score=%d grade=%s — BTC nur A)",
                 _score, _grade)
        return None
    _stage("entry_ready")

    # ── Ensemble-Signal-Voting (Bot/Ensemble-Entry-Signals.md) ────────────
    # 3 independent patterns: Order-Block, FVG-Mitigation, Equilibrium+RSI.
    # ≥2 agree → STRONG (A-Tier). 1 agrees → WEAK (B-Tier). 0 → SKIP.
    _ensemble = ensemble_mod.ensemble_evaluate(
        {
            "1h": bars.get("1h"),
            "5m": bars.get("5m"),
            "1d": bars.get("1d"),
        },
        side=bias.direction,
    )
    if not _ensemble.has_signal:
        log.info(
            "evaluate %s: ensemble voting rejected (no voter agreement, "
            "variant=%s)",
            instrument, variant
        )
        return None
    _stage("ensemble_ok")

    # Ensemble confidence multiplier: STRONG=1.0, WEAK=0.7
    _ensemble_confidence_mult = 1.0 if _ensemble.ensemble_grade == "STRONG" else 0.7
    log.debug(
        "evaluate %s: ensemble=%s voters=%s confidence_mult=%.2f",
        instrument, _ensemble.ensemble_grade, _ensemble.voters,
        _ensemble_confidence_mult,
    )

    # ── Multi-TP-Staffelung (Vault/Bot/TP-Staffelung.md + ERL-IRL.md) ─────
    # ERL/IRL-Modell: TP1 = nächste FVG-Fill (IRL) zwischen Entry und erstem
    # Pivot, TP2 = dieser Pivot (ERL = Dealing-Range-Boundary), TP3 = entferntester
    # HTF-Pivot (= IPDA-Draw). Wenn keine FVG im Pfad → Fallback auf reine
    # Pivot-Heuristik (nächster/mittel/entferntester).
    # A-Setup mit HTF-Draw + Conviction ≥ 8 + trending Regime → 40/40/20 (Runner aktiv).
    # Sonst: 50/50/0 (B-Setup, kein Runner — bei tp2 wird voll geschlossen).
    _htf_fvgs_in_path: list = []
    if len(df_htf) >= 3:
        # IRL = gegen-direktion FVG (Bear-FVG für Long-Trade, Bull-FVG für Short).
        # Suche im gesamten HTF-Window — FVGs bleiben gültig solange ungefüllt.
        _opp_dir = "short" if bias.direction == "long" else "long"
        _htf_fvgs_in_path = zones_mod.find_fvgs(df_htf, 1, len(df_htf) - 2, _opp_dir)
    _sl_dist = abs(float(entry) - float(sl))
    _tp1, _tp2, _tp3 = compute_multi_tp_targets(
        bias.direction, float(entry), htf_pivots,
        irl_fvgs=_htf_fvgs_in_path,
        sl_distance=_sl_dist,
    )

    # Volatility-Regime aus Daily-ATR%. Low-Vol → kein Runner (Markt reicht
    # nicht 3R+); für Position-Sizing wird der kontinuierliche Faktor genutzt.
    _vol_regime, _atr_pct = vol_mod.compute_volatility_regime(bars["1d"], instrument)
    # Kontinuierlicher Vol-Faktor (Vault: Volatility-Based-Position-Sizing.md).
    # Skaliert invers zur Volatilität — bei aktueller ATR% > Referenz-ATR% → factor < 1.
    _vol_size_factor, _cur_atr, _ref_atr = vol_mod.compute_continuous_vol_factor(
        bars["1d"], instrument,
    )

    _use_runner = (
        _grade == "A"
        and _score >= 8
        and _tp2 is not None
        and _tp3 is not None
        and _tp2 != _tp3
        and _regime != "ranging"
        and _vol_regime != "low"          # Low-Vol: kein Runner — 3R+ wird selten erreicht
    )
    if _use_runner:
        _pct1, _pct2, _pct3 = 0.4, 0.4, 0.2
    else:
        _pct1, _pct2, _pct3 = 0.5, 0.5, 0.0

    # Vol-Sizing: Position skaliert mit Vol-Faktor (loggt nur wenn signifikant abweichend)
    _size_mult *= _vol_size_factor
    if abs(_vol_size_factor - 1.0) > 0.05:
        log.info(
            "evaluate %s: vol_regime=%s atr_pct=%.3f%% (ref %.3f%%) vol_factor=%.2f",
            instrument, _vol_regime, _cur_atr * 100, _ref_atr * 100, _vol_size_factor,
        )

    # Confidence-Score (Vault/Bot/Signal-Confidence-Scoring.md) — 4 Faktoren,
    # liefert (0..1, breakdown, tier-size_mult). Filtert bei < 50 % und moduliert
    # ansonsten die Size feiner als der diskrete A/B-Faktor. Wir setzen ihn
    # MULTIPLIKATIV auf _size_mult, aber sanft (0.7 / 1.0 / 1.25) damit nicht
    # 4 Filter zusammen die Size auf 0 drücken.
    _conf_ctx = confidence_mod.ConfidenceContext(
        mtf_size_factor=_mtf["size_factor"],
        rr=float(rr),
        conviction_score=_score,
        adx_regime=_regime,
        atr_regime=_vol_regime,
    )
    _confidence, _conf_breakdown, _conf_size_mult = confidence_mod.compute_confidence(_conf_ctx)
    # Apply ensemble confidence multiplier (STRONG=1.0, WEAK=0.7)
    _confidence *= _ensemble_confidence_mult
    _conf_size_mult *= _ensemble_confidence_mult
    if _conf_size_mult == 0.0:
        log.info(
            "evaluate %s: confidence %.0f%% < 50%% — skip (mtf=%.2f rr=%.2f conv=%.2f reg=%.2f ens=%.2f)",
            instrument, _confidence * 100,
            _conf_breakdown["mtf"], _conf_breakdown["rr"],
            _conf_breakdown["conviction"], _conf_breakdown["regime"],
            _ensemble_confidence_mult,
        )
        return None
    _size_mult *= _conf_size_mult
    if abs(_conf_size_mult - 1.0) > 0.05:
        log.info(
            "evaluate %s: confidence %.0f%% tier=%s size_mult=%.2f",
            instrument, _confidence * 100,
            confidence_mod.size_tier_label(_confidence), _conf_size_mult,
        )

    # Trade-Journal-Snapshot — Kontext bei Entry für spätere Attribution.
    _entry_ts = df_ltf.index[-1]
    _session_label = sessions_mod.session_label(_entry_ts, instrument)
    _weekday = int(_entry_ts.weekday())

    # Defense-in-Depth: Lunch-Gap (12:00–15:30 Berlin) nur für Instrumente
    # blockieren die dort historisch verlieren. Live-Analyse 2026-06-24:
    # - SP500: 1 closed-Trade -1.03R → blockieren
    # - DE40 + NASDAQ in "closed" waren mehrheitlich profitabel (+147€ netto über 5 Trades)
    #   → erlauben, mehr Daten sammeln bevor wir final entscheiden
    _CLOSED_BLOCKLIST = {"SP500"}
    if instrument in _CLOSED_BLOCKLIST and _session_label == "closed":
        log.info(
            "evaluate %s: skip — closed-Session in Blocklist (Bar-Zeit %s)",
            instrument, _entry_ts.isoformat(),
        )
        return None

    return Signal(
        time=_entry_ts,
        instrument=instrument,
        side=bias.direction,
        entry=float(entry),
        sl=float(sl),
        tp=float(tp),
        rr=float(rr),
        variant=variant,
        htf_used=htf_used,
        ltf_used=ltf_used,
        bias=bias,
        sweep=detected_sweep,
        structure_break=bos,
        ob=ob,
        fvg=fvg,
        equilibrium=eq_data,
        conviction_score=_score,
        conviction_grade=_grade,
        size_multiplier=_size_mult,
        adx_regime=_regime,
        tp1=_tp1,
        tp2=_tp2,
        pct1=_pct1,
        pct2=_pct2,
        pct3=_pct3,
        # Tags
        session_label=_session_label,
        atr_regime=_vol_regime,
        atr_pct=float(_atr_pct),
        vol_size_factor=float(_vol_size_factor),
        weekday=_weekday,
        confidence=float(_confidence),
        confidence_tier=confidence_mod.size_tier_label(_confidence),
        # Ensemble voting metadata
        ensemble_voters=_ensemble.voters,
        ensemble_grade=_ensemble.ensemble_grade,
    )
