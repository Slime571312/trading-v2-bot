const BASE = "/api/backend";

export type Instrument = "DE40" | "NASDAQ" | "SP500" | "BTC";
export type Timeframe = "1m" | "5m" | "15m" | "30m" | "1h" | "4h" | "1d";

export interface SignalResponse {
  instrument: string;
  side: "long" | "short" | "none";
  bias_direction: "long" | "short" | "neutral";
  bias_bos_time: string | null;
  bias_bos_level: number | null;
  variant: "primary" | "ob_retest" | "fvg_retest" | "ultimate" | null;
  entry: number | null;
  sl: number | null;
  tp: number | null;
  rr: number | null;
  htf_used: string | null;
  ltf_used: string | null;
  has_ob: boolean;
  has_fvg: boolean;
  reason: string;
}

export interface MetricsOut {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_return_pct: number;
  avg_win_r: number;
  avg_loss_r: number;
  profit_factor: number;
  max_drawdown_pct: number;
  expectancy_r: number;
  sharpe: number;
  exposure_pct: number;
  longs: number;
  shorts: number;
}

export interface TradeOut {
  open_time: string;
  close_time: string;
  side: string;
  variant: string;
  entry: number;
  exit: number;
  sl: number;
  tp: number;
  r_multiple: number;
  pnl_abs: number;
  pnl_pct: number;
  exit_reason: string;
  bars_held: number;
  htf_used: string;
  ltf_used: string;
}

export interface WFWindowOut {
  window_idx: number;
  oos_start: string;
  oos_end: string;
  metrics: MetricsOut;
  n_trades: number;
}

export interface WalkForwardOut {
  n_windows: number;
  total_trades: number;
  win_rate: number;
  total_return_pct: number;
  avg_expectancy_r: number;
  avg_profit_factor: number;
  avg_max_drawdown_pct: number;
  avg_sharpe: number;
  pct_windows_positive: number;
  windows: WFWindowOut[];
}

export interface BacktestResponse {
  instrument: string;
  iter_tf: string;
  start: string;
  end: string;
  initial_capital: number;
  final_equity: number;
  metrics: MetricsOut;
  trades: TradeOut[];
  equity_curve: { time: string; value: number }[];
  report_url: string;
  walkforward: WalkForwardOut | null;
}

export interface BacktestRequest {
  instrument: Instrument;
  iter_tf: Timeframe;
  bars: number;
  initial_capital: number;
  rr_threshold: number;
  risk_pct: number;
  sweep_lookback: number;
  with_walkforward: boolean;
  wfo_oos_bars: number;
  wfo_in_sample_bars: number;
}

export interface HealthResponse {
  status: string;
  version: string;
  timestamp_utc: string;
  bot_running: boolean;
  broker_connected: boolean;
}

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch(`${BASE}/health`, { cache: "no-store" });
  if (!res.ok) throw new Error(`health ${res.status}`);
  return res.json();
}

export async function fetchSignal(instrument: Instrument): Promise<SignalResponse> {
  const res = await fetch(`${BASE}/signal/${instrument}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`signal ${res.status}`);
  return res.json();
}

export async function runBacktest(req: BacktestRequest): Promise<BacktestResponse> {
  const res = await fetch(`${BASE}/backtest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? `backtest ${res.status}`);
  }
  return res.json();
}

// ─── Live Bot ──────────────────────────────────────────────────────────

export interface OpenTrade {
  id: string;
  instrument: string;
  side: "long" | "short";
  open_time: string;
  entry: number;
  sl: number;
  tp: number;
  size: number;
  variant: "primary" | "ob_retest" | "fvg_retest" | "ultimate";
  htf_used: string;
  ltf_used: string;
  rr_at_open: number;
}

export interface ClosedTrade extends OpenTrade {
  close_time: string;
  exit: number;
  pnl_abs: number;
  pnl_pct: number;
  r_multiple: number;
  exit_reason: "sl" | "tp" | "manual" | "bot_stopped";
}

export interface BotState {
  running: boolean;
  started_at: string | null;
  stopped_at: string | null;
  last_tick: string | null;
  last_signal_check: string | null;
  tick_interval_s: number;
  initial_capital: number;
  equity: number;
  instruments: string[];
  open_trades: OpenTrade[];
  closed_trades: ClosedTrade[];
  last_error: string | null;
  rr_threshold: number;
  risk_pct: number;
  sweep_lookback: number;
  n_ws_clients?: number;
}

export interface BotStartRequest {
  initial_capital?: number;
  tick_interval_s?: number;
  instruments?: Instrument[];
  rr_threshold?: number;
  risk_pct?: number;
  sweep_lookback?: number;
  reset?: boolean;
}

export async function fetchBotState(): Promise<BotState> {
  const res = await fetch(`${BASE}/bot/state`, { cache: "no-store" });
  if (!res.ok) throw new Error(`bot/state ${res.status}`);
  return res.json();
}

export async function startBot(req: BotStartRequest = {}): Promise<BotState> {
  const res = await fetch(`${BASE}/bot/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (!res.ok) throw new Error(`bot/start ${res.status}`);
  return res.json();
}

export async function stopBot(): Promise<BotState> {
  const res = await fetch(`${BASE}/bot/stop`, { method: "POST" });
  if (!res.ok) throw new Error(`bot/stop ${res.status}`);
  return res.json();
}

export async function resetBot(): Promise<BotState> {
  const res = await fetch(`${BASE}/bot/reset`, { method: "POST" });
  if (!res.ok) throw new Error(`bot/reset ${res.status}`);
  return res.json();
}
