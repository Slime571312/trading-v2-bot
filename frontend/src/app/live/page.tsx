"use client";

import { useEffect, useMemo, useState } from "react";
import {
  fetchBotState,
  startBot,
  stopBot,
  resetBot,
  type BotState,
  type BotStartRequest,
  type Instrument,
  type OpenTrade,
  type ClosedTrade,
} from "@/lib/api";
import { useLiveFeed } from "@/lib/useLiveFeed";

const INSTRUMENTS: Instrument[] = ["DE40", "NASDAQ", "SP500", "BTC"];

function fmtMoney(v: number) {
  return v.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtDate(s: string | null) {
  if (!s) return "—";
  return s.slice(0, 19).replace("T", " ");
}

function StatusPill({ running, wsStatus }: { running: boolean; wsStatus: string }) {
  const bg = running ? "rgba(34,197,94,0.15)" : "rgba(148,163,184,0.15)";
  const fg = running ? "var(--green)" : "var(--text-dim)";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <span
        style={{
          fontSize: 11, fontWeight: 700,
          padding: "3px 10px", borderRadius: 20,
          background: bg, color: fg, letterSpacing: 0.5,
        }}
      >
        {running ? "● RUNNING" : "○ STOPPED"}
      </span>
      <span style={{ fontSize: 10, color: "var(--text-dim)" }}>WS: {wsStatus}</span>
    </div>
  );
}

