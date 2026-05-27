"""FastAPI-Entry — alle HTTP- und WebSocket-Routen.

Etappe 0: nur /health und /config.
Spätere Etappen ergänzen /backtest, /bot, /chat, /trades, /tuner, /ws.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel, Field

from src.api import ws as ws_hub
from src.backtest import run_backtest
from src.backtest.report import REPORT_DIR, render_html
from src.backtest.walkforward import WalkForwardResult, run_walk_forward
from src.brokers.capital_com import CapitalAPIError, CapitalAuthError
from src.config import EPIC_MAP, RESOLUTION_MAP, config
from src.data.fetcher import load_candles
from src.live import get_orchestrator
from src.live.orchestrator import shutdown_orchestrator
from src.strategy_core import evaluate as evaluate_signal
from src.strategy_core.bias import compute_bias
from src.strategy_core.liquidity import liquidity_levels
from src.strategy_core.pivots import find_pivots
from src.strategy_core.sessions import is_in_session, session_label
from src.strategy_core.structure import find_bos_after
from src.strategy_core.sweep import detect_sweeps

logging.basicConfig(level=config.log_level.upper())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI-Lifecycle. Lädt persistierten Bot-State beim Start, stoppt sauber."""
    orch = get_orchestrator()
    log = logging.getLogger("trading-v2")
    log.info(
        "Bot-Orchestrator initialisiert: open_trades=%d, closed_trades=%d, equity=%.2f",
        len(orch.state.open_trades),
        len(orch.state.closed_trades),
        orch.state.equity,
    )
    yield
    await shutdown_orchestrator()
    log.info("Backend-Shutdown — Bot gestoppt, State persistiert")


app = FastAPI(
    title="trading-v2 Backend",
    version="0.1.0",
    description="ICT Multi-TF Bot + Backtest + Chat — Bot v2 Greenfield",
    lifespan=lifespan,
)

# Frontend (Next.js, Etappe 4) darf cross-origin zugreifen
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Backtest-HTML-Reports unter /reports/<datei.html> ausliefern
app.mount("/reports", StaticFiles(directory=str(REPORT_DIR), html=True), name="reports")


# ─── Schemas ─────────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp_utc: str
    bot_running: bool
    broker_connected: bool


class ConfigResponse(BaseModel):
    instruments: list[str]
    htf_liquidity: list[str]
    ltf_trigger: list[str]
    bias_source_tf: str
    rr_threshold_direct: float
    risk_pct_per_trade: float
    max_trades_per_day: int
    sessions: dict[str, str]
    capital_demo: bool
    broker_configured: bool
    chat_configured: bool


class Candle(BaseModel):
    time: str  # ISO 8601 UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandlesResponse(BaseModel):
    instrument: str
    epic: str
    tf: str
    resolution: str
    count: int
    candles: list[Candle]


# Literal-Listen aus den Mapping-Dicts ableiten, damit FastAPI sie als Enum validiert
InstrumentName = Literal["DE40", "NASDAQ", "SP500", "BTC"]
Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]


class SignalResponse(BaseModel):
    """Signal aus dem Strategy-Core. Bei `side='none'` ist kein Setup aktiv."""

    instrument: str
    side: Literal["long", "short", "none"]
    bias_direction: Literal["long", "short", "neutral"]
    bias_bos_time: str | None = None
    bias_bos_level: float | None = None
    variant: Literal["primary", "ob_retest", "fvg_retest", "ultimate"] | None = None
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    rr: float | None = None
    htf_used: str | None = None
    ltf_used: str | None = None
    sweep_time: str | None = None
    sweep_level: float | None = None
    sweep_direction: Literal["bsl", "ssl"] | None = None
    bos_time: str | None = None
    bos_level: float | None = None
    eq_level: float | None = None
    has_ob: bool = False
    has_fvg: bool = False
    reason: str


# ─── Routes ──────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Smoke-Test-Endpoint. Liefert Backend-Status + Verbindungs-Indikatoren."""
    orch = get_orchestrator()
    return HealthResponse(
        status="ok",
        version="0.2.0",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        bot_running=orch.is_running(),
        broker_connected=config.broker_configured,
    )


