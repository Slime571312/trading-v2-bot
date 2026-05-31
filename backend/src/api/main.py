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
from src.chat import get_chat_client, get_chat_state
from src.config import EPIC_MAP, RESOLUTION_MAP, config
from src.data.fetcher import load_candles
from src.data.signal_cache import get_signal_cache
from src.live import DEFAULT_INSTRUMENTS, get_orchestrator
from src.live.orchestrator import shutdown_orchestrator
from src.tuner import get_tuner
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
    """FastAPI-Lifecycle. Lädt Bot-State + startet SignalCache-Background-Task."""
    orch = get_orchestrator()
    cache = get_signal_cache()
    log_app = logging.getLogger("trading-v2")
    n_inst = len(orch.state.instances)
    total_open = sum(len(i.open_trades) for i in orch.state.instances.values())
    total_closed = sum(len(i.closed_trades) for i in orch.state.instances.values())
    log_app.info(
        "Bot-Orchestrator initialisiert: %d Instanzen, open=%d, closed=%d",
        n_inst, total_open, total_closed,
    )
    await cache.start(instruments=DEFAULT_INSTRUMENTS, refresh_interval_s=60)
    log_app.info("SignalCache Background-Task gestartet")
    yield
    await cache.stop()
    await shutdown_orchestrator()
    log_app.info("Backend-Shutdown — Bot + Cache gestoppt, State persistiert")


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
    chat_backend: str  # 'agent-sdk' wenn `claude` CLI da, sonst 'unavailable'


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
    """Signal aus dem Cache. Bei `side='none'` ist kein Setup aktiv,
    bei `side='error'` ist die Auswertung gecrasht (siehe `error`-Feld)."""

    instrument: str
    epic: str | None = None
    refreshed_at: str | None = None
    side: Literal["long", "short", "none", "error"]
    bias_direction: Literal["long", "short", "neutral"] = "neutral"
    bias_bos_time: str | None = None
    bias_bos_level: float | None = None
    variant: Literal["primary", "ob_retest", "fvg_retest", "ultimate"] | None = None
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    rr: float | None = None
    htf_used: str | None = None
    ltf_used: str | None = None
    has_ob: bool = False
    has_fvg: bool = False
    reason: str = ""
    error: str | None = None

    model_config = {"extra": "ignore"}  # ignoriere unbekannte Cache-Felder


# ─── Routes ──────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Smoke-Test-Endpoint. Liefert Backend-Status + Verbindungs-Indikatoren."""
    orch = get_orchestrator()
    return HealthResponse(
        status="ok",
        version="0.3.0",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        bot_running=orch.any_running(),
        broker_connected=config.broker_configured,
    )


@app.get("/config", response_model=ConfigResponse)
def get_config() -> ConfigResponse:
    """Aktuelle Playbook-Konfiguration. Read-only in Etappe 0 — schreibbar später."""
    import shutil
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
        chat_backend="agent-sdk" if shutil.which("claude") else "unavailable",
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
async def get_signal(instrument: InstrumentName) -> SignalResponse:
    """Cached Signal-Auswertung — antwortet aus dem SignalCache (instant).

    Der Cache refresht im Hintergrund alle 60s. Beim ersten Call vor dem
    initialen Refresh wartet wir kurz auf den Cache (max 5s).
    """
    cache = get_signal_cache()
    if not cache.get(instrument):
        await cache.wait_initial(timeout=5.0)
    cached = cache.get(instrument)
    if not cached:
        raise HTTPException(503, f"Signal für {instrument} noch nicht im Cache")
    return SignalResponse(**cached.to_json())


@app.get("/signals", response_model=list[SignalResponse])
async def get_signals_batch() -> list[SignalResponse]:
    """Batch-Endpoint: alle Signals in einem Call (für Dashboard-Cards)."""
    cache = get_signal_cache()
    if not cache.get_all():
        await cache.wait_initial(timeout=5.0)
    return [SignalResponse(**c.to_json()) for c in cache.get_all()]


