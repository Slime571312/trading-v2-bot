"use client";

import { useState } from "react";
import dynamic from "next/dynamic";
import {
  runBacktest,
  type BacktestResponse,
  type BacktestRequest,
  type Instrument,
  type Timeframe,
} from "@/lib/api";

const EquityChart = dynamic(() => import("@/components/EquityChart"), { ssr: false });

const INSTRUMENTS: Instrument[] = ["DE40", "NASDAQ", "SP500", "BTC"];
const TIMEFRAMES: Timeframe[] = ["1m", "5m", "15m", "30m", "1h"];

const label = (s: string) => (
  <label style={{ fontSize: 12, color: "var(--text-dim)", display: "block", marginBottom: 4 }}>
    {s}
  </label>
);

const input = (
  value: string | number | boolean,
  onChange: (v: string) => void,
  type = "number",
  extra?: object
) => (
  <input
    type={type}
    value={String(value)}
    onChange={(e) => onChange(e.target.value)}
    style={{
      background: "var(--bg)",
      border: "1px solid var(--border)",
      borderRadius: 6,
      color: "var(--text)",
      padding: "6px 10px",
      fontSize: 13,
      width: "100%",
      outline: "none",
    }}
    {...extra}
  />
);

function MetricsGrid({ m, initial, final: finalEq }: {
  m: BacktestResponse["metrics"];
  initial: number;
  final: number;
}) {
  const returnPos = (m.total_return_pct ?? 0) >= 0;
  // Null-safe helper: Backend kann inf/NaN als null senden (Pydantic v2 default).
  const fx = (v: number | null | undefined, digits = 2, prefix = "") =>
    v === null || v === undefined || !isFinite(v) ? "—" : `${prefix}${v.toFixed(digits)}`;
  const pct = (v: number | null | undefined, digits = 1) =>
    v === null || v === undefined || !isFinite(v) ? "—" : `${v.toFixed(digits)}%`;
  const signedPct = (v: number | null | undefined, digits = 2) =>
    v === null || v === undefined || !isFinite(v) ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;
  const signedR = (v: number | null | undefined, digits = 2) =>
    v === null || v === undefined || !isFinite(v) ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(digits)}R`;

  // Profit-Factor: Backend liefert null für Inf (kein Loss überhaupt)
  const pfCell = m.profit_factor === null || m.profit_factor === undefined
    ? (m.wins > 0 && m.losses === 0 ? "∞" : "—")
    : !isFinite(m.profit_factor) ? "∞" : m.profit_factor.toFixed(2);

  const cells: [string, string][] = [
    ["Trades", `${m.total_trades} (${m.longs}L / ${m.shorts}S)`],
    ["Win-Rate", pct((m.win_rate ?? 0) * 100, 1)],
    ["Return", signedPct(m.total_return_pct)],
    ["Max DD", pct(m.max_drawdown_pct, 2)],
    ["Expectancy", signedR(m.expectancy_r)],
    ["Profit Factor", pfCell],
    ["Sharpe", fx(m.sharpe)],
    ["Exposure", pct(m.exposure_pct, 1)],
    ["Avg Win", signedR(m.avg_win_r)],
    ["Avg Loss", signedR(m.avg_loss_r)],
    ["Final Equity", `€${finalEq.toLocaleString("de-DE", { minimumFractionDigits: 2 })}`],
    ["Initial", `€${initial.toLocaleString("de-DE", { minimumFractionDigits: 2 })}`],
  ];

  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: 10 }}>
      {cells.map(([k, v]) => (
        <div
          key={k}
          style={{
            background: "var(--bg)",
            border: "1px solid var(--border)",
            borderRadius: 8,
            padding: "10px 14px",
          }}
        >
          <div style={{ fontSize: 11, color: "var(--text-dim)", marginBottom: 3 }}>{k}</div>
          <div
            style={{
              fontSize: 16,
              fontWeight: 700,
              color:
                k === "Return"
                  ? returnPos ? "var(--green)" : "var(--red)"
                  : k === "Expectancy"
                  ? m.expectancy_r >= 0 ? "var(--green)" : "var(--red)"
                  : "var(--text)",
            }}
          >
            {v}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function BacktestPage() {
  const [form, setForm] = useState<BacktestRequest>({
    instrument: "BTC",
    iter_tf: "5m",
    bars: 1000,
    initial_capital: 10000,
    rr_threshold: 2.0,
    risk_pct: 0.01,
    sweep_lookback: 10,
    with_walkforward: false,
    wfo_oos_bars: 200,
    wfo_in_sample_bars: 500,
  });
  const [result, setResult] = useState<BacktestResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tradeFilter, setTradeFilter] = useState<"all" | "long" | "short" | "win" | "loss">("all");

  const set = (k: keyof BacktestRequest) => (v: string) =>
    setForm((f) => ({ ...f, [k]: typeof f[k] === "number" ? parseFloat(v) || 0 : v }));

  // Client-seitige Vorab-Validierung — gleiche Bounds wie Backend, damit der
  // User direkt sieht was schief läuft statt eines 422 vom Server.
  function validate(f: BacktestRequest): string | null {
    if (f.bars < 200 || f.bars > 1000) return `Bars: ${f.bars} — muss 200–1000 sein`;
    if (f.initial_capital < 100) return `Start-Kapital: ${f.initial_capital} — min 100`;
    if (f.rr_threshold < 1.0 || f.rr_threshold > 5.0) return `RR-Schwelle: ${f.rr_threshold} — muss 1.0–5.0 sein`;
    if (f.risk_pct < 0.001 || f.risk_pct > 0.05) return `Risk %: ${f.risk_pct} — muss 0.001–0.05 sein (0.1%–5%)`;
    if (f.sweep_lookback < 3 || f.sweep_lookback > 50) return `Sweep-Lookback: ${f.sweep_lookback} — muss 3–50 sein`;
    if (f.with_walkforward) {
      if (f.wfo_oos_bars < 50 || f.wfo_oos_bars > 500) return `WFO OOS: ${f.wfo_oos_bars} — muss 50–500 sein`;
      if (f.wfo_in_sample_bars < 100 || f.wfo_in_sample_bars > 900) return `WFO In-Sample: ${f.wfo_in_sample_bars} — muss 100–900 sein`;
    }
    return null;
  }

  async function handleRun() {
    const validationErr = validate(form);
    if (validationErr) {
      setError(validationErr);
      return;
    }
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const r = await runBacktest(form);
      setResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  const filteredTrades = result?.trades.filter((t) => {
    if (tradeFilter === "long") return t.side === "long";
    if (tradeFilter === "short") return t.side === "short";
    if (tradeFilter === "win") return t.r_multiple > 0;
    if (tradeFilter === "loss") return t.r_multiple <= 0;
    return true;
  });

  return (
    <>
      <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 24 }}>Backtest</h1>

      {/* Formular */}
      <div
        style={{
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: 10,
          padding: "20px 24px",
          marginBottom: 24,
        }}
      >
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 16 }}>
          {/* Instrument */}
          <div style={{ minWidth: 120 }}>
            {label("Instrument")}
            <select
              value={form.instrument}
              onChange={(e) => setForm((f) => ({ ...f, instrument: e.target.value as Instrument }))}
              style={{
                background: "var(--bg)",
                border: "1px solid var(--border)",
                borderRadius: 6,
                color: "var(--text)",
                padding: "6px 10px",
                fontSize: 13,
                width: "100%",
              }}
            >
              {INSTRUMENTS.map((i) => <option key={i}>{i}</option>)}
            </select>
          </div>

          {/* Timeframe */}
          <div style={{ minWidth: 100 }}>
            {label("Timeframe")}
            <select
              value={form.iter_tf}
              onChange={(e) => setForm((f) => ({ ...f, iter_tf: e.target.value as Timeframe }))}
              style={{
                background: "var(--bg)",
                border: "1px solid var(--border)",
                borderRadius: 6,
                color: "var(--text)",
                padding: "6px 10px",
                fontSize: 13,
                width: "100%",
              }}
            >
              {TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}
            </select>
          </div>

          {/* Bars */}
          <div style={{ minWidth: 100 }}>
            {label("Bars")}
            {input(form.bars, set("bars"))}
          </div>

          {/* Capital */}
          <div style={{ minWidth: 120 }}>
            {label("Start-Kapital (€)")}
            {input(form.initial_capital, set("initial_capital"))}
          </div>

          {/* RR */}
          <div style={{ minWidth: 100 }}>
            {label("RR-Schwelle")}
            {input(form.rr_threshold, set("rr_threshold"))}
          </div>

          {/* Risk */}
          <div style={{ minWidth: 100 }}>
            {label("Risk % (0.01 = 1%)")}
            {input(form.risk_pct, set("risk_pct"))}
          </div>

          {/* Sweep Lookback */}
          <div style={{ minWidth: 110 }}>
            {label("Sweep-Lookback")}
            {input(form.sweep_lookback, set("sweep_lookback"))}
          </div>
        </div>

        {/* Walk-Forward */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
          <input
            type="checkbox"
            id="wfo"
            checked={form.with_walkforward}
            onChange={(e) => setForm((f) => ({ ...f, with_walkforward: e.target.checked }))}
          />
          <label htmlFor="wfo" style={{ fontSize: 13, cursor: "pointer" }}>Walk-Forward (rollende OOS-Fenster)</label>
          {form.with_walkforward && (
            <>
              <span style={{ marginLeft: 16, fontSize: 12, color: "var(--text-dim)" }}>In-Sample:</span>
              <input
                type="number"
                value={form.wfo_in_sample_bars}
                onChange={(e) => setForm((f) => ({ ...f, wfo_in_sample_bars: parseInt(e.target.value) }))}
                style={{
                  width: 70, background: "var(--bg)", border: "1px solid var(--border)",
                  borderRadius: 6, color: "var(--text)", padding: "4px 8px", fontSize: 13,
                }}
              />
              <span style={{ fontSize: 12, color: "var(--text-dim)" }}>OOS:</span>
              <input
                type="number"
                value={form.wfo_oos_bars}
                onChange={(e) => setForm((f) => ({ ...f, wfo_oos_bars: parseInt(e.target.value) }))}
                style={{
                  width: 70, background: "var(--bg)", border: "1px solid var(--border)",
                  borderRadius: 6, color: "var(--text)", padding: "4px 8px", fontSize: 13,
                }}
              />
            </>
          )}
        </div>

        <button
          onClick={handleRun}
          disabled={loading}
          style={{
            background: loading ? "var(--border)" : "var(--accent)",
            color: "#fff",
            border: "none",
            borderRadius: 8,
            padding: "10px 28px",
            fontSize: 14,
            fontWeight: 600,
            cursor: loading ? "default" : "pointer",
          }}
        >
          {loading ? "Läuft..." : "Backtest starten"}
        </button>
      </div>

      {error && (
        <div
          style={{
            background: "rgba(239,68,68,0.1)",
            border: "1px solid var(--red)",
            borderRadius: 8,
            padding: "12px 16px",
            color: "var(--red)",
            marginBottom: 20,
            fontSize: 13,
            display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12,
          }}
        >
          <div style={{ whiteSpace: "pre-wrap", flex: 1, fontFamily: "ui-monospace, monospace" }}>
            ⚠ {error}
          </div>
          <button
            onClick={() => {
              setForm({
                instrument: "BTC", iter_tf: "5m", bars: 1000, initial_capital: 10000,
                rr_threshold: 2.0, risk_pct: 0.01, sweep_lookback: 10,
                with_walkforward: false, wfo_oos_bars: 200, wfo_in_sample_bars: 500,
              });
              setError(null);
            }}
            style={{
              background: "transparent", border: "1px solid var(--red)", color: "var(--red)",
              padding: "3px 10px", borderRadius: 5, fontSize: 11, cursor: "pointer",
              whiteSpace: "nowrap",
            }}
          >
            ↺ Defaults
          </button>
        </div>
      )}

      {loading && (
        <div style={{ textAlign: "center", color: "var(--text-dim)", padding: "40px 0", fontSize: 14 }}>
          Backtest läuft — fetche Daten + Replay...
        </div>
      )}

      {result && !loading && (
        <>
          {/* Header */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
            <h2 style={{ fontSize: 16, fontWeight: 700, margin: 0 }}>
              {result.instrument} · {result.iter_tf} · {result.start.slice(0, 10)} → {result.end.slice(0, 10)}
            </h2>
            <a
              href={result.report_url}
              target="_blank"
              rel="noreferrer"
              style={{
                background: "var(--surface)",
                border: "1px solid var(--border)",
                color: "var(--accent)",
                padding: "6px 14px",
                borderRadius: 6,
                fontSize: 13,
                textDecoration: "none",
              }}
            >
              HTML-Report öffnen ↗
            </a>
          </div>

          {/* Metriken */}
          <div style={{ marginBottom: 20 }}>
            <MetricsGrid m={result.metrics} initial={result.initial_capital} final={result.final_equity} />
          </div>

          {/* Equity-Chart */}
          {result.equity_curve.length > 0 && (
            <div
              style={{
                background: "var(--surface)",
                border: "1px solid var(--border)",
                borderRadius: 10,
                padding: 16,
                marginBottom: 20,
              }}
            >
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>Equity-Curve</div>
              <EquityChart data={result.equity_curve} initialCapital={result.initial_capital} />
            </div>
          )}

          {/* Walk-Forward */}
          {result.walkforward && (
            <div
              style={{
                background: "var(--surface)",
                border: "1px solid var(--border)",
                borderRadius: 10,
                padding: "16px 20px",
                marginBottom: 20,
              }}
            >
              <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>
                Walk-Forward — {result.walkforward.n_windows} OOS-Fenster
              </div>
              <div style={{ display: "flex", gap: 20, flexWrap: "wrap", marginBottom: 14, fontSize: 13 }}>
                {(() => {
                  const wf = result.walkforward!;
                  const safe = (v: number | null | undefined, fmt: (n: number) => string) =>
                    v === null || v === undefined || !isFinite(v) ? "—" : fmt(v);
                  return [
                    ["Win-Rate", safe(wf.win_rate, (v) => `${(v * 100).toFixed(1)}%`)],
                    ["Avg Expectancy", safe(wf.avg_expectancy_r, (v) => `${v >= 0 ? "+" : ""}${v.toFixed(2)}R`)],
                    ["Fenster positiv", safe(wf.pct_windows_positive, (v) => `${(v * 100).toFixed(0)}%`)],
                    ["Avg Sharpe", safe(wf.avg_sharpe, (v) => v.toFixed(2))],
                    ["Avg Max DD", safe(wf.avg_max_drawdown_pct, (v) => `${v.toFixed(2)}%`)],
                  ] as [string, string][];
                })().map(([k, v]) => (
                  <div key={k}>
                    <span style={{ color: "var(--text-dim)" }}>{k}: </span>
                    <b>{v}</b>
                  </div>
                ))}
              </div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {result.walkforward.windows.map((w) => {
                  const exp = w.metrics.expectancy_r;
                  const hasExp = exp !== null && exp !== undefined && isFinite(exp);
                  const pos = hasExp && exp > 0;
                  return (
                    <div
                      key={w.window_idx}
                      title={`${w.oos_start.slice(0, 10)} → ${w.oos_end.slice(0, 10)} · ${w.n_trades} Trades`}
                      style={{
                        background: pos ? "rgba(34,197,94,0.15)" : "rgba(239,68,68,0.15)",
                        border: `1px solid ${pos ? "var(--green)" : "var(--red)"}`,
                        borderRadius: 6,
                        padding: "5px 10px",
                        fontSize: 11,
                        fontWeight: 600,
                      }}
                    >
                      #{w.window_idx + 1} {hasExp ? `${exp >= 0 ? "+" : ""}${exp.toFixed(2)}R` : "—"}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Trade-Liste */}
          {result.trades.length > 0 && (
            <div
              style={{
                background: "var(--surface)",
                border: "1px solid var(--border)",
                borderRadius: 10,
                padding: "16px 20px",
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                <div style={{ fontSize: 14, fontWeight: 600 }}>
                  Trade-Liste ({result.trades.length})
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  {(["all", "long", "short", "win", "loss"] as const).map((f) => (
                    <button
                      key={f}
                      onClick={() => setTradeFilter(f)}
                      style={{
                        background: tradeFilter === f ? "var(--accent)" : "var(--bg)",
                        border: "1px solid var(--border)",
                        borderRadius: 5,
                        color: tradeFilter === f ? "#fff" : "var(--text-dim)",
                        padding: "3px 10px",
                        fontSize: 11,
                        cursor: "pointer",
                        fontWeight: 600,
                        textTransform: "capitalize",
                      }}
                    >
                      {f}
                    </button>
                  ))}
                </div>
              </div>
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                  <thead>
                    <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
                      {["Open", "Close", "Side", "Variant", "Entry", "Exit", "SL", "TP", "R", "PnL", "Reason", "Bars"].map((h) => (
                        <th key={h} style={{ padding: "6px 10px", borderBottom: "1px solid var(--border)", fontWeight: 600 }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {filteredTrades?.map((t, i) => (
                      <tr
                        key={i}
                        style={{
                          borderBottom: "1px solid var(--border)",
                          background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.02)",
                        }}
                      >
                        <td style={{ padding: "5px 10px" }}>{t.open_time.slice(0, 16).replace("T", " ")}</td>
                        <td style={{ padding: "5px 10px" }}>{t.close_time.slice(0, 16).replace("T", " ")}</td>
                        <td style={{ padding: "5px 10px", color: t.side === "long" ? "var(--green)" : "var(--red)", fontWeight: 600 }}>{t.side}</td>
                        <td style={{ padding: "5px 10px", color: "var(--text-dim)" }}>{t.variant}</td>
                        <td style={{ padding: "5px 10px" }}>{t.entry.toFixed(2)}</td>
                        <td style={{ padding: "5px 10px" }}>{t.exit.toFixed(2)}</td>
                        <td style={{ padding: "5px 10px", color: "var(--red)" }}>{t.sl.toFixed(2)}</td>
                        <td style={{ padding: "5px 10px", color: "var(--green)" }}>{t.tp.toFixed(2)}</td>
                        <td style={{ padding: "5px 10px", color: t.r_multiple > 0 ? "var(--green)" : "var(--red)", fontWeight: 700 }}>
                          {t.r_multiple >= 0 ? "+" : ""}{t.r_multiple.toFixed(2)}R
                        </td>
                        <td style={{ padding: "5px 10px", color: t.pnl_abs >= 0 ? "var(--green)" : "var(--red)" }}>
                          {t.pnl_abs >= 0 ? "+" : ""}{t.pnl_abs.toFixed(2)}
                        </td>
                        <td style={{ padding: "5px 10px", color: "var(--text-dim)" }}>{t.exit_reason}</td>
                        <td style={{ padding: "5px 10px", color: "var(--text-dim)" }}>{t.bars_held}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {result.trades.length === 0 && (
            <div
              style={{
                background: "var(--surface)",
                border: "1px solid var(--border)",
                borderRadius: 10,
                padding: "32px 20px",
                textAlign: "center",
                color: "var(--text-dim)",
              }}
            >
              <p>Keine Trades generiert. Strategie zu restriktiv oder zu wenig historische Daten.</p>
              <p style={{ fontSize: 12, marginTop: 8 }}>
                Tipp: <code>/diagnose/{form.instrument}</code> im Backend zeigt warum kein Signal entsteht.
              </p>
            </div>
          )}
        </>
      )}
    </>
  );
}