function OpenTradesTable({ trades }: { trades: OpenTrade[] }) {
  if (trades.length === 0) {
    return (
      <p style={{ color: "var(--text-dim)", padding: "20px 0", fontSize: 13 }}>
        Keine offenen Positionen.
      </p>
    );
  }
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
      <thead>
        <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
          {["Instr.", "Side", "Variant", "Entry", "SL", "TP", "Size", "RR", "Open"].map((h) => (
            <th key={h} style={{ padding: "6px 8px", borderBottom: "1px solid var(--border)" }}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {trades.map((t) => (
          <tr key={t.id} style={{ borderBottom: "1px solid var(--border)" }}>
            <td style={{ padding: "5px 8px", fontWeight: 600 }}>{t.instrument}</td>
            <td
              style={{
                padding: "5px 8px",
                color: t.side === "long" ? "var(--green)" : "var(--red)",
                fontWeight: 600,
              }}
            >
              {t.side}
            </td>
            <td style={{ padding: "5px 8px", color: "var(--text-dim)" }}>{t.variant}</td>
            <td style={{ padding: "5px 8px" }}>{t.entry.toFixed(2)}</td>
            <td style={{ padding: "5px 8px", color: "var(--red)" }}>{t.sl.toFixed(2)}</td>
            <td style={{ padding: "5px 8px", color: "var(--green)" }}>{t.tp.toFixed(2)}</td>
            <td style={{ padding: "5px 8px" }}>{t.size.toFixed(4)}</td>
            <td style={{ padding: "5px 8px", color: "var(--accent)", fontWeight: 600 }}>
              {t.rr_at_open.toFixed(2)}
            </td>
            <td style={{ padding: "5px 8px", color: "var(--text-dim)" }}>{fmtDate(t.open_time)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ClosedTradesTable({ trades }: { trades: ClosedTrade[] }) {
  if (trades.length === 0) {
    return (
      <p style={{ color: "var(--text-dim)", padding: "20px 0", fontSize: 13 }}>
        Noch keine geschlossenen Trades.
      </p>
    );
  }
  const sorted = [...trades].sort(
    (a, b) => new Date(b.close_time).getTime() - new Date(a.close_time).getTime(),
  );
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
      <thead>
        <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
          {["Instr.", "Side", "Entry", "Exit", "R", "PnL", "Reason", "Closed"].map((h) => (
            <th key={h} style={{ padding: "6px 8px", borderBottom: "1px solid var(--border)" }}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sorted.map((t) => (
          <tr key={t.id} style={{ borderBottom: "1px solid var(--border)" }}>
            <td style={{ padding: "5px 8px", fontWeight: 600 }}>{t.instrument}</td>
            <td
              style={{
                padding: "5px 8px",
                color: t.side === "long" ? "var(--green)" : "var(--red)",
                fontWeight: 600,
              }}
            >
              {t.side}
            </td>
            <td style={{ padding: "5px 8px" }}>{t.entry.toFixed(2)}</td>
            <td style={{ padding: "5px 8px" }}>{t.exit.toFixed(2)}</td>
            <td
              style={{
                padding: "5px 8px",
                color: t.r_multiple > 0 ? "var(--green)" : "var(--red)",
                fontWeight: 700,
              }}
            >
              {t.r_multiple >= 0 ? "+" : ""}
              {t.r_multiple.toFixed(2)}R
            </td>
            <td
              style={{
                padding: "5px 8px",
                color: t.pnl_abs >= 0 ? "var(--green)" : "var(--red)",
                fontWeight: 600,
              }}
            >
              {t.pnl_abs >= 0 ? "+" : ""}
              {t.pnl_abs.toFixed(2)}
            </td>
            <td style={{ padding: "5px 8px", color: "var(--text-dim)" }}>{t.exit_reason}</td>
            <td style={{ padding: "5px 8px", color: "var(--text-dim)" }}>{fmtDate(t.close_time)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function LivePage() {
  const { state: liveState, status: wsStatus } = useLiveFeed();
  const [fallbackState, setFallbackState] = useState<BotState | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);

  // Form für Bot-Start
  const [form, setForm] = useState<BotStartRequest>({
    initial_capital: 10_000,
    tick_interval_s: 60,
    instruments: INSTRUMENTS,
    rr_threshold: 2.0,
    risk_pct: 0.01,
    sweep_lookback: 10,
    reset: false,
  });

  // Initial-Fetch (falls WS langsam connectet) und periodisches Sync-Fallback
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const s = await fetchBotState();
        if (!cancelled) setFallbackState(s);
      } catch {}
    }
    load();
    const id = setInterval(load, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const state = liveState ?? fallbackState;

  const pnlAbs = useMemo(() => (state ? state.equity - state.initial_capital : 0), [state]);
  const pnlPct = useMemo(
    () => (state && state.initial_capital > 0 ? (pnlAbs / state.initial_capital) * 100 : 0),
    [state, pnlAbs],
  );
  const winRate = useMemo(() => {
    if (!state || state.closed_trades.length === 0) return null;
    const wins = state.closed_trades.filter((t) => t.r_multiple > 0).length;
    return (wins / state.closed_trades.length) * 100;
  }, [state]);

  async function handleStart() {
    setBusy(true); setErr(null);
    try { await startBot(form); }
    catch (e) { setErr(String(e)); }
    finally { setBusy(false); }
  }

  async function handleStop() {
    setBusy(true); setErr(null);
    try { await stopBot(); }
    catch (e) { setErr(String(e)); }
    finally { setBusy(false); }
  }

  async function handleReset() {
    if (!confirm("Wirklich zurücksetzen? Alle Trades und Equity gehen verloren.")) return;
    setBusy(true); setErr(null);
    try { await resetBot(); }
    catch (e) { setErr(String(e)); }
    finally { setBusy(false); }
  }

  if (!state) {
    return (
      <div style={{ padding: "40px 0", textAlign: "center", color: "var(--text-dim)" }}>
        Lade Bot-State... (Backend offline?)
      </div>
    );
  }

  const running = state.running;

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0 }}>Live Paper-Bot</h1>
          <StatusPill running={running} wsStatus={wsStatus} />
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {!running && (
            <button
              onClick={handleStart} disabled={busy}
              style={btnPrimary(busy)}
            >
              {busy ? "..." : "▶ Start"}
            </button>
          )}
          {running && (
            <button
              onClick={handleStop} disabled={busy}
              style={btnDanger(busy)}
            >
              {busy ? "..." : "■ Stop"}
            </button>
          )}
          <button onClick={handleReset} disabled={busy} style={btnGhost(busy)}>Reset</button>
          <button
            onClick={() => setShowSettings(!showSettings)}
            style={btnGhost(false)}
          >
            ⚙ Settings
          </button>
        </div>
      </div>

      {err && (
        <div style={errBox}>{err}</div>
      )}
      {state.last_error && (
        <div style={{ ...errBox, background: "rgba(234,179,8,0.1)", borderColor: "var(--yellow)", color: "var(--yellow)" }}>
          Letzter Bot-Fehler: {state.last_error}
        </div>
      )}

      {/* Settings-Panel */}
      {showSettings && (
        <div style={{ ...card, marginBottom: 20 }}>
          <h3 style={{ fontSize: 14, fontWeight: 600, margin: "0 0 12px 0" }}>
            Bot-Konfiguration (für nächsten Start)
          </h3>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px,1fr))", gap: 12 }}>
            <FormField label="Start-Kapital (€)" value={form.initial_capital ?? 0}
              onChange={(v) => setForm({ ...form, initial_capital: parseFloat(v) || 10000 })} />
            <FormField label="Tick-Intervall (s)" value={form.tick_interval_s ?? 60}
              onChange={(v) => setForm({ ...form, tick_interval_s: parseInt(v) || 60 })} />
            <FormField label="RR-Schwelle" value={form.rr_threshold ?? 2.0}
              onChange={(v) => setForm({ ...form, rr_threshold: parseFloat(v) || 2.0 })} step="0.1" />
            <FormField label="Risk % (0.01 = 1%)" value={form.risk_pct ?? 0.01}
              onChange={(v) => setForm({ ...form, risk_pct: parseFloat(v) || 0.01 })} step="0.001" />
            <FormField label="Sweep-Lookback" value={form.sweep_lookback ?? 10}
              onChange={(v) => setForm({ ...form, sweep_lookback: parseInt(v) || 10 })} />
            <div>
              <label style={{ fontSize: 12, color: "var(--text-dim)", display: "block", marginBottom: 4 }}>
                Reset vor Start
              </label>
              <input
                type="checkbox"
                checked={form.reset || false}
                onChange={(e) => setForm({ ...form, reset: e.target.checked })}
                style={{ marginTop: 6 }}
              />
            </div>
          </div>
        </div>
      )}

      {/* Stats-Cards */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 20 }}>
        <StatCard label="Equity" value={`€${fmtMoney(state.equity)}`} />
        <StatCard
          label="P&L"
          value={`${pnlAbs >= 0 ? "+" : ""}€${fmtMoney(pnlAbs)} (${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%)`}
          color={pnlAbs >= 0 ? "var(--green)" : "var(--red)"}
        />
        <StatCard label="Offene Trades" value={String(state.open_trades.length)} />
        <StatCard label="Trades gesamt" value={String(state.closed_trades.length)} />
        <StatCard label="Win-Rate" value={winRate === null ? "—" : `${winRate.toFixed(1)}%`} />
        <StatCard label="Tick" value={state.tick_interval_s + "s"} />
      </div>

      {/* Run-Info */}
      <div style={{ ...card, marginBottom: 20, fontSize: 12, color: "var(--text-dim)" }}>
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
          <span>Gestartet: <b style={{ color: "var(--text)" }}>{fmtDate(state.started_at)}</b></span>
          <span>Letzter Tick: <b style={{ color: "var(--text)" }}>{fmtDate(state.last_tick)}</b></span>
          <span>Letzte Signal-Prüfung: <b style={{ color: "var(--text)" }}>{fmtDate(state.last_signal_check)}</b></span>
          <span>RR≥<b style={{ color: "var(--text)" }}>{state.rr_threshold}</b></span>
          <span>Risk <b style={{ color: "var(--text)" }}>{(state.risk_pct * 100).toFixed(1)}%</b></span>
          <span>Instr.: <b style={{ color: "var(--text)" }}>{state.instruments.join(", ")}</b></span>
        </div>
      </div>

      {/* Offene Trades */}
      <div style={{ ...card, marginBottom: 20 }}>
        <h2 style={{ fontSize: 14, fontWeight: 600, margin: "0 0 12px 0" }}>
          Offene Positionen ({state.open_trades.length})
        </h2>
        <OpenTradesTable trades={state.open_trades} />
      </div>

      {/* Geschlossene Trades */}
      <div style={card}>
        <h2 style={{ fontSize: 14, fontWeight: 600, margin: "0 0 12px 0" }}>
          Geschlossen ({state.closed_trades.length})
        </h2>
        <ClosedTradesTable trades={state.closed_trades} />
      </div>
    </>
  );
}

// ─── helpers ──────────────────────────────────────────────────────────

function StatCard({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 10,
        padding: "14px 18px",
        minWidth: 140,
        flex: "0 1 auto",
      }}
    >
      <div style={{ fontSize: 11, color: "var(--text-dim)", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: color ?? "var(--text)" }}>{value}</div>
    </div>
  );
}

function FormField({
  label, value, onChange, step,
}: {
  label: string; value: number | string;
  onChange: (v: string) => void; step?: string;
}) {
  return (
    <div>
      <label style={{ fontSize: 12, color: "var(--text-dim)", display: "block", marginBottom: 4 }}>{label}</label>
      <input
        type="number"
        step={step}
        value={String(value)}
        onChange={(e) => onChange(e.target.value)}
        style={{
          background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 6,
          color: "var(--text)", padding: "6px 10px", fontSize: 13, width: "100%",
        }}
      />
    </div>
  );
}

const card = {
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: 10,
  padding: "16px 20px",
} as const;

const errBox = {
  background: "rgba(239,68,68,0.1)",
  border: "1px solid var(--red)",
  borderRadius: 8,
  padding: "10px 14px",
  color: "var(--red)",
  fontSize: 13,
  marginBottom: 16,
} as const;

const btnPrimary = (busy: boolean) => ({
  background: busy ? "var(--border)" : "var(--green)",
  color: "#fff", border: "none", borderRadius: 8,
  padding: "8px 18px", fontSize: 13, fontWeight: 600,
  cursor: busy ? "default" : "pointer",
});

const btnDanger = (busy: boolean) => ({
  background: busy ? "var(--border)" : "var(--red)",
  color: "#fff", border: "none", borderRadius: 8,
  padding: "8px 18px", fontSize: 13, fontWeight: 600,
  cursor: busy ? "default" : "pointer",
});

const btnGhost = (busy: boolean) => ({
  background: "transparent", color: "var(--text-dim)",
  border: "1px solid var(--border)", borderRadius: 8,
  padding: "8px 14px", fontSize: 13, fontWeight: 500,
  cursor: busy ? "default" : "pointer",
});