@app.get("/config", response_model=ConfigResponse)
def get_config() -> ConfigResponse:
    """Aktuelle Playbook-Konfiguration. Read-only in Etappe 0 — schreibbar später."""
    return ConfigResponse(
        instruments=config.instruments,
        htf_liquidity=config.htf_liquidity,
        ltf_trigger=config.ltf_trigger,
        bias_source_tf=config.bias_source_tf,
        rr_threshold_direct=config.rr_threshold_direct,
        risk_pct_per_trade=config.risk_pct_per_trade,
        max_trades_per_day=config.max_trades_per_day,
        sessions={
            "london_open": f"{config.london_open_start}–{config.london_open_end}",
            "ny_open": f"{config.ny_open_start}–{config.ny_open_end}",
            "crypto_247": str(config.crypto_247),
        },
        capital_demo=config.is_demo,
        broker_configured=config.broker_configured,
        chat_configured=bool(config.anthropic_api_key),
    )


@app.get("/data/{instrument}", response_model=CandlesResponse)
async def get_candles(
    instrument: InstrumentName,
    tf: Timeframe = Query(default="1h", description="Bar-Größe"),
    bars: int = Query(default=200, ge=10, le=1000, description="Anzahl Kerzen (max 1000)"),
    refresh: bool = Query(default=False, description="Cache umgehen + frisch holen"),
) -> CandlesResponse:
    """OHLCV-Daten für ein Playbook-Instrument auf einem TF.

    Auf Capital-Seite: GET /prices/{epic}?resolution=...&max=... — identisch zu v1.
    Lokal: CSV-Cache mit TTL pro TF (siehe `config.cache_ttl_seconds`).
    """
    try:
        df = await load_candles(instrument, tf, bars=bars, refresh=refresh)
    except CapitalAuthError as e:
        raise HTTPException(status_code=503, detail=f"Capital-Auth fehlgeschlagen: {e}")
    except CapitalAPIError as e:
        raise HTTPException(status_code=e.status if e.status < 600 else 502, detail=str(e))

    candles = [
        Candle(
            time=ts.isoformat(),
            open=float(row.open), high=float(row.high),
            low=float(row.low), close=float(row.close),
            volume=float(row.volume),
        )
        for ts, row in df.iterrows()
    ]
    return CandlesResponse(
        instrument=instrument,
        epic=EPIC_MAP[instrument],
        tf=tf,
        resolution=RESOLUTION_MAP[tf],
        count=len(candles),
        candles=candles,
    )


@app.get("/signal/{instrument}", response_model=SignalResponse)
async def get_signal(
    instrument: InstrumentName,
    sweep_lookback: int = Query(default=10, ge=3, le=100,
                                 description="Wie viele HTF-Bars zurück nach Sweeps suchen"),
    rr_threshold: float = Query(default=2.0, ge=1.0, le=5.0,
                                description="RR-Schwelle für Direct-Entry"),
) -> SignalResponse:
    """Vollständige Playbook-Auswertung — holt alle TFs und ruft engine.evaluate.

    Liefert immer eine Response (auch wenn kein Signal aktiv ist) inkl. `bias_direction`
    und `reason`, damit das Dashboard auch im 'kein Setup'-Fall sinnvoll anzeigen kann.
    """
    tfs_needed = ["1d", "1h", "30m", "15m", "5m", "1m"]
    bars: dict[str, "pd.DataFrame"] = {}
    failed_tfs: list[str] = []
    for tf in tfs_needed:
        try:
            df = await load_candles(instrument, tf, bars=200)
            if not df.empty:
                bars[tf] = df
        except (CapitalAuthError, CapitalAPIError) as e:
            failed_tfs.append(f"{tf}({type(e).__name__})")

    if "1d" not in bars:
        raise HTTPException(status_code=503,
                            detail=f"Daily-Daten fehlen für {instrument}; failed: {failed_tfs}")

    daily_bias = compute_bias(bars["1d"])
    signal = evaluate_signal(
        instrument, bars,
        rr_threshold=rr_threshold,
        sweep_lookback_bars=sweep_lookback,
    )

    if signal is not None:
        return SignalResponse(
            instrument=instrument,
            side=signal.side,
            bias_direction=signal.bias.direction,
            bias_bos_time=signal.bias.last_bos_time.isoformat() if signal.bias.last_bos_time else None,
            bias_bos_level=signal.bias.last_bos_level,
            variant=signal.variant,
            entry=signal.entry,
            sl=signal.sl,
            tp=signal.tp,
            rr=round(signal.rr, 3),
            htf_used=signal.htf_used,
            ltf_used=signal.ltf_used,
            sweep_time=signal.sweep.time.isoformat(),
            sweep_level=signal.sweep.level,
            sweep_direction=signal.sweep.direction,
            bos_time=signal.structure_break.time.isoformat(),
            bos_level=signal.structure_break.broken_swing.price,
            eq_level=signal.equilibrium.eq if signal.equilibrium else None,
            has_ob=signal.ob is not None,
            has_fvg=signal.fvg is not None,
            reason=f"setup found: {signal.variant} ({signal.htf_used}-sweep → {signal.ltf_used}-bos)",
        )

    # Kein Signal — erkläre warum
    reasons = [f"daily bias: {daily_bias.direction}"]
    if daily_bias.direction == "neutral":
        reasons.append("kein Daily-BoS detektiert")
    else:
        reasons.append("kein gültiger HTF-Sweep + LTF-BOS im Lookback-Fenster")
    return SignalResponse(
        instrument=instrument,
        side="none",
        bias_direction=daily_bias.direction,
        bias_bos_time=daily_bias.last_bos_time.isoformat() if daily_bias.last_bos_time else None,
        bias_bos_level=daily_bias.last_bos_level,
        reason=" · ".join(reasons),
    )