@app.post("/signals/refresh")
async def refresh_signals(instrument: InstrumentName | None = None) -> dict:
    """Triggert sofortigen Cache-Refresh (UI 'Refresh now' Button)."""
    cache = get_signal_cache()
    await cache.refresh_now(instrument=instrument)
    return {"refreshed": instrument or "all", "n_cached": len(cache.get_all())}


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


# ─── Multi-Bot — eine Instanz pro Instrument ─────────────────────────────


class BotStartRequest(BaseModel):
    initial_capital: float | None = Field(default=None, ge=100.0)
    tick_interval_s: int | None = Field(default=None, ge=10, le=600)
    rr_threshold: float | None = Field(default=None, ge=1.0, le=5.0)
    risk_pct: float | None = Field(default=None, ge=0.001, le=0.05)
    sweep_lookback: int | None = Field(default=None, ge=3, le=50)
    reset: bool = Field(default=False)


class StartAllRequest(BaseModel):
    instruments: list[InstrumentName] | None = Field(default=None,
                                                      description="None → alle bekannten")
    initial_capital: float | None = None
    tick_interval_s: int | None = None
    rr_threshold: float | None = None
    risk_pct: float | None = None
    sweep_lookback: int | None = None
    reset: bool = False


class TickLogOut(BaseModel):
    timestamp: str
    action: str
    decision: str
    bias: str | None = None
    htf_used: str | None = None
    ltf_used: str | None = None
    sweep_time: str | None = None
    sweep_direction: str | None = None
    bos_time: str | None = None
    rr_computed: float | None = None
    variant: str | None = None
    detail: str | None = None
    related_trade_id: str | None = None


class BotInstanceResponse(BaseModel):
    instrument: str
    running: bool
    started_at: str | None
    stopped_at: str | None
    last_tick: str | None
    last_signal_check: str | None
    tick_interval_s: int
    initial_capital: float
    equity: float
    open_trades: list[dict]
    closed_trades: list[dict]
    last_error: str | None
    rr_threshold: float
    risk_pct: float
    sweep_lookback: int
    n_tick_log: int  # Anzahl ohne den ganzen Log zu senden


def _instance_to_response(inst) -> BotInstanceResponse:
    d = inst.to_json()
    d.pop("tick_log", None)
    d["n_tick_log"] = len(inst.tick_log)
    return BotInstanceResponse(**d)


class BotOverviewResponse(BaseModel):
    instances: list[BotInstanceResponse]
    total_equity: float
    any_running: bool
    n_ws_clients: int


@app.get("/bot/instances", response_model=BotOverviewResponse)
def bot_instances() -> BotOverviewResponse:
    """Alle Bot-Instanzen im Überblick."""
    orch = get_orchestrator()
    return BotOverviewResponse(
        instances=[_instance_to_response(inst) for inst in orch.state.instances.values()],
        total_equity=round(orch.state.total_equity(), 2),
        any_running=orch.any_running(),
        n_ws_clients=ws_hub.n_clients(),
    )


@app.get("/bot/{instrument}", response_model=BotInstanceResponse)
def bot_get(instrument: InstrumentName) -> BotInstanceResponse:
    orch = get_orchestrator()
    inst = orch.state.ensure(instrument)
    return _instance_to_response(inst)


@app.post("/bot/{instrument}/start", response_model=BotInstanceResponse)
async def bot_start(instrument: InstrumentName, req: BotStartRequest) -> BotInstanceResponse:
    orch = get_orchestrator()
    inst = await orch.start(
        instrument,
        initial_capital=req.initial_capital,
        tick_interval_s=req.tick_interval_s,
        rr_threshold=req.rr_threshold,
        risk_pct=req.risk_pct,
        sweep_lookback=req.sweep_lookback,
        reset=req.reset,
    )
    await ws_hub.push_to_dashboard({
        "type": "instance_update", "instrument": instrument,
        "instance": inst.to_json(),
    })
    return _instance_to_response(inst)


@app.post("/bot/{instrument}/stop", response_model=BotInstanceResponse)
async def bot_stop(instrument: InstrumentName) -> BotInstanceResponse:
    orch = get_orchestrator()
    try:
        inst = await orch.stop(instrument)
    except KeyError as e:
        raise HTTPException(404, str(e))
    await ws_hub.push_to_dashboard({
        "type": "instance_update", "instrument": instrument,
        "instance": inst.to_json(),
    })
    return _instance_to_response(inst)


