"use client";

import { useEffect, useRef, useState } from "react";
import {
  sendChatMessage,
  listConversations,
  getConversation,
  deleteConversation,
  listProposals,
  applyProposal,
  rejectProposal,
  type ChatModel,
  type ConversationSummary,
  type ConversationDetail,
  type Proposal,
  type ToolCall,
} from "@/lib/api";

const MODELS: { value: ChatModel | ""; label: string; hint: string }[] = [
  { value: "", label: "Default", hint: "Was deine Subscription liefert" },
  { value: "sonnet", label: "Sonnet", hint: "Schnell, sehr fähig" },
  { value: "opus", label: "Opus", hint: "Tieferes Reasoning" },
  { value: "haiku", label: "Haiku", hint: "Am schnellsten" },
];

// ─── Tool-Use-Badge ─────────────────────────────────────────────────────

function ToolUseBadge({ name, input, output, defaultOpen = false }: {
  name: string; input: Record<string, unknown>; output: string; defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div
      style={{
        background: "rgba(59,130,246,0.08)",
        border: "1px solid rgba(59,130,246,0.3)",
        borderRadius: 8,
        padding: "8px 12px",
        marginTop: 6,
        marginBottom: 6,
        fontSize: 12,
      }}
    >
      <div
        onClick={() => setOpen(!open)}
        style={{ cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center" }}
      >
        <span><span style={{ color: "var(--accent)" }}>🔧</span> <b>{name}</b>
          <span style={{ color: "var(--text-dim)", marginLeft: 8 }}>
            ({Object.keys(input).length === 0 ? "no args" : JSON.stringify(input).slice(0, 60)})
          </span>
        </span>
        <span style={{ color: "var(--text-dim)" }}>{open ? "▼" : "▶"}</span>
      </div>
      {open && (
        <pre style={{
          marginTop: 8, padding: 10, background: "var(--bg)",
          borderRadius: 6, overflow: "auto", maxHeight: 240,
          fontSize: 11, color: "var(--text-dim)", whiteSpace: "pre-wrap",
        }}>
          {tryFormatJson(output)}
        </pre>
      )}
    </div>
  );
}

function tryFormatJson(s: string): string {
  try { return JSON.stringify(JSON.parse(s), null, 2); }
  catch { return s; }
}

// ─── Proposals-Panel ────────────────────────────────────────────────────

function ProposalsPanel({ refreshTrigger }: { refreshTrigger: number }) {
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  async function load() {
    try { setProposals(await listProposals()); } catch {}
  }
  useEffect(() => { load(); const id = setInterval(load, 10_000); return () => clearInterval(id); }, [refreshTrigger]);

  async function handle(id: string, action: "apply" | "reject") {
    setBusy(id);
    try {
      if (action === "apply") await applyProposal(id);
      else await rejectProposal(id);
      await load();
    } catch (e) { alert(String(e)); }
    finally { setBusy(null); }
  }

  const pending = proposals.filter((p) => p.status === "pending");
  const recent = proposals.filter((p) => p.status !== "pending").slice(0, 5);

  if (proposals.length === 0) {
    return (
      <div style={{ ...sidebarCard, color: "var(--text-dim)", fontSize: 12 }}>
        Noch keine Konfig-Vorschläge.<br /><br />
        Frag z.B.: <i>„Schlag mir eine bessere RR-Schwelle für BTC vor"</i>
      </div>
    );
  }

  return (
    <div style={sidebarCard}>
      {pending.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <h3 style={{ fontSize: 12, fontWeight: 700, color: "var(--yellow)", margin: "0 0 8px 0", textTransform: "uppercase", letterSpacing: 0.5 }}>
            Pending ({pending.length})
          </h3>
          {pending.map((p) => (
            <div key={p.id} style={{
              background: "var(--bg)", borderRadius: 8, padding: 10,
              marginBottom: 8, border: "1px solid var(--yellow)",
            }}>
              <div style={{ fontSize: 11, color: "var(--text-dim)", marginBottom: 4 }}>
                {p.created_at.slice(0, 16).replace("T", " ")}
              </div>
              <pre style={{ fontSize: 11, margin: "4px 0", color: "var(--accent)" }}>
                {JSON.stringify(p.diff, null, 2)}
              </pre>
              <p style={{ fontSize: 11, color: "var(--text-dim)", margin: "6px 0", lineHeight: 1.5 }}>
                {p.rationale}
              </p>
              <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                <button onClick={() => handle(p.id, "apply")} disabled={busy === p.id}
                  style={{ flex: 1, background: "var(--green)", color: "#fff", border: "none", borderRadius: 5, padding: "5px 10px", fontSize: 11, fontWeight: 600, cursor: "pointer" }}>
                  {busy === p.id ? "..." : "✓ Apply"}
                </button>
                <button onClick={() => handle(p.id, "reject")} disabled={busy === p.id}
                  style={{ flex: 1, background: "transparent", color: "var(--text-dim)", border: "1px solid var(--border)", borderRadius: 5, padding: "5px 10px", fontSize: 11, cursor: "pointer" }}>
                  ✕ Reject
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      {recent.length > 0 && (
        <div>
          <h3 style={{ fontSize: 11, fontWeight: 600, color: "var(--text-dim)", margin: "0 0 6px 0", textTransform: "uppercase", letterSpacing: 0.5 }}>
            Vergangen
          </h3>
          {recent.map((p) => (
            <div key={p.id} style={{
              padding: "4px 0", fontSize: 11, color: "var(--text-dim)",
              borderBottom: "1px solid var(--border)",
            }}>
              <span style={{ color: p.status === "applied" ? "var(--green)" : "var(--red)" }}>
                {p.status === "applied" ? "✓" : "✕"}
              </span>{" "}
              {JSON.stringify(p.diff).slice(0, 40)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Main Page ──────────────────────────────────────────────────────────

export default function ChatPage() {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [activeDetail, setActiveDetail] = useState<ConversationDetail | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [model, setModel] = useState<ChatModel | "">("");
  const [lastMeta, setLastMeta] = useState<{ ms: number; cost: number | null; model: string | null } | null>(null);
  const [proposalsRefresh, setProposalsRefresh] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  async function loadConversations() {
    try { setConversations(await listConversations()); } catch {}
  }
  useEffect(() => { loadConversations(); }, []);

  useEffect(() => {
    if (!activeId) { setActiveDetail(null); return; }
    let cancelled = false;
    (async () => {
      try {
        const d = await getConversation(activeId);
        if (!cancelled) {
          setActiveDetail(d);
          setModel((d.model as ChatModel) ?? "");
        }
      } catch {}
    })();
    return () => { cancelled = true; };
  }, [activeId]);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [activeDetail, busy]);

  async function handleSend() {
    if (!input.trim() || busy) return;
    const message = input.trim();
    setInput("");
    setBusy(true);
    setError(null);
    try {
      const res = await sendChatMessage(message, activeId ?? undefined, model || undefined);
      setActiveId(res.conversation_id);
      setLastMeta({ ms: res.duration_ms ?? 0, cost: res.cost_usd, model: res.model_used });
      const d = await getConversation(res.conversation_id);
      setActiveDetail(d);
      await loadConversations();
      setProposalsRefresh((n) => n + 1);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  function handleNewChat() {
    setActiveId(null);
    setActiveDetail(null);
    setLastMeta(null);
    setError(null);
  }

  async function handleDelete(id: string) {
    if (!confirm("Konversation löschen?")) return;
    try {
      await deleteConversation(id);
      if (activeId === id) handleNewChat();
      await loadConversations();
    } catch (e) { alert(String(e)); }
  }

  const turns = activeDetail?.turns ?? [];

  return (
    <div style={{ display: "flex", gap: 16, height: "calc(100vh - 100px)" }}>
      {/* Sidebar */}
      <aside style={{ width: 220, display: "flex", flexDirection: "column", gap: 12, flexShrink: 0 }}>
        <button onClick={handleNewChat}
          style={{ background: "var(--accent)", color: "#fff", border: "none", borderRadius: 8, padding: "8px 12px", fontSize: 13, fontWeight: 600, cursor: "pointer" }}>
          + Neue Konversation
        </button>
        <div style={{ ...sidebarCard, flex: 1, overflow: "auto" }}>
          <h3 style={sidebarHead}>Konversationen</h3>
          {conversations.length === 0 && (
            <p style={{ fontSize: 11, color: "var(--text-dim)" }}>Noch keine.</p>
          )}
          {conversations.map((c) => (
            <div key={c.id} onClick={() => setActiveId(c.id)}
              style={{
                padding: "6px 8px", marginBottom: 4, borderRadius: 6,
                cursor: "pointer",
                background: activeId === c.id ? "rgba(59,130,246,0.15)" : "transparent",
                border: activeId === c.id ? "1px solid var(--accent)" : "1px solid transparent",
                display: "flex", justifyContent: "space-between", alignItems: "start",
              }}
            >
              <div style={{ overflow: "hidden", flex: 1 }}>
                <div style={{ fontSize: 12, fontWeight: 500, color: "var(--text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {c.title}
                </div>
                <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 2 }}>
                  {c.n_turns} turns{c.model ? ` · ${c.model}` : ""}
                </div>
              </div>
              <button onClick={(e) => { e.stopPropagation(); handleDelete(c.id); }}
                style={{ background: "transparent", border: "none", color: "var(--text-dim)", cursor: "pointer", fontSize: 11, padding: 0 }}>
                ✕
              </button>
            </div>
          ))}
        </div>
        <ProposalsPanel refreshTrigger={proposalsRefresh} />
      </aside>

      {/* Main */}
      <main style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, gap: 12, flexWrap: "wrap" }}>
          <div>
            <h1 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>
              {activeDetail ? activeDetail.title : "Chat-Bot (Agent SDK)"}
            </h1>
            <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 2 }}>
              Läuft via Claude-Code-Subscription — kein API-Key nötig
            </div>
          </div>
          <select value={model} onChange={(e) => setModel(e.target.value as ChatModel | "")}
            style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)", padding: "5px 10px", fontSize: 12 }}>
            {MODELS.map((m) => <option key={m.value} value={m.value}>{m.label} — {m.hint}</option>)}
          </select>
        </div>

        <div ref={scrollRef}
          style={{
            flex: 1, overflowY: "auto",
            background: "var(--surface)", border: "1px solid var(--border)",
            borderRadius: 10, padding: 16, marginBottom: 12,
            display: "flex", flexDirection: "column", gap: 12,
          }}>
          {turns.length === 0 && !busy && (
            <div style={{ color: "var(--text-dim)", textAlign: "center", marginTop: 40, fontSize: 14 }}>
              <p>Frag mich nach Bot-Status, lass Backtests laufen, oder bitte um Strategie-Vorschläge.</p>
              <div style={{ marginTop: 24, fontSize: 12, lineHeight: 1.9 }}>
                <p style={{ color: "var(--text-dim)" }}>Beispiele:</p>
                <p>• <i>Wie steht der Bot gerade?</i></p>
                <p>• <i>Lauf einen Backtest auf BTC, 800 5m-Bars, RR 2.5</i></p>
                <p>• <i>Warum gibt es kein Signal auf DE40?</i></p>
                <p>• <i>Welche Trades waren in den letzten 24h profitabel?</i></p>
                <p>• <i>Schlag mir eine bessere RR-Schwelle für SP500 vor</i></p>
              </div>
            </div>
          )}

          {turns.map((t, i) => (
            <div key={i} style={{ display: "flex", flexDirection: "column" }}>
              <div style={{
                alignSelf: t.role === "user" ? "flex-end" : "flex-start",
                background: t.role === "user" ? "var(--accent-dim)" : "var(--bg)",
                color: t.role === "user" ? "#fff" : "var(--text)",
                padding: "10px 14px", borderRadius: 12,
                maxWidth: "85%", whiteSpace: "pre-wrap", fontSize: 14, lineHeight: 1.5,
              }}>
                {t.text || (t.role === "assistant" && t.tool_calls.length > 0
                  ? "(Tool-Calls — siehe unten)" : "")}
              </div>
              {t.tool_calls.length > 0 && (
                <div style={{ alignSelf: "flex-start", maxWidth: "85%", marginTop: 4 }}>
                  {t.tool_calls.map((tc, j) => (
                    <ToolUseBadge key={j} name={tc.name} input={tc.input} output={tc.output} />
                  ))}
                </div>
              )}
            </div>
          ))}

          {busy && (
            <div style={{
              alignSelf: "flex-start", color: "var(--text-dim)", fontSize: 13,
              fontStyle: "italic", padding: "6px 0",
            }}>
              ● ● ● Claude denkt nach...
            </div>
          )}
        </div>

        {lastMeta && (
          <div style={{ fontSize: 10, color: "var(--text-dim)", textAlign: "right", marginBottom: 4 }}>
            {lastMeta.model ?? "?"} · {(lastMeta.ms / 1000).toFixed(1)}s
            {lastMeta.cost !== null && ` · ~$${lastMeta.cost.toFixed(4)} (Sub-Eq.)`}
          </div>
        )}

        {error && (
          <div style={{
            background: "rgba(239,68,68,0.1)", border: "1px solid var(--red)",
            color: "var(--red)", borderRadius: 8, padding: "8px 12px",
            fontSize: 13, marginBottom: 8,
          }}>
            {error}
          </div>
        )}

        <div style={{ display: "flex", gap: 8 }}>
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
            }}
            placeholder="Nachricht eingeben... (Enter zum Senden, Shift+Enter für Zeilenumbruch)"
            disabled={busy}
            rows={2}
            style={{
              flex: 1, background: "var(--surface)", border: "1px solid var(--border)",
              borderRadius: 8, color: "var(--text)", padding: "10px 14px",
              fontSize: 14, resize: "vertical", fontFamily: "inherit", outline: "none",
            }}
          />
          <button onClick={handleSend} disabled={busy || !input.trim()}
            style={{
              background: busy || !input.trim() ? "var(--border)" : "var(--accent)",
              color: "#fff", border: "none", borderRadius: 8,
              padding: "0 24px", fontSize: 14, fontWeight: 600,
              cursor: busy || !input.trim() ? "default" : "pointer",
            }}>
            {busy ? "..." : "Senden"}
          </button>
        </div>
      </main>
    </div>
  );
}

const sidebarCard = {
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: 10,
  padding: 12,
} as const;

const sidebarHead = {
  fontSize: 11, fontWeight: 700, color: "var(--text-dim)",
  margin: "0 0 8px 0", textTransform: "uppercase", letterSpacing: 0.5,
} as const;
