"use client";

export default function ChatPage() {
  return (
    <div style={{ textAlign: "center", paddingTop: 80 }}>
      <div style={{ fontSize: 40, marginBottom: 16 }}>💬</div>
      <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 12 }}>Chat-Bot</h1>
      <p style={{ color: "var(--text-dim)", maxWidth: 420, margin: "0 auto", lineHeight: 1.7 }}>
        Kommt in <b>Etappe 6</b> — Anthropic Claude API mit Tool-Use:
        <code style={{ display: "block", marginTop: 12, padding: "10px 16px", background: "var(--surface)", borderRadius: 6, fontSize: 12, textAlign: "left" }}>
          read_status · run_backtest · get_trades · propose_config_diff
        </code>
      </p>
      <p style={{ color: "var(--text-dim)", fontSize: 12, marginTop: 24 }}>
        Der Chat kann dann Backtests starten, Status erklären und Konfigurations-Änderungen vorschlagen.
      </p>
    </div>
  );
}