@app.post("/bot/{instrument}/reset", response_model=BotInstanceResponse)
async def bot_reset(instrument: InstrumentName) -> BotInstanceResponse:
    orch = get_orchestrator()
    inst = await orch.reset(instrument)
    await ws_hub.push_to_dashboard({
        "type": "instance_update", "instrument": instrument,
        "instance": inst.to_json(),
    })
    return _instance_to_response(inst)


@app.get("/bot/{instrument}/ticks", response_model=list[TickLogOut])
def bot_ticks(instrument: InstrumentName, limit: int = Query(default=50, ge=1, le=200)) -> list[TickLogOut]:
    """Tick-Log einer Instanz — neueste zuerst."""
    orch = get_orchestrator()
    inst = orch.state.ensure(instrument)
    return [TickLogOut(**t.to_json()) for t in reversed(inst.tick_log[-limit:])]


@app.post("/bot/start-all", response_model=BotOverviewResponse)
async def bot_start_all(req: StartAllRequest) -> BotOverviewResponse:
    orch = get_orchestrator()
    targets = req.instruments or DEFAULT_INSTRUMENTS
    for inst_name in targets:
        await orch.start(
            inst_name,
            initial_capital=req.initial_capital,
            tick_interval_s=req.tick_interval_s,
            rr_threshold=req.rr_threshold,
            risk_pct=req.risk_pct,
            sweep_lookback=req.sweep_lookback,
            reset=req.reset,
        )
    return bot_instances()


@app.post("/bot/stop-all", response_model=BotOverviewResponse)
async def bot_stop_all() -> BotOverviewResponse:
    orch = get_orchestrator()
    await orch.stop_all()
    return bot_instances()


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    """WebSocket für Live-Updates — sendet initial alle Instanzen, dann Push pro Event."""
    orch = get_orchestrator()
    initial = {
        "type": "snapshot",
        "instances": {k: v.to_json() for k, v in orch.state.instances.items()},
    }
    await ws_hub.handle_client(ws, initial_message=initial)


# ─── Chat-Bot ────────────────────────────────────────────────────────────


class ChatMessageRequest(BaseModel):
    conversation_id: str | None = Field(default=None,
                                         description="None → neue Konversation wird erzeugt")
    message: str = Field(min_length=1, max_length=8000)
    model: str | None = Field(default=None,
                               description="sonnet | opus | haiku (None = Claude-Code Default)")


class ToolCallOut(BaseModel):
    name: str
    input: dict
    output: str


class ChatMessageResponse(BaseModel):
    conversation_id: str
    reply: str
    tool_calls: list[ToolCallOut]
    num_turns: int
    model_used: str | None
    cost_usd: float | None
    duration_ms: int | None


class ChatTurnOut(BaseModel):
    role: str
    text: str
    timestamp: str
    tool_calls: list[ToolCallOut]


class ConversationOut(BaseModel):
    id: str
    created_at: str
    title: str
    n_turns: int
    model: str | None
    session_id: str | None


class ConversationDetailOut(ConversationOut):
    turns: list[ChatTurnOut]


class ProposalOut(BaseModel):
    id: str
    created_at: str
    diff: dict
    rationale: str
    status: str
    applied_at: str | None
    rejected_at: str | None
    conversation_id: str | None