# ─── Backtest ────────────────────────────────────────────────────────────


class BacktestRequest(BaseModel):
    instrument: InstrumentName
    iter_tf: Timeframe = Field(default="5m", description="Replay-Auflösung")
    bars: int = Field(default=1000, ge=200, le=1000, description="Bars pro TF (max 1000)")
    initial_capital: float = Field(default=10_000.0, ge=100.0)
    rr_threshold: float = Field(default=2.0, ge=1.0, le=5.0)
    risk_pct: float = Field(default=0.01, ge=0.001, le=0.05)
    sweep_lookback: int = Field(default=10, ge=3, le=50)
    with_walkforward: bool = Field(default=False, description="Rollendes OOS-Test-Fenster zusätzlich laufen lassen")
    wfo_oos_bars: int = Field(default=200, ge=50, le=500, description="OOS-Fenstergröße für WFO")
    wfo_in_sample_bars: int = Field(default=500, ge=100, le=900, description="In-Sample-Fenstergröße für WFO")


class TradeOut(BaseModel):
    open_time: str
    close_time: str
    side: str
    variant: str
    entry: float
    exit: float
    sl: float
    tp: float
    r_multiple: float
    pnl_abs: float
    pnl_pct: float
    exit_reason: str
    bars_held: int
    htf_used: str
    ltf_used: str


class MetricsOut(BaseModel):
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_return_pct: float
    avg_win_r: float
    avg_loss_r: float
    profit_factor: float
    max_drawdown_pct: float
    expectancy_r: float
    sharpe: float
    exposure_pct: float
    longs: int
    shorts: int


class WFWindowOut(BaseModel):
    window_idx: int
    oos_start: str
    oos_end: str
    metrics: MetricsOut
    n_trades: int


class WalkForwardOut(BaseModel):
    n_windows: int
    total_trades: int
    win_rate: float
    total_return_pct: float
    avg_expectancy_r: float
    avg_profit_factor: float
    avg_max_drawdown_pct: float
    avg_sharpe: float
    pct_windows_positive: float
    windows: list[WFWindowOut]


class BacktestResponse(BaseModel):
    instrument: str
    iter_tf: str
    start: str
    end: str
    initial_capital: float
    final_equity: float
    metrics: MetricsOut
    trades: list[TradeOut]
    equity_curve: list[dict]  # [{time: ISO, value: float}]
    report_url: str
    walkforward: WalkForwardOut | None = None


