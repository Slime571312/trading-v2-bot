"use client";

import { useEffect, useState, useCallback } from "react";
import { fetchSignal, fetchHealth, type SignalResponse, type HealthResponse, type Instrument } from "@/lib/api";

const INSTRUMENTS: Instrument[] = ["DE40", "NASDAQ", "SP500", "BTC"];

const SIDE_COLOR: Record<string, string> = {
  long: "var(--green)",
  short: "var(--red)",
  none: "var(--text-dim)",
};

const BIAS_EMOJI: Record<string, string> = {
  long: "⬆",
  short: "⬇",
  neutral: "↔",
};

function SignalCard({ instrument }: { instrument: Instrument }) {
  const [signal, setSignal] = useState<SignalResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const s = await fetchSignal(instrument);
        if (!cancelled) { setSignal(s); setError(null); }
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    const id = setInterval(load, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [instrument]);

  return (
    <div
      style={{
        background: "var(--surface)",
        border: `1px solid ${signal?.side && signal.side !== "none" ? SIDE_COLOR[signal.side] : "var(--border)"}`,
        borderRadius: 10,
        padding: "18px 20px",
        minWidth: 220,
        flex: "1 1 220px",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <span style={{ fontWeight: 700, fontSize: 16 }}>{instrument}</span>
        {signal && (
          <span style={{ fontSize: 11, color: "var(--text-dim)", fontWeight: 600 }}>
            {BIAS_EMOJI[signal.bias_direction]} {signal.bias_direction.toUpperCase()}
          </span>
        )}
      </div>

      {loading && <p style={{ color: "var(--text-dim)", fontSize: 13 }}>Lädt...</p>}
      {error && <p style={{ color: "var(--red)", fontSize: 12 }}>Fehler: Backend offline?</p>}

      {signal && !loading && (
        <>
          <div
            style={{
              fontSize: 22,
              fontWeight: 800,
              color: SIDE_COLOR[signal.side],
              marginBottom: 6,
            }}
          >
            {signal.side === "none" ? "—" : signal.side.toUpperCase()}
            {signal.variant && (
              <span style={{ fontSize: 12, fontWeight: 500, marginLeft: 8, color: "var(--text-dim)" }}>
                {signal.variant}
              </span>
            )}
          </div>

          {signal.side !== "none" && signal.entry !== null && (
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

          {signal.side === "none" && (
            <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 4 }}>
              {signal.reason}
            </p>
          )}
        </>
      )}
    </div>
  );
}

export default function DashboardPage() {
  const [health, setHealth] = useState<HealthResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function loadHealth() {
      try {
        const h = await fetchHealth();
        if (!cancelled) setHealth(h);
      } catch {}
    }
    loadHealth();
    const id = setInterval(loadHealth, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 24 }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0 }}>Dashboard</h1>
        <span
          style={{
            fontSize: 11,
            fontWeight: 600,
            padding: "2px 8px",
            borderRadius: 20,
            background: health?.status === "ok" ? "rgba(34,197,94,0.15)" : "rgba(239,68,68,0.15)",
            color: health?.status === "ok" ? "var(--green)" : "var(--red)",
          }}
        >
          {health?.status === "ok" ? "Backend online" : "Backend offline"}
        </span>
      </div>

      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 32 }}>
        {INSTRUMENTS.map((inst) => (
          <SignalCard key={inst} instrument={inst} />
        ))}
      </div>

      <div
        style={{
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: 10,
          padding: "16px 20px",
          fontSize: 13,
          color: "var(--text-dim)",
        }}
      >
        <p style={{ margin: 0 }}>
          Signals aktualisieren sich alle 30s automatisch. Klicke <a href="/backtest" style={{ color: "var(--accent)" }}>Backtest</a> um historische Performance zu prüfen,
          oder <a href="/live" style={{ color: "var(--accent)" }}>Live</a> für Paper-Trading-Status (Etappe 5).
        </p>
      </div>
    </>
  );
}
