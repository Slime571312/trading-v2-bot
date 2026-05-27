"use client";

export default function LivePage() {
  return (
    <div style={{ textAlign: "center", paddingTop: 80 }}>
      <div style={{ fontSize: 40, marginBottom: 16 }}>🤖</div>
      <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 12 }}>Live Paper-Bot</h1>
      <p style={{ color: "var(--text-dim)", maxWidth: 400, margin: "0 auto", lineHeight: 1.7 }}>
        Kommt in <b>Etappe 5</b> — Capital.com WebSocket-Stream → engine.py → Demo-Orders → State-Persist → WS-Push.
      </p>
      <p style={{ color: "var(--text-dim)", fontSize: 12, marginTop: 24 }}>
        Aktuell: <a href="/backtest" style={{ color: "var(--accent)" }}>Backtest</a> nutzen um historische Strategie-Performance zu prüfen.
      </p>
    </div>
  );
}