@app.post("/backtest", response_model=BacktestResponse)
async def run_backtest_endpoint(req: BacktestRequest) -> BacktestResponse:
    """Historischer Replay des Strategy-Cores auf einem Instrument.

    Fetcht frische Daten (bis `bars` Kerzen pro TF), führt Bar-für-Bar-Replay
    aus, speichert HTML-Report und gibt Metriken + Trade-Liste zurück.
    Optional: `with_walkforward=true` läuft zusätzlich rollende OOS-Fenster.
    """
    tfs_needed = ["1d", "1h", "30m", "15m", req.iter_tf]
    if "5m" not in tfs_needed:
        tfs_needed.append("5m")
    tfs_needed = list(dict.fromkeys(tfs_needed))  # deduplizieren, Reihenfolge halten

    bars: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    for tf in tfs_needed:
        try:
            df = await load_candles(req.instrument, tf, bars=req.bars, refresh=True)
            if not df.empty:
                bars[tf] = df
        except (CapitalAuthError, CapitalAPIError) as e:
            failed.append(f"{tf}({type(e).__name__})")

    if "1d" not in bars:
        raise HTTPException(
            status_code=503,
            detail=f"Daily-Daten für {req.instrument} nicht ladbar; failed: {failed}",
        )
    if req.iter_tf not in bars:
        raise HTTPException(
            status_code=503,
            detail=f"iter_tf={req.iter_tf} nicht ladbar für {req.instrument}",
        )

    result = await run_backtest(
        instrument=req.instrument,
        bars_full=bars,
        initial_capital=req.initial_capital,
        rr_threshold=req.rr_threshold,
        sweep_lookback=req.sweep_lookback,
        risk_pct_per_trade=req.risk_pct,
        iter_tf=req.iter_tf,
    )

    report_path = render_html(result)
    report_url = f"/reports/{report_path.name}"

    def _metrics(m) -> MetricsOut:
        return MetricsOut(
            total_trades=m.total_trades, wins=m.wins, losses=m.losses,
            win_rate=round(m.win_rate, 4),
            total_return_pct=round(m.total_return_pct, 4),
            avg_win_r=round(m.avg_win_r, 4),
            avg_loss_r=round(m.avg_loss_r, 4),
            profit_factor=round(m.profit_factor, 4),
            max_drawdown_pct=round(m.max_drawdown_pct, 4),
            expectancy_r=round(m.expectancy_r, 4),
            sharpe=round(m.sharpe, 4),
            exposure_pct=round(m.exposure_pct, 4),
            longs=m.longs, shorts=m.shorts,
        )

    trades_out = [
        TradeOut(
            open_time=t.open_time.isoformat(),
            close_time=t.close_time.isoformat(),
            side=t.side,
            variant=t.variant,
            entry=round(t.entry, 5),
            exit=round(t.exit, 5),
            sl=round(t.sl, 5),
            tp=round(t.tp, 5),
            r_multiple=round(t.r_multiple, 3),
            pnl_abs=round(t.pnl_abs, 2),
            pnl_pct=round(t.pnl_pct, 4),
            exit_reason=t.exit_reason,
            bars_held=t.bars_held,
            htf_used=t.htf_used,
            ltf_used=t.ltf_used,
        )
        for t in result.trades
    ]

    equity_curve_out = [
        {"time": ts.isoformat(), "value": round(v, 2)}
        for ts, v in result.equity_curve[::10]  # alle 10 Punkte für Bandbreite
    ]

    wfo_out: WalkForwardOut | None = None
    if req.with_walkforward:
        try:
            wfo = await run_walk_forward(
                instrument=req.instrument,
                bars_full=bars,
                iter_tf=req.iter_tf,
                oos_bars=req.wfo_oos_bars,
                in_sample_bars=req.wfo_in_sample_bars,
                initial_capital=req.initial_capital,
                rr_threshold=req.rr_threshold,
                sweep_lookback=req.sweep_lookback,
                risk_pct_per_trade=req.risk_pct,
            )
            wfo_out = WalkForwardOut(
                n_windows=wfo.n_windows,
                total_trades=wfo.total_trades,
                win_rate=round(wfo.win_rate, 4),
                total_return_pct=round(wfo.total_return_pct, 4),
                avg_expectancy_r=round(wfo.avg_expectancy_r, 4),
                avg_profit_factor=round(wfo.avg_profit_factor, 4),
                avg_max_drawdown_pct=round(wfo.avg_max_drawdown_pct, 4),
                avg_sharpe=round(wfo.avg_sharpe, 4),
                pct_windows_positive=round(wfo.pct_windows_positive, 4),
                windows=[
                    WFWindowOut(
                        window_idx=w.window_idx,
                        oos_start=w.oos_start.isoformat(),
                        oos_end=w.oos_end.isoformat(),
                        metrics=_metrics(w.result.metrics),
                        n_trades=w.result.metrics.total_trades,
                    )
                    for w in wfo.windows
                ],
            )
        except ValueError as e:
            # Zu wenig Daten für WFO — kein Fehler, nur kein WFO-Output
            pass

    return BacktestResponse(
        instrument=result.instrument,
        iter_tf=result.iter_tf,
        start=result.start.isoformat(),
        end=result.end.isoformat(),
        initial_capital=result.initial_capital,
        final_equity=round(result.final_equity, 2),
        metrics=_metrics(result.metrics),
        trades=trades_out,
        equity_curve=equity_curve_out,
        report_url=report_url,
        walkforward=wfo_out,
    )


