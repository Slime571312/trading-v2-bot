"use client";

import { useEffect, useMemo, useState } from "react";
import {
  fetchBotOverview, startBot, stopBot, resetBot,
  startAllBots, stopAllBots, fetchTickLog,
  type BotInstance, type BotOverview, type Instrument, type TickLogEntry,
} from "@/lib/api";
import { useLiveFeed } from "@/lib/useLiveFeed";

const INSTRUMENTS: Instrument[] = ["DE40", "NASDAQ", "SP500", "BTC"];

function fmtMoney(v: number) {
  return v.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtDate(s: string | null | undefined) {
  if (!s) return "—";
  return s.slice(11, 19);
}

const ACTION_COLOR: Record<string, string> = {
  open: "var(--green)",
  close: "var(--accent)",
  skip: "var(--text-dim)",
  eval: "var(--text-dim)",
  error: "var(--red)",
  outside_session: "var(--yellow)",
};

// ─── Tick-Log Modal ─────────────────────────────────────────────────────

function TickLogModal({ instrument, onClose }: { instrument: Instrument; onClose: () => void }) {
  const [ticks, setTicks] = useState<TickLogEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const t = await fetchTickLog(instrument, 100);
        if (!cancelled) { setTicks(t); setLoading(false); }
      } catch (e) { console.error(e); }
    }
    load();
    const id = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [instrument]);

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg)", border: "1px solid var(--border)",
          borderRadius: 12, padding: 0, maxWidth: 900, width: "90%",
          maxHeight: "85vh", display: "flex", flexDirection: "column",
        }}
      >
        <div style={{
          display: "flex", justifyContent: "space-between", alignItems: "center",
          padding: "14px 20px", borderBottom: "1px solid var(--border)",
        }}>
          <div>
            <h2 style={{ fontSize: 16, fontWeight: 700, margin: 0 }}>{instrument} — Bot-Reasoning</h2>
            <p style={{ fontSize: 11, color: "var(--text-dim)", margin: "2px 0 0 0" }}>
              Warum hat der Bot was gemacht (Tick-Log, neueste zuerst, alle 5s refresht)
            </p>
          </div>
          <button onClick={onClose} style={{
            background: "transparent", border: "1px solid var(--border)",
            color: "var(--text-dim)", padding: "4px 12px", borderRadius: 6,
            cursor: "pointer", fontSize: 12,
          }}>✕ Close</button>
        </div>
        <div style={{ overflow: "auto", padding: "12px 20px", flex: 1 }}>
          {loading && <p style={{ color: "var(--text-dim)" }}>Lädt...</p>}
          {!loading && ticks.length === 0 && (
            <p style={{ color: "var(--text-dim)", fontSize: 13 }}>
              Noch keine Tick-Logs. Bot starten und einen Tick abwarten.
            </p>
          )}
          {ticks.map((t, i) => (
            <div key={i} style={{
              padding: "8px 0", borderBottom: "1px solid var(--border)",
              display: "flex", gap: 12, fontSize: 12,
            }}>
              <span style={{ color: "var(--text-dim)", width: 70, flexShrink: 0 }}>
                {fmtDate(t.timestamp)}
              </span>
              <span style={{
                color: ACTION_COLOR[t.action] ?? "var(--text)",
                fontWeight: 700, width: 70, flexShrink: 0,
              }}>
                [{t.action}]
              </span>
              <div style={{ flex: 1 }}>
                <div style={{ color: "var(--text)" }}>
                  <b>{t.decision}</b>
                  {t.detail && (
                    <span style={{ color: "var(--text-dim)", marginLeft: 8 }}>
                      — {t.detail}
                    </span>
                  )}
                </div>
                {(t.bias || t.htf_used || t.rr_computed !== null) && (
                  <div style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 2 }}>
                    {t.bias && <span>bias: <b style={{ color: "var(--text)" }}>{t.bias}</b></span>}
                    {t.htf_used && <span>{" · "}HTF: <b style={{ color: "var(--text)" }}>{t.htf_used}</b></span>}
                    {t.ltf_used && <span>{" · "}LTF: <b style={{ color: "var(--text)" }}>{t.ltf_used}</b></span>}
                    {t.sweep_direction && <span>{" · "}sweep: <b style={{ color: "var(--text)" }}>{t.sweep_direction}</b></span>}
                    {t.rr_computed !== null && <span>{" · "}RR: <b style={{ color: "var(--accent)" }}>{t.rr_computed.toFixed(2)}</b></span>}
                    {t.variant && <span>{" · "}variant: <b style={{ color: "var(--text)" }}>{t.variant}</b></span>}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── Single Bot-Card ────────────────────────────────────────────────────

function BotCard({ inst, onChange, onShowTicks }: {
  inst: BotInstance;
  onChange: () => Promise<void>;
  onShowTicks: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);

  async function action(fn: () => Promise<unknown>, label: string) {
    setBusy(label);
    try { await fn(); await onChange(); }
    catch (e) { alert(String(e)); }
    finally { setBusy(null); }
  }

  const pnl = inst.equity - inst.initial_capital;
  const pnlPct = inst.initial_capital > 0 ? (pnl / inst.initial_capital) * 100 : 0;
  const wins = inst.closed_trades.filter((t) => t.r_multiple > 0).length;
  const wr = inst.closed_trades.length > 0 ? (wins / inst.closed_trades.length) * 100 : null;
  const isRunning = inst.running;

  return (
    <div style={{
      background: "var(--surface)",
      border: `1px solid ${isRunning ? "var(--green)" : "var(--border)"}`,
      borderRadius: 10, padding: "14px 18px",
      flex: "1 1 280px", minWidth: 280,
    }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontWeight: 700, fontSize: 18 }}>{inst.instrument}</span>
          <span style={{
            fontSize: 10, fontWeight: 700, padding: "2px 7px", borderRadius: 12,
            background: isRunning ? "rgba(34,197,94,0.15)" : "rgba(148,163,184,0.15)",
            color: isRunning ? "var(--green)" : "var(--text-dim)",
          }}>
            {isRunning ? "● RUN" : "○"}
          </span>
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          {!isRunning && (
            <button onClick={() => action(() => startBot(inst.instrument as Instrument), "start")}
              disabled={!!busy}
              style={btn("var(--green)")}>
              {busy === "start" ? "..." : "▶"}
            </button>
          )}
          {isRunning && (
            <button onClick={() => action(() => stopBot(inst.instrument as Instrument), "stop")}
              disabled={!!busy}
              style={btn("var(--red)")}>
              {busy === "stop" ? "..." : "■"}
            </button>
          )}
          <button onClick={() => {
            if (confirm(`${inst.instrument} zurücksetzen? Equity + Trades gehen verloren.`)) {
              action(() => resetBot(inst.instrument as Instrument), "reset");
            }
          }} disabled={!!busy} style={btn("transparent", "var(--text-dim)")}>
            ↺
          </button>
        </div>
      </div>

      {/* Equity + P&L */}
      <div style={{ marginBottom: 10 }}>
        <div style={{ fontSize: 22, fontWeight: 800 }}>€{fmtMoney(inst.equity)}</div>
        <div style={{ fontSize: 12, color: pnl >= 0 ? "var(--green)" : "var(--red)" }}>
          {pnl >= 0 ? "+" : ""}€{fmtMoney(pnl)} ({pnlPct >= 0 ? "+" : ""}{pnlPct.toFixed(2)}%)
        </div>
      </div>

      {/* Open Trade */}
      {inst.open_trades.length > 0 && (
        <div style={{
          background: "var(--bg)", border: "1px solid var(--border)",
          borderRadius: 6, padding: "6px 10px", marginBottom: 10, fontSize: 11,
        }}>
          <div style={{
            color: inst.open_trades[0].side === "long" ? "var(--green)" : "var(--red)",
            fontWeight: 700, marginBottom: 2,
          }}>
            OFFEN: {inst.open_trades[0].side.toUpperCase()} @ {inst.open_trades[0].entry.toFixed(2)}
          </div>
          <div style={{ color: "var(--text-dim)" }}>
            SL <span style={{ color: "var(--red)" }}>{inst.open_trades[0].sl.toFixed(2)}</span>
            {" · "}TP <span style={{ color: "var(--green)" }}>{inst.open_trades[0].tp.toFixed(2)}</span>
            {" · "}RR <span style={{ color: "var(--accent)" }}>{inst.open_trades[0].rr_at_open.toFixed(2)}</span>
          </div>
        </div>
      )}

      {/* Stats */}
      <div style={{ display: "flex", gap: 12, fontSize: 11, color: "var(--text-dim)", marginBottom: 10 }}>
        <span>Trades: <b style={{ color: "var(--text)" }}>{inst.closed_trades.length}</b></span>
        {wr !== null && <span>WR: <b style={{ color: "var(--text)" }}>{wr.toFixed(0)}%</b></span>}
        <span>RR≥<b style={{ color: "var(--text)" }}>{inst.rr_threshold}</b></span>
        <span>Risk <b style={{ color: "var(--text)" }}>{(inst.risk_pct * 100).toFixed(1)}%</b></span>
      </div>

      {/* Action Row */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 10, color: "var(--text-dim)" }}>
          tick: {fmtDate(inst.last_tick)}{inst.tick_interval_s ? ` (${inst.tick_interval_s}s)` : ""}
        </span>
        <button onClick={onShowTicks} style={{
          background: "transparent", border: "1px solid var(--border)",
          color: "var(--accent)", padding: "3px 8px", borderRadius: 5,
          fontSize: 10, cursor: "pointer",
        }}>
          🔍 Reasoning ({inst.n_tick_log})
        </button>
      </div>

      {inst.last_error && (
        <div style={{
          marginTop: 8, padding: "4px 8px",
          background: "rgba(239,68,68,0.1)", border: "1px solid var(--red)",
          borderRadius: 4, fontSize: 10, color: "var(--red)",
        }}>
          {inst.last_error}
        </div>
      )}
    </div>
  );
}

// ─── Closed-Trades-Tabelle (alle Instanzen) ─────────────────────────────

function AllClosedTrades({ instances }: { instances: BotInstance[] }) {
  const all = instances.flatMap((inst) => inst.closed_trades);
  const sorted = [...all].sort((a, b) =>
    new Date(b.close_time).getTime() - new Date(a.close_time).getTime(),
  );
  if (sorted.length === 0) {
    return <p style={{ color: "var(--text-dim)", padding: "20px 0", fontSize: 13 }}>
      Noch keine geschlossenen Trades.
    </p>;
  }
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
      <thead>
        <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
          {["Instr", "Side", "Entry", "Exit", "R", "PnL", "Reason", "Closed"].map((h) => (
            <th key={h} style={{ padding: "6px 8px", borderBottom: "1px solid var(--border)" }}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sorted.slice(0, 30).map((t) => (
          <tr key={t.id} style={{ borderBottom: "1px solid var(--border)" }}>
            <td style={{ padding: "5px 8px", fontWeight: 600 }}>{t.instrument}</td>
            <td style={{ padding: "5px 8px", color: t.side === "long" ? "var(--green)" : "var(--red)", fontWeight: 600 }}>{t.side}</td>
            <td style={{ padding: "5px 8px" }}>{t.entry.toFixed(2)}</td>
            <td style={{ padding: "5px 8px" }}>{t.exit.toFixed(2)}</td>
            <td style={{ padding: "5px 8px", color: t.r_multiple > 0 ? "var(--green)" : "var(--red)", fontWeight: 700 }}>
              {t.r_multiple >= 0 ? "+" : ""}{t.r_multiple.toFixed(2)}R
            </td>
            <td style={{ padding: "5px 8px", color: t.pnl_abs >= 0 ? "var(--green)" : "var(--red)" }}>
              {t.pnl_abs >= 0 ? "+" : ""}{t.pnl_abs.toFixed(2)}
            </td>
            <td style={{ padding: "5px 8px", color: "var(--text-dim)" }}>{t.exit_reason}</td>
            <td style={{ padding: "5px 8px", color: "var(--text-dim)" }}>{t.close_time.slice(0, 16).replace("T", " ")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ─── Main Page ──────────────────────────────────────────────────────────

export default function LivePage() {
  const { instances: liveInstances, status: wsStatus } = useLiveFeed();
  const [fallback, setFallback] = useState<BotOverview | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [tickModalFor, setTickModalFor] = useState<Instrument | null>(null);

  async function reload() {
    try {
      const o = await fetchBotOverview();
      setFallback(o);
    } catch {}
  }

  useEffect(() => {
    reload();
    const id = setInterval(reload, 15_000);
    return () => clearInterval(id);
  }, []);

  // WS hat Vorrang, sonst Fallback aus REST
  const instances: BotInstance[] = useMemo(() => {
    if (Object.keys(liveInstances).length > 0) {
      return INSTRUMENTS.map((i) => liveInstances[i]).filter(Boolean);
    }
    return fallback?.instances ?? [];
  }, [liveInstances, fallback]);

  const totalEquity = instances.reduce((sum, i) => sum + i.equity, 0);
  const totalInitial = instances.reduce((sum, i) => sum + i.initial_capital, 0);
  const totalPnl = totalEquity - totalInitial;
  const totalPnlPct = totalInitial > 0 ? (totalPnl / totalInitial) * 100 : 0;
  const runningCount = instances.filter((i) => i.running).length;
  const totalOpen = instances.reduce((s, i) => s + i.open_trades.length, 0);
  const totalClosed = instances.reduce((s, i) => s + i.closed_trades.length, 0);

  async function handleStartAll() {
    setBusy(true); setErr(null);
    try { await startAllBots(); await reload(); }
    catch (e) { setErr(String(e)); }
    finally { setBusy(false); }
  }
  async function handleStopAll() {
    setBusy(true); setErr(null);
    try { await stopAllBots(); await reload(); }
    catch (e) { setErr(String(e)); }
    finally { setBusy(false); }
  }

  if (instances.length === 0) {
    return <p style={{ color: "var(--text-dim)" }}>Lade Bot-Status... (Backend offline?)</p>;
  }

  return (
    <>
      {tickModalFor && (
        <TickLogModal instrument={tickModalFor} onClose={() => setTickModalFor(null)} />
      )}

      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20, gap: 12, flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0 }}>Live Paper-Bots</h1>
          <span style={{
            fontSize: 11, fontWeight: 600, padding: "3px 10px", borderRadius: 20,
            background: runningCount > 0 ? "rgba(34,197,94,0.15)" : "rgba(148,163,184,0.15)",
            color: runningCount > 0 ? "var(--green)" : "var(--text-dim)",
          }}>
            {runningCount > 0 ? `● ${runningCount}/${instances.length} RUN` : "○ ALL STOPPED"}
          </span>
          <span style={{ fontSize: 10, color: "var(--text-dim)" }}>WS: {wsStatus}</span>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button onClick={handleStartAll} disabled={busy || runningCount === instances.length}
            style={btnLg("var(--green)")}>
            ▶ Start all
          </button>
          <button onClick={handleStopAll} disabled={busy || runningCount === 0}
            style={btnLg("var(--red)")}>
            ■ Stop all
          </button>
        </div>
      </div>

      {err && (
        <div style={{ background: "rgba(239,68,68,0.1)", border: "1px solid var(--red)", color: "var(--red)", padding: "8px 12px", borderRadius: 6, fontSize: 12, marginBottom: 12 }}>
          {err}
        </div>
      )}

      {/* Totals */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 20 }}>
        <StatCard label="Equity (gesamt)" value={`€${fmtMoney(totalEquity)}`} />
        <StatCard label="P&L (gesamt)"
          value={`${totalPnl >= 0 ? "+" : ""}€${fmtMoney(totalPnl)} (${totalPnlPct >= 0 ? "+" : ""}${totalPnlPct.toFixed(2)}%)`}
          color={totalPnl >= 0 ? "var(--green)" : "var(--red)"} />
        <StatCard label="Offene Positionen" value={String(totalOpen)} />
        <StatCard label="Trades gesamt" value={String(totalClosed)} />
      </div>

      {/* Bot-Cards (4 nebeneinander auf großen Screens) */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 24 }}>
        {instances.map((inst) => (
          <BotCard key={inst.instrument} inst={inst}
            onChange={reload}
            onShowTicks={() => setTickModalFor(inst.instrument as Instrument)} />
        ))}
      </div>

      {/* All Closed Trades */}
      <div style={{
        background: "var(--surface)", border: "1px solid var(--border)",
        borderRadius: 10, padding: "16px 20px",
      }}>
        <h2 style={{ fontSize: 14, fontWeight: 600, margin: "0 0 12px 0" }}>
          Letzte Trades (alle Instanzen)
        </h2>
        <AllClosedTrades instances={instances} />
      </div>
    </>
  );
}

// ─── helpers ──────────────────────────────────────────────────────────

function StatCard({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{
      background: "var(--surface)", border: "1px solid var(--border)",
      borderRadius: 10, padding: "14px 18px", minWidth: 140, flex: "0 1 auto",
    }}>
      <div style={{ fontSize: 11, color: "var(--text-dim)", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: color ?? "var(--text)" }}>{value}</div>
    </div>
  );
}

const btn = (bg: string, fg = "#fff") => ({
  background: bg, color: fg, border: bg === "transparent" ? "1px solid var(--border)" : "none",
  borderRadius: 5, padding: "4px 8px", fontSize: 12, fontWeight: 700,
  cursor: "pointer", minWidth: 28,
});

const btnLg = (bg: string) => ({
  background: bg, color: "#fff", border: "none", borderRadius: 8,
  padding: "8px 18px", fontSize: 13, fontWeight: 600, cursor: "pointer",
});
