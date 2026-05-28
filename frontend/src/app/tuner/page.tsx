"use client";

import { useEffect, useState } from "react";
import {
  fetchTunerStatus, fetchTunerHistory, runTuner,
  listProposals, applyProposal, rejectProposal,
  type TunerRun, type GridResult, type GridCombo, type Proposal, type Instrument,
} from "@/lib/api";

const INSTRUMENTS: Instrument[] = ["DE40", "NASDAQ", "SP500", "BTC"];

function fmtDate(s: string | null | undefined) {
  if (!s) return "—";
  return s.slice(0, 19).replace("T", " ");
}

function fmtDelta(v: number | null | undefined) {
  if (v === null || v === undefined) return "—";
  return (v >= 0 ? "+" : "") + v.toFixed(3) + "R";
}

// ─── Combo-Tabelle ──────────────────────────────────────────────────────

function ComboTable({ result }: { result: GridResult }) {
  const best = result.best;
  const sorted = [...result.combos].sort((a, b) => b.score - a.score);
  return (
    <div style={{ overflowX: "auto", marginTop: 10 }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
        <thead>
          <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
            {["RR", "Sweep", "Trades", "WR", "Exp R", "PF", "Sharpe", "MaxDD", "Score"].map((h) => (
              <th key={h} style={{ padding: "4px 8px", borderBottom: "1px solid var(--border)" }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((c, i) => {
            const isBest = c === best;
            const isCurrent =
              c.rr_threshold === result.current_rr_threshold &&
              c.sweep_lookback === result.current_sweep_lookback;
            return (
              <tr
                key={`${c.rr_threshold}-${c.sweep_lookback}`}
                style={{
                  background: isBest ? "rgba(34,197,94,0.10)"
                    : isCurrent ? "rgba(59,130,246,0.08)" : "transparent",
                  borderBottom: "1px solid var(--border)",
                }}
              >
                <td style={{ padding: "4px 8px", fontWeight: 600 }}>
                  {c.rr_threshold} {isBest && <span style={{ color: "var(--green)" }}>★</span>}
                  {isCurrent && <span style={{ color: "var(--accent)", marginLeft: 4 }}>●live</span>}
                </td>
                <td style={{ padding: "4px 8px" }}>{c.sweep_lookback}</td>
                <td style={{ padding: "4px 8px" }}>{c.total_trades}</td>
                <td style={{ padding: "4px 8px" }}>{(c.win_rate * 100).toFixed(1)}%</td>
                <td style={{
                  padding: "4px 8px",
                  color: c.expectancy_r > 0 ? "var(--green)" : "var(--red)",
                  fontWeight: 600,
                }}>
                  {c.expectancy_r >= 0 ? "+" : ""}{c.expectancy_r.toFixed(2)}R
                </td>
                <td style={{ padding: "4px 8px" }}>{c.profit_factor.toFixed(2)}</td>
                <td style={{ padding: "4px 8px" }}>{c.sharpe.toFixed(2)}</td>
                <td style={{ padding: "4px 8px", color: "var(--red)" }}>{c.max_drawdown_pct.toFixed(2)}%</td>
                <td style={{ padding: "4px 8px", color: "var(--text-dim)" }}>{c.score.toFixed(3)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ─── Instrument-Result-Card ─────────────────────────────────────────────

function InstrumentResult({ inst, result }: {
  inst: string;
  result: GridResult | { error: string };
}) {
  const [open, setOpen] = useState(false);

  if ("error" in result) {
    return (
      <div style={cardError}>
        <b>{inst}</b>: <span style={{ color: "var(--red)" }}>{result.error}</span>
      </div>
    );
  }

  const improvement = result.improvement_vs_current;
  const isImprovement = improvement !== null && improvement > 0.1;

  return (
    <div style={{
      ...card,
      borderColor: isImprovement ? "var(--green)" : "var(--border)",
    }}>
      <div
        onClick={() => setOpen(!open)}
        style={{ cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center" }}
      >
        <div>
          <span style={{ fontWeight: 700, fontSize: 14 }}>{inst}</span>
          <span style={{ marginLeft: 12, fontSize: 11, color: "var(--text-dim)" }}>
            {result.bars_used} {result.iter_tf}-Bars
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {result.best && (
            <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
              best: <b style={{ color: "var(--text)" }}>rr={result.best.rr_threshold} sl={result.best.sweep_lookback}</b>
              {" → "}
              <b style={{ color: result.best.expectancy_r > 0 ? "var(--green)" : "var(--red)" }}>
                {result.best.expectancy_r >= 0 ? "+" : ""}{result.best.expectancy_r.toFixed(2)}R
              </b>
            </span>
          )}
          <span style={{
            fontSize: 11, padding: "3px 8px", borderRadius: 12, fontWeight: 600,
            background: isImprovement ? "rgba(34,197,94,0.18)" : "rgba(148,163,184,0.15)",
            color: isImprovement ? "var(--green)" : "var(--text-dim)",
          }}>
            Δ {fmtDelta(improvement)}
            {result.current_too_few_trades && " (current too few trades)"}
          </span>
          <span style={{ color: "var(--text-dim)", fontSize: 12 }}>{open ? "▼" : "▶"}</span>
        </div>
      </div>
      {open && <ComboTable result={result} />}
    </div>
  );
}

// ─── Proposals-Bar ──────────────────────────────────────────────────────

function ProposalsBar({ proposalIds, refreshKey, onChanged }: {
  proposalIds: string[]; refreshKey: number; onChanged: () => void;
}) {
  const [props, setProps] = useState<Proposal[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    if (proposalIds.length === 0) { setProps([]); return; }
    let cancelled = false;
    (async () => {
      try {
        const all = await listProposals();
        if (!cancelled) {
          setProps(all.filter((p) => proposalIds.includes(p.id)));
        }
      } catch {}
    })();
    return () => { cancelled = true; };
  }, [proposalIds, refreshKey]);

  async function handle(id: string, action: "apply" | "reject") {
    setBusy(id);
    try {
      if (action === "apply") await applyProposal(id);
      else await rejectProposal(id);
      onChanged();
    } catch (e) { alert(String(e)); }
    finally { setBusy(null); }
  }

  if (props.length === 0) return null;

  return (
    <div style={{ marginTop: 12 }}>
      <h4 style={{ fontSize: 12, fontWeight: 700, color: "var(--yellow)", margin: "0 0 8px 0", textTransform: "uppercase", letterSpacing: 0.5 }}>
        Erzeugte Proposals
      </h4>
      {props.map((p) => (
        <div key={p.id} style={{
          background: "var(--bg)", borderRadius: 8, padding: 10, marginBottom: 6,
          border: `1px solid ${p.status === "pending" ? "var(--yellow)"
            : p.status === "applied" ? "var(--green)" : "var(--red)"}`,
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "start", gap: 8 }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <code style={{ fontSize: 11, color: "var(--accent)", display: "block", marginBottom: 4 }}>
                {JSON.stringify(p.diff)}
              </code>
              <div style={{ fontSize: 11, color: "var(--text-dim)", whiteSpace: "pre-wrap", lineHeight: 1.5 }}>
                {p.rationale}
              </div>
            </div>
            {p.status === "pending" ? (
              <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                <button onClick={() => handle(p.id, "apply")} disabled={busy === p.id}
                  style={{ background: "var(--green)", color: "#fff", border: "none", borderRadius: 5, padding: "4px 10px", fontSize: 11, cursor: "pointer", fontWeight: 600 }}>
                  {busy === p.id ? "..." : "✓ Apply"}
                </button>
                <button onClick={() => handle(p.id, "reject")} disabled={busy === p.id}
                  style={{ background: "transparent", color: "var(--text-dim)", border: "1px solid var(--border)", borderRadius: 5, padding: "4px 10px", fontSize: 11, cursor: "pointer" }}>
                  ✕ Reject
                </button>
              </div>
            ) : (
              <span style={{ fontSize: 11, color: p.status === "applied" ? "var(--green)" : "var(--red)" }}>
                {p.status === "applied" ? "✓ applied" : "✕ rejected"}
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Run-Card ───────────────────────────────────────────────────────────

function RunCard({ run, refreshKey, onChanged }: {
  run: TunerRun; refreshKey: number; onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ ...card, padding: "12px 16px", marginBottom: 10 }}>
      <div
        onClick={() => setOpen(!open)}
        style={{ cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16 }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12, fontSize: 13 }}>
          <span style={{
            fontSize: 10, padding: "2px 8px", borderRadius: 12, fontWeight: 600,
            background: run.status === "completed" ? "rgba(34,197,94,0.15)"
              : run.status === "failed" ? "rgba(239,68,68,0.15)"
              : "rgba(234,179,8,0.15)",
            color: run.status === "completed" ? "var(--green)"
              : run.status === "failed" ? "var(--red)" : "var(--yellow)",
          }}>
            {run.status.toUpperCase()}
          </span>
          <code style={{ fontSize: 11, color: "var(--text-dim)" }}>{run.id}</code>
          <span style={{ color: "var(--text-dim)", fontSize: 11 }}>
            {fmtDate(run.started_at)} · {run.triggered_by}
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 14, fontSize: 12 }}>
          <span style={{ color: "var(--text-dim)" }}>
            {run.instruments.length} instr · {run.proposal_ids.length} proposal{run.proposal_ids.length === 1 ? "" : "s"}
          </span>
          <span style={{ color: "var(--text-dim)" }}>{open ? "▼" : "▶"}</span>
        </div>
      </div>

      {open && (
        <div style={{ marginTop: 14 }}>
          {run.error && (
            <div style={{ color: "var(--red)", fontSize: 12, padding: 8, background: "rgba(239,68,68,0.1)", borderRadius: 6, marginBottom: 8 }}>
              {run.error}
            </div>
          )}
          {run.claude_summary && (
            <div style={{
              background: "rgba(59,130,246,0.08)", border: "1px solid rgba(59,130,246,0.3)",
              borderRadius: 8, padding: 10, fontSize: 12, marginBottom: 10, lineHeight: 1.5,
              whiteSpace: "pre-wrap",
            }}>
              <b style={{ color: "var(--accent)" }}>Claude:</b> {run.claude_summary}
            </div>
          )}
          {Object.entries(run.results).map(([inst, r]) => (
            <InstrumentResult key={inst} inst={inst} result={r as GridResult | { error: string }} />
          ))}
          <ProposalsBar proposalIds={run.proposal_ids} refreshKey={refreshKey} onChanged={onChanged} />
        </div>
      )}
    </div>
  );
}

// ─── Main Page ──────────────────────────────────────────────────────────

export default function TunerPage() {
  const [status, setStatus] = useState<{ is_running: boolean; current_run_id: string | null } | null>(null);
  const [history, setHistory] = useState<TunerRun[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  // Form
  const [selInstruments, setSelInstruments] = useState<Instrument[]>(INSTRUMENTS);
  const [bars, setBars] = useState(1000);
  const [useClaude, setUseClaude] = useState(true);

  async function reload() {
    try {
      const [s, h] = await Promise.all([fetchTunerStatus(), fetchTunerHistory(20)]);
      setStatus(s);
      setHistory(h);
    } catch {}
  }

  useEffect(() => {
    reload();
    const id = setInterval(reload, 10_000);
    return () => clearInterval(id);
  }, [refreshKey]);

  async function handleRun() {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      await runTuner({ instruments: selInstruments, bars, use_claude: useClaude });
      await reload();
      setRefreshKey((k) => k + 1);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  function toggleInstrument(inst: Instrument) {
    setSelInstruments((prev) =>
      prev.includes(inst) ? prev.filter((i) => i !== inst) : [...prev, inst]
    );
  }

  const isRunning = status?.is_running || busy;
  const estSeconds = selInstruments.length * 12 * 3; // ~3s pro Combo
  const estMin = Math.ceil(estSeconds / 60);

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0 }}>Self-Improve-Tuner</h1>
        <span style={{
          fontSize: 11, padding: "3px 10px", borderRadius: 20, fontWeight: 600,
          background: isRunning ? "rgba(234,179,8,0.15)" : "rgba(148,163,184,0.15)",
          color: isRunning ? "var(--yellow)" : "var(--text-dim)",
        }}>
          {isRunning ? "● RUNNING" : "○ IDLE"}
        </span>
      </div>

      {/* Run-Form */}
      <div style={{ ...card, marginBottom: 24 }}>
        <h2 style={{ fontSize: 14, fontWeight: 600, margin: "0 0 12px 0" }}>Tuner manuell starten</h2>
        <p style={{ fontSize: 12, color: "var(--text-dim)", marginBottom: 16, lineHeight: 1.5 }}>
          Param-Grid: <b style={{ color: "var(--text)" }}>rr_threshold</b> × <b style={{ color: "var(--text)" }}>sweep_lookback</b>{" "}
          = 4 × 3 = <b>12 Backtests/Instrument</b>. Pro Backtest ~2-3s.
          Geschätzt: <b>{selInstruments.length}× 12 Backtests ≈ {estMin} Min</b>.
        </p>

        <div style={{ marginBottom: 14 }}>
          <label style={{ fontSize: 12, color: "var(--text-dim)", display: "block", marginBottom: 6 }}>Instrumente</label>
          <div style={{ display: "flex", gap: 6 }}>
            {INSTRUMENTS.map((i) => (
              <button
                key={i}
                onClick={() => toggleInstrument(i)}
                style={{
                  background: selInstruments.includes(i) ? "var(--accent)" : "var(--bg)",
                  color: selInstruments.includes(i) ? "#fff" : "var(--text-dim)",
                  border: "1px solid var(--border)", borderRadius: 6,
                  padding: "5px 12px", fontSize: 12, fontWeight: 600, cursor: "pointer",
                }}
              >
                {i}
              </button>
            ))}
          </div>
        </div>

        <div style={{ display: "flex", gap: 16, alignItems: "end", flexWrap: "wrap" }}>
          <div>
            <label style={{ fontSize: 12, color: "var(--text-dim)", display: "block", marginBottom: 4 }}>Bars/TF</label>
            <input
              type="number"
              value={bars}
              onChange={(e) => setBars(parseInt(e.target.value) || 1000)}
              style={{
                background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 6,
                color: "var(--text)", padding: "6px 10px", fontSize: 13, width: 100,
              }}
            />
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox" id="claude" checked={useClaude}
              onChange={(e) => setUseClaude(e.target.checked)}
            />
            <label htmlFor="claude" style={{ fontSize: 13, cursor: "pointer" }}>
              Claude-Reasoning für Rationale (braucht ANTHROPIC_API_KEY)
            </label>
          </div>
          <button
            onClick={handleRun}
            disabled={isRunning || selInstruments.length === 0}
            style={{
              background: isRunning ? "var(--border)" : "var(--accent)",
              color: "#fff", border: "none", borderRadius: 8,
              padding: "8px 20px", fontSize: 13, fontWeight: 600,
              cursor: isRunning || selInstruments.length === 0 ? "default" : "pointer",
              marginLeft: "auto",
            }}
          >
            {isRunning ? "Läuft..." : "▶ Tuner-Run starten"}
          </button>
        </div>

        {err && (
          <div style={{ background: "rgba(239,68,68,0.1)", color: "var(--red)", padding: "8px 12px", borderRadius: 6, fontSize: 12, marginTop: 12 }}>
            {err}
          </div>
        )}
      </div>

      {/* Auto-Run Info */}
      <div style={{
        background: "rgba(59,130,246,0.06)", border: "1px solid rgba(59,130,246,0.2)",
        borderRadius: 10, padding: "10px 14px", fontSize: 12, color: "var(--text-dim)",
        marginBottom: 20, lineHeight: 1.6,
      }}>
        <b style={{ color: "var(--accent)" }}>Nightly-Auto-Run:</b> Tuner kann täglich um 03:00 Uhr automatisch laufen.
        Plist aktivieren via:{" "}
        <code style={{ background: "var(--bg)", padding: "2px 6px", borderRadius: 4 }}>
          launchctl load ~/trading-v2/infra/launchd/com.simonzernahle.trading-v2.tuner.plist
        </code>
      </div>

      {/* History */}
      <h2 style={{ fontSize: 16, fontWeight: 600, margin: "0 0 12px 0" }}>
        History ({history.length})
      </h2>
      {history.length === 0 && (
        <p style={{ color: "var(--text-dim)", fontSize: 13 }}>
          Noch keine Tuner-Läufe. Klick „Tuner-Run starten" oben.
        </p>
      )}
      {history.map((r) => (
        <RunCard key={r.id} run={r} refreshKey={refreshKey}
          onChanged={() => setRefreshKey((k) => k + 1)} />
      ))}
    </>
  );
}

const card = {
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: 10,
  padding: "16px 20px",
} as const;

const cardError = {
  background: "rgba(239,68,68,0.05)",
  border: "1px solid rgba(239,68,68,0.3)",
  borderRadius: 8,
  padding: "10px 16px",
  marginBottom: 8,
  fontSize: 13,
} as const;