@app.get("/diagnose/{instrument}")
async def diagnose(
    instrument: InstrumentName,
    sweep_lookback: int = Query(default=50, ge=3, le=200),
) -> dict:
    """Zwischenschritte des Engines transparent — Bias, Pivots, Levels, Sweeps, LTF-BOS pro TF.

    Hilft beim Verstehen warum aktuell kein Signal entsteht: zu wenig prominente
    Pivot-Lows? Sweep noch nicht passiert? LTF-BOS verpasst?
    """
    tfs_needed = ["1d", "1h", "30m", "15m", "5m", "1m"]
    bars: dict[str, "pd.DataFrame"] = {}
    for tf in tfs_needed:
        try:
            df = await load_candles(instrument, tf, bars=200)
            if not df.empty:
                bars[tf] = df
        except Exception:
            pass

    if "1d" not in bars:
        raise HTTPException(503, f"Daily-Daten fehlen für {instrument}")

    daily_bias = compute_bias(bars["1d"])
    result = {
        "instrument": instrument,
        "epic": EPIC_MAP[instrument],
        "now_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "session": session_label(pd.Timestamp.now(tz="UTC"), instrument),
        "daily_bias": {
            "direction": daily_bias.direction,
            "last_bos_time": daily_bias.last_bos_time.isoformat() if daily_bias.last_bos_time else None,
            "last_bos_level": daily_bias.last_bos_level,
            "daily_bars_loaded": len(bars["1d"]),
        },
        "htf_diagnostics": {},
        "ltf_diagnostics": {},
    }

    if daily_bias.direction == "neutral":
        result["note"] = "Bias neutral → keine LQ-Suche, kein Setup möglich"
        return result

    for tf in ["1h", "30m", "15m"]:
        if tf not in bars:
            result["htf_diagnostics"][tf] = {"missing": True}
            continue
        df = bars[tf]
        pivots = find_pivots(df)
        levels = liquidity_levels(df, daily_bias.direction)
        sweeps = detect_sweeps(df, levels, tf=tf, lookback=sweep_lookback)
        in_sess = is_in_session(df.index[-1], instrument)
        result["htf_diagnostics"][tf] = {
            "bars": len(df),
            "pivots_total": len(pivots),
            "pivot_highs": sum(1 for p in pivots if p.kind == "high"),
            "pivot_lows": sum(1 for p in pivots if p.kind == "low"),
            "liquidity_levels": len(levels),
            "sweeps_in_lookback": len(sweeps),
            "in_session": in_sess,
            "latest_bar_time": df.index[-1].isoformat(),
            "latest_sweep": (
                {
                    "time": sweeps[-1].time.isoformat(),
                    "level": sweeps[-1].level,
                    "direction": sweeps[-1].direction,
                } if sweeps else None
            ),
        }

    # LTF-BOS-Check (vom letzten verfügbaren HTF-Sweep aus)
    latest_sweep = None
    for tf in ["1h", "30m", "15m"]:
        if tf in bars:
            levels = liquidity_levels(bars[tf], daily_bias.direction)
            sweeps = detect_sweeps(bars[tf], levels, tf=tf, lookback=sweep_lookback)
            if sweeps:
                latest_sweep = sweeps[-1]
                break

    if latest_sweep:
        for ltf in ["5m", "1m"]:
            if ltf not in bars:
                continue
            df = bars[ltf]
            after = df[df.index >= latest_sweep.time]
            if len(after) < 3:
                result["ltf_diagnostics"][ltf] = {"insufficient_bars_after_sweep": len(after)}
                continue
            idx = df.index.get_loc(after.index[0])
            if isinstance(idx, slice):
                idx = idx.start
            bos = find_bos_after(df, daily_bias.direction, int(idx))
            result["ltf_diagnostics"][ltf] = {
                "bars_after_sweep": len(after),
                "bos_found": bos is not None,
                "bos_time": bos.time.isoformat() if bos else None,
                "bos_broken_level": bos.broken_swing.price if bos else None,
            }

    return result