@app.post("/chat/message", response_model=ChatMessageResponse)
async def chat_message(req: ChatMessageRequest) -> ChatMessageResponse:
    """Eine User-Message via Claude Agent SDK senden.

    Erzeugt neue Konversation wenn keine angegeben. Nutzt die Claude-Code-CLI
    des Users (subscription-based, KEIN ANTHROPIC_API_KEY).
    """
    from src.chat import ChatTurn, ToolCallRecord, is_available

    if not await is_available():
        raise HTTPException(
            503,
            "Claude-Code-CLI nicht gefunden (`claude` nicht im PATH). "
            "Install: https://docs.claude.com/en/docs/claude-code/installation",
        )

    state = get_chat_state()
    client = get_chat_client()

    conv = state.get(req.conversation_id) if req.conversation_id else None
    if conv is None:
        conv = state.new_conversation(model=req.model)

    # User-Turn anhängen + persistieren
    user_turn = ChatTurn(role="user", text=req.message)
    state.append_turn(conv.id, user_turn)

    # Erste User-Frage wird Titel
    if len(conv.turns) == 1:
        title = req.message.strip().split("\n")[0][:60]
        state.update_title(conv.id, title)

    try:
        result = await client.run_turn(
            user_message=req.message,
            conversation_id=conv.id,
            session_id=conv.session_id,
            model=req.model or conv.model,
        )
    except Exception as e:
        logging.getLogger(__name__).exception("chat-call gescheitert: %s", e)
        raise HTTPException(500, f"Chat-Call gescheitert: {type(e).__name__}: {e}")

    # Assistant-Turn persistieren + session_id für Resumption merken
    assistant_turn = ChatTurn(
        role="assistant",
        text=result.text,
        tool_calls=list(result.tool_calls),
    )
    state.append_turn(conv.id, assistant_turn)
    if result.session_id:
        state.update_session_id(conv.id, result.session_id)

    return ChatMessageResponse(
        conversation_id=conv.id,
        reply=result.text,
        tool_calls=[
            ToolCallOut(name=t.name, input=t.input, output=t.output)
            for t in result.tool_calls
        ],
        num_turns=result.num_turns,
        model_used=result.model_used,
        cost_usd=result.cost_usd,
        duration_ms=result.duration_ms,
    )


@app.get("/chat/conversations", response_model=list[ConversationOut])
def chat_list() -> list[ConversationOut]:
    state = get_chat_state()
    return [
        ConversationOut(
            id=c.id, created_at=c.created_at, title=c.title,
            n_turns=len(c.turns), model=c.model, session_id=c.session_id,
        )
        for c in state.list_conversations()
    ]


@app.get("/chat/conversations/{conv_id}", response_model=ConversationDetailOut)
def chat_get(conv_id: str) -> ConversationDetailOut:
    state = get_chat_state()
    conv = state.get(conv_id)
    if not conv:
        raise HTTPException(404, f"Konversation {conv_id} nicht gefunden")
    return ConversationDetailOut(
        id=conv.id, created_at=conv.created_at, title=conv.title,
        n_turns=len(conv.turns), model=conv.model, session_id=conv.session_id,
        turns=[
            ChatTurnOut(
                role=t.role, text=t.text, timestamp=t.timestamp,
                tool_calls=[
                    ToolCallOut(name=tc.name, input=tc.input, output=tc.output)
                    for tc in t.tool_calls
                ],
            )
            for t in conv.turns
        ],
    )


@app.delete("/chat/conversations/{conv_id}")
def chat_delete(conv_id: str) -> dict:
    state = get_chat_state()
    if not state.delete(conv_id):
        raise HTTPException(404, f"Konversation {conv_id} nicht gefunden")
    return {"deleted": conv_id}


@app.get("/chat/proposals", response_model=list[ProposalOut])
def chat_proposals(
    status: Literal["pending", "applied", "rejected"] | None = None,
) -> list[ProposalOut]:
    state = get_chat_state()
    return [
        ProposalOut(**p.to_json()) for p in state.list_proposals(status=status)
    ]


class ProposalApplyRequest(BaseModel):
    instrument: InstrumentName | None = Field(
        default=None,
        description=("Auf welche Bot-Instanz anwenden. None → alle Default-Instanzen "
                     "(gleiche Params überall)"),
    )


@app.post("/chat/proposals/{prop_id}/apply", response_model=ProposalOut)
async def chat_proposal_apply(prop_id: str, req: ProposalApplyRequest = ProposalApplyRequest()) -> ProposalOut:
    """Wendet einen Config-Diff-Vorschlag auf eine (oder alle) Bot-Instanz(en) an."""
    state = get_chat_state()
    proposal = state.get_proposal(prop_id)
    if not proposal:
        raise HTTPException(404, f"Vorschlag {prop_id} nicht gefunden")
    if proposal.status != "pending":
        raise HTTPException(409, f"Vorschlag bereits {proposal.status}")

    orch = get_orchestrator()
    targets = [req.instrument] if req.instrument else DEFAULT_INSTRUMENTS
    try:
        for inst_name in targets:
            await orch.apply_config_diff(inst_name, proposal.diff)
    except KeyError as e:
        raise HTTPException(400, str(e))

    state.mark_proposal(prop_id, "applied")
    refreshed = state.get_proposal(prop_id)
    for inst_name in targets:
        inst = orch.state.ensure(inst_name)
        await ws_hub.push_to_dashboard({
            "type": "instance_update", "instrument": inst_name, "instance": inst.to_json(),
        })
    return ProposalOut(**refreshed.to_json())


