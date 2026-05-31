"use client";

import { useEffect, useState } from "react";
import { fetchSignalsBatch, fetchHealth, refreshSignals, type CachedSignal, type HealthResponse, type Instrument } from "@/lib/api";

const INSTRUMENTS: Instrument[] = ["DE40", "NASDAQ", "SP500", "BTC"];

const SIDE_COLOR: Record<string, string> = {
  long: "var(--green)",
  short: "var(--red)",
  none: "var(--text-dim)",
  error: "var(--yellow)",
};

const BIAS_EMOJI: Record<string, string> = {
  long: "⬆",
  short: "⬇",
  neutral: "↔",
};

function SignalCard({ signal, loading }: { signal: CachedSignal | undefined; loading: boolean }) {
  const instrument = signal?.instrument;

  return (
    <div
      style={{
        background: "var(--surface)",
        border: `1px solid ${signal?.side && signal.side !== "none" && signal.side !== "error" ? SIDE_COLOR[signal.side] : "var(--border)"}`,
        borderRadius: 10,
        padding: "18px 20px",
        minWidth: 220,
        flex: "1 1 220px",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <span style={{ fontWeight: 700, fontSize: 16 }}>{instrument ?? "—"}</span>
        {signal && (
          <span style={{ fontSize: 11, color: "var(--text-dim)", fontWeight: 600 }}>
            {BIAS_EMOJI[signal.bias_direction]} {signal.bias_direction.toUpperCase()}
          </span>
        )}
      </div>

      {loading && !signal && <p style={{ color: "var(--text-dim)", fontSize: 13 }}>Lädt...</p>}

      {signal && (
        <>
          <div
            style={{
              fontSize: 22,
              fontWeight: 800,
              color: SIDE_COLOR[signal.side] ?? "var(--text-dim)",
              marginBottom: 6,
            }}
          >
            {signal.side === "none" ? "—" : signal.side === "error" ? "ERR" : signal.side.toUpperCase()}
            {signal.variant && (
              <span style={{ fontSize: 12, fontWeight: 500, marginLeft: 8, color: "var(--text-dim)" }}>
                {signal.variant}
              </span>
            )}
          </div>

          {signal.side !== "none" && signal.side !== "error" && signal.entry !== null && (
            <div style={{ fontSize: 12, color: "var(--text-dim)", lineHeight: 1.8 }}>
              <span>Entry: <b style={{ color: "var(--text)" }}>{signal.entry?.toFixed(2)}</b></span>
              {"  "}
              <span>SL: <b style={{ color: "var(--red)" }}>{signal.sl?.toFixed(2)}</b></span>
              {"  "}
              <span>TP: <b style={{ color: "var(--green)" }}>{signal.tp?.toFixed(2)}</b></span>
              <br />
              <span>RR: <b style={{ color: "var(--accent)" }}>{signal.rr?.toFixed(2)}</b></span>
              {"  "}
              <span>HTF: {signal.htf_used} → LTF: {signal.ltf_used}</span>
            </div>
          )}

          {(signal.side === "none" || signal.side === "error") && (
            <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 4 }}>
              {signal.reason}
            </p>
          )}

          {signal.refreshed_at && (
            <p style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 6, textAlign: "right" }}>
              Cache: {new Date(signal.refreshed_at).toLocaleTimeString("de-DE")}
            </p>
          )}
        </>
      )}
    </div>
  );
}

export default function DashboardPage() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [signals, setSignals] = useState<CachedSignal[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  async function loadSignals() {
    try {
      const s = await fetchSignalsBatch();
      setSignals(s);
    } catch {}
    finally { setLoading(false); }
  }

  useEffect(() => {
    let cancelled = false;
    async function loadHealth() {
      try {
        const h = await fetchHealth();
        if (!cancelled) setHealth(h);
      } catch {}
    }
    loadHealth();
    loadSignals();
    const id = setInterval(() => { if (!cancelled) { loadHealth(); loadSignals(); } }, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  async function handleRefresh() {
    setRefreshing(true);
    try { await refreshSignals(); await loadSignals(); }
    finally { setRefreshing(false); }
  }

  const signalsByInst = Object.fromEntries(signals.map((s) => [s.instrument, s]));

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24, gap: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0 }}>Dashboard</h1>
          <span style={{
            fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 20,
            background: health?.status === "ok" ? "rgba(34,197,94,0.15)" : "rgba(239,68,68,0.15)",
            color: health?.status === "ok" ? "var(--green)" : "var(--red)",
          }}>
            {health?.status === "ok" ? "Backend online" : "Backend offline"}
          </span>
          {health?.bot_running && (
            <span style={{
              fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 20,
              background: "rgba(59,130,246,0.15)", color: "var(--accent)",
            }}>
              ● Bot läuft
            </span>
          )}
        </div>
        <button onClick={handleRefresh} disabled={refreshing}
          style={{
            background: "var(--surface)", border: "1px solid var(--border)",
            color: "var(--text-dim)", padding: "5px 12px", borderRadius: 6,
            fontSize: 12, cursor: refreshing ? "default" : "pointer",
          }}>
          {refreshing ? "Refresht..." : "🔄 Signals neu fetchen"}
        </button>
      </div>

      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 32 }}>
        {INSTRUMENTS.map((inst) => (
          <SignalCard key={inst} signal={signalsByInst[inst]} loading={loading} />
        ))}
      </div>

      <div style={{
        background: "var(--surface)", border: "1px solid var(--border)",
        borderRadius: 10, padding: "16px 20px",
        fontSize: 13, color: "var(--text-dim)",
      }}>
        <p style={{ margin: 0 }}>
          Signals kommen aus Backend-Cache (refresht alle 60s). Auf <a href="/live" style={{ color: "var(--accent)" }}>Live</a> kannst du Bots pro Instrument starten/stoppen,
          auf <a href="/backtest" style={{ color: "var(--accent)" }}>Backtest</a> historische Performance prüfen,
          auf <a href="/tuner" style={{ color: "var(--accent)" }}>Tuner</a> Param-Optimierung.
        </p>
      </div>
    </>
  );
}
