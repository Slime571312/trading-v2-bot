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

// ─── Chat ──────────────────────────────────────────────────────────────

export type ChatModel = "sonnet" | "opus" | "haiku";

export interface ToolCall {
  name: string;
  input: Record<string, unknown>;
  output: string;
}

export interface ChatTurn {
  role: "user" | "assistant";
  text: string;
  timestamp: string;
  tool_calls: ToolCall[];
}

export interface ChatMessageResponse {
  conversation_id: string;
  reply: string;
  tool_calls: ToolCall[];
  num_turns: number;
  model_used: string | null;
  cost_usd: number | null;
  duration_ms: number | null;
}

export interface ConversationSummary {
  id: string;
  created_at: string;
  title: string;
  n_turns: number;
  model: string | null;
  session_id: string | null;
}

export interface ConversationDetail extends ConversationSummary {
  turns: ChatTurn[];
}

export interface Proposal {
  id: string;
  created_at: string;
  diff: Record<string, unknown>;
  rationale: string;
  status: "pending" | "applied" | "rejected";
  applied_at: string | null;
  rejected_at: string | null;
  conversation_id: string | null;
}

export async function sendChatMessage(
  message: string,
  conversation_id?: string,
  model?: ChatModel,
): Promise<ChatMessageResponse> {
  const res = await fetch(`${BASE}/chat/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, conversation_id, model }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? `chat ${res.status}`);
  }
  return res.json();
}

export async function listConversations(): Promise<ConversationSummary[]> {
  const res = await fetch(`${BASE}/chat/conversations`, { cache: "no-store" });
  if (!res.ok) throw new Error(`conversations ${res.status}`);
  return res.json();
}

export async function getConversation(id: string): Promise<ConversationDetail> {
  const res = await fetch(`${BASE}/chat/conversations/${id}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`conversation ${res.status}`);
  return res.json();
}

export async function deleteConversation(id: string): Promise<void> {
  const res = await fetch(`${BASE}/chat/conversations/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`delete ${res.status}`);
}

export async function listProposals(status?: "pending" | "applied" | "rejected"): Promise<Proposal[]> {
  const url = status ? `${BASE}/chat/proposals?status=${status}` : `${BASE}/chat/proposals`;
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`proposals ${res.status}`);
  return res.json();
}

export async function applyProposal(id: string): Promise<Proposal> {
  const res = await fetch(`${BASE}/chat/proposals/${id}/apply`, { method: "POST" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? `apply ${res.status}`);
  }
  return res.json();
}

export async function rejectProposal(id: string): Promise<Proposal> {
  const res = await fetch(`${BASE}/chat/proposals/${id}/reject`, { method: "POST" });
  if (!res.ok) throw new Error(`reject ${res.status}`);
  return res.json();
}

// ─── Tuner ──────────────────────────────────────────────────────────────

export interface GridCombo {
  rr_threshold: number;
  sweep_lookback: number;
  total_trades: number;
  win_rate: number;
  expectancy_r: number;
  profit_factor: number;
  total_return_pct: number;
  max_drawdown_pct: number;
  sharpe: number;
  score: number;
}

export interface GridResult {
  instrument: string;
  iter_tf: string;
  bars_used: number;
  current_rr_threshold: number;
  current_sweep_lookback: number;
  current_score: number | null;
  current_too_few_trades: boolean;
  improvement_vs_current: number | null;
  best: GridCombo | null;
  combos: GridCombo[];
}

export interface TunerRun {
  id: string;
  started_at: string;
  finished_at: string | null;
  status: "running" | "completed" | "failed";
  instruments: string[];
  iter_tf: string;
  bars_used: number;
  results: Record<string, GridResult | { error: string }>;
  proposal_ids: string[];
  claude_summary: string | null;
  error: string | null;
  triggered_by: "manual" | "cron";
}

export interface TunerStatus {
  is_running: boolean;
  current_run_id: string | null;
  n_runs_history: number;
}

export interface TunerRunRequest {
  instruments?: Instrument[];
  iter_tf?: Timeframe;
  bars?: number;
  use_claude?: boolean;
}

export async function fetchTunerStatus(): Promise<TunerStatus> {
  const res = await fetch(`${BASE}/tuner/status`, { cache: "no-store" });
  if (!res.ok) throw new Error(`tuner/status ${res.status}`);
  return res.json();
}

export async function fetchTunerHistory(limit = 20): Promise<TunerRun[]> {
  const res = await fetch(`${BASE}/tuner/history?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`tuner/history ${res.status}`);
  return res.json();
}

export async function runTuner(req: TunerRunRequest = {}): Promise<TunerRun> {
  const res = await fetch(`${BASE}/tuner/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...req, triggered_by: "manual" }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? `tuner/run ${res.status}`);
  }
  return res.json();
}