@app.post("/chat/proposals/{prop_id}/reject", response_model=ProposalOut)
def chat_proposal_reject(prop_id: str) -> ProposalOut:
    state = get_chat_state()
    proposal = state.get_proposal(prop_id)
    if not proposal:
        raise HTTPException(404, f"Vorschlag {prop_id} nicht gefunden")
    if proposal.status != "pending":
        raise HTTPException(409, f"Vorschlag bereits {proposal.status}")
    state.mark_proposal(prop_id, "rejected")
    refreshed = state.get_proposal(prop_id)
    return ProposalOut(**refreshed.to_json())


# ─── Self-Improve-Tuner (Etappe 7) ──────────────────────────────────────


class TunerRunRequest(BaseModel):
    instruments: list[InstrumentName] | None = Field(default=None,
                                                      description="None → nutzt Bot-State-Instrumente")
    iter_tf: Timeframe = Field(default="5m")
    bars: int = Field(default=1000, ge=200, le=1000)
    use_claude: bool = Field(default=True,
                              description="Wenn False, Template-Rationale auch wenn API-Key da")
    triggered_by: Literal["manual", "cron"] = Field(default="manual")


class TunerRunOut(BaseModel):
    id: str
    started_at: str
    finished_at: str | None
    status: str
    instruments: list[str]
    iter_tf: str
    bars_used: int
    results: dict[str, dict]
    proposal_ids: list[str]
    claude_summary: str | None
    error: str | None
    triggered_by: str


class TunerStatusOut(BaseModel):
    is_running: bool
    current_run_id: str | None
    n_runs_history: int


@app.get("/tuner/status", response_model=TunerStatusOut)
def tuner_status() -> TunerStatusOut:
    t = get_tuner()
    return TunerStatusOut(
        is_running=t.is_running,
        current_run_id=t.current_run.id if t.current_run else None,
        n_runs_history=len(t.history.runs),
    )


@app.get("/tuner/history", response_model=list[TunerRunOut])
def tuner_history(limit: int = Query(default=20, ge=1, le=50)) -> list[TunerRunOut]:
    t = get_tuner()
    runs = sorted(t.history.runs, key=lambda r: r.started_at, reverse=True)[:limit]
    return [TunerRunOut(**r.to_json()) for r in runs]


@app.get("/tuner/runs/{run_id}", response_model=TunerRunOut)
def tuner_run_detail(run_id: str) -> TunerRunOut:
    t = get_tuner()
    run = next((r for r in t.history.runs if r.id == run_id), None)
    if not run:
        raise HTTPException(404, f"Tuner-Run {run_id} nicht gefunden")
    return TunerRunOut(**run.to_json())


@app.post("/tuner/run", response_model=TunerRunOut)
async def tuner_run(req: TunerRunRequest) -> TunerRunOut:
    """Triggert einen Tuner-Lauf. Blockt bis fertig (kann mehrere Minuten dauern).

    Verhält sich idempotent bei is_running: returnt 409 statt 2× parallel zu laufen.
    """
    t = get_tuner()
    if t.is_running:
        raise HTTPException(409, f"Tuner läuft bereits (run_id={t.current_run.id})")
    try:
        run = await t.run(
            instruments=req.instruments,
            iter_tf=req.iter_tf,
            bars=req.bars,
            triggered_by=req.triggered_by,
            use_claude=req.use_claude,
        )
    except Exception as e:
        raise HTTPException(500, f"Tuner-Run gecrasht: {type(e).__name__}: {e}")
    return TunerRunOut(**run.to_json())