# ─── Live-Bot ────────────────────────────────────────────────────────────


class BotStartRequest(BaseModel):
    initial_capital: float | None = Field(default=None, ge=100.0)
    tick_interval_s: int | None = Field(default=None, ge=10, le=600)
    instruments: list[InstrumentName] | None = None
    rr_threshold: float | None = Field(default=None, ge=1.0, le=5.0)
    risk_pct: float | None = Field(default=None, ge=0.001, le=0.05)
    sweep_lookback: int | None = Field(default=None, ge=3, le=50)
    reset: bool = Field(default=False, description="Trades + Equity zurücksetzen vor Start")


class BotStateResponse(BaseModel):
    running: bool
    started_at: str | None
    stopped_at: str | None
    last_tick: str | None
    last_signal_check: str | None
    tick_interval_s: int
    initial_capital: float
    equity: float
    instruments: list[str]
    open_trades: list[dict]
    closed_trades: list[dict]
    last_error: str | None
    rr_threshold: float
    risk_pct: float
    sweep_lookback: int
    n_ws_clients: int


def _state_to_response() -> BotStateResponse:
    orch = get_orchestrator()
    d = orch.state.to_json()
    d["n_ws_clients"] = ws_hub.n_clients()
    return BotStateResponse(**d)


@app.get("/bot/state", response_model=BotStateResponse)
def bot_state() -> BotStateResponse:
    """Aktueller Bot-State — auch wenn der Bot nicht läuft."""
    return _state_to_response()


@app.post("/bot/start", response_model=BotStateResponse)
async def bot_start(req: BotStartRequest) -> BotStateResponse:
    """Startet den Tick-Loop. Idempotent: wenn er schon läuft, gibt aktuellen State."""
    orch = get_orchestrator()
    await orch.start(
        initial_capital=req.initial_capital,
        tick_interval_s=req.tick_interval_s,
        instruments=req.instruments,
        rr_threshold=req.rr_threshold,
        risk_pct=req.risk_pct,
        sweep_lookback=req.sweep_lookback,
        reset=req.reset,
    )
    await ws_hub.push_to_dashboard({"type": "state", "state": orch.state.to_json()})
    return _state_to_response()


@app.post("/bot/stop", response_model=BotStateResponse)
async def bot_stop() -> BotStateResponse:
    """Stoppt den Tick-Loop. State (offene Trades, Equity) bleibt erhalten."""
    orch = get_orchestrator()
    await orch.stop()
    await ws_hub.push_to_dashboard({"type": "state", "state": orch.state.to_json()})
    return _state_to_response()


@app.post("/bot/reset", response_model=BotStateResponse)
async def bot_reset() -> BotStateResponse:
    """Komplett-Reset: stoppt Bot, leert Trades, Equity = initial_capital."""
    orch = get_orchestrator()
    await orch.reset()
    await ws_hub.push_to_dashboard({"type": "state", "state": orch.state.to_json()})
    return _state_to_response()


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    """WebSocket für Live-Updates — sendet zunächst kompletten State, dann Push pro Event."""
    orch = get_orchestrator()
    initial = {"type": "state", "state": orch.state.to_json()}
    await ws_hub.handle_client(ws, initial_message=initial)
