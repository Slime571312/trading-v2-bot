"use client";

import { useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import {
  fetchBotOverview, startBot, stopBot, resetBot,
  startAllBots, stopAllBots, fetchTickLog,
  fetchBotStats, patchBotConfig, fetchCandles,
  type BotInstance, type BotOverview, type Instrument, type TickLogEntry,
  type BotStats, type BotConfigPatch, type Candle, type Timeframe,
} from "@/lib/api";
import { useLiveFeed } from "@/lib/useLiveFeed";

// SSR-disabled — lightweight-charts braucht window
const TradeChart = dynamic(() => import("@/components/TradeChart"), { ssr: false });
type TradeMarker = { time: string; side: "long" | "short"; kind: "open" | "close"; price: number; label?: string };

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

// ─── Bot-Detail Modal: Stats + Hot-Reload Config ───────────────────────

function BotDetailModal({ instrument, instance, onClose, onRefresh }: {
  instrument: Instrument;
  instance: BotInstance | undefined;
  onClose: () => void;
  onRefresh: () => Promise<void>;
}) {
  const [stats, setStats] = useState<BotStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [editMode, setEditMode] = useState(false);

  // Form state — initial aus stats
  const [rr, setRr] = useState<string>("");
  const [risk, setRisk] = useState<string>("");
  const [lookback, setLookback] = useState<string>("");
  const [tick, setTick] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [saveOk, setSaveOk] = useState<string | null>(null);

  // Chart-State
  const [chartTf, setChartTf] = useState<Timeframe>("5m");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [chartLoading, setChartLoading] = useState(false);
  const [chartErr, setChartErr] = useState<string | null>(null);

  async function load() {
    try {
      const s = await fetchBotStats(instrument);
      setStats(s);
      if (!editMode) {
        setRr(String(s.rr_threshold));
        setRisk(String((s.risk_pct * 100).toFixed(2)));
        setLookback(String(s.sweep_lookback));
        setTick(String(s.tick_interval_s));
      }
      setLoading(false);
    } catch (e) {
      console.error(e);
    }
  }

  // Candles laden bei TF-Wechsel + alle 30s refresh
  useEffect(() => {
    let cancelled = false;
    async function loadCandles() {
      setChartLoading(true);
      setChartErr(null);
      try {
        const res = await fetchCandles(instrument, chartTf, 100);
        if (!cancelled) setCandles(res.candles);
      } catch (e) {
        if (!cancelled) setChartErr(String(e));
      } finally {
        if (!cancelled) setChartLoading(false);
      }
    }
    loadCandles();
    const id = setInterval(loadCandles, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [instrument, chartTf]);

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instrument, editMode]);

  // Trade-Linien + Marker bestimmen — offener Trade hat Vorrang, sonst letzter geschlossener
  const openTrade = instance?.open_trades?.[0];
  const lastClosed = instance?.closed_trades && instance.closed_trades.length > 0
    ? [...instance.closed_trades].sort((a, b) =>
        new Date(b.close_time).getTime() - new Date(a.close_time).getTime())[0]
    : undefined;

  const priceLines: Array<{ price: number; color: string; label: string; dashed?: boolean }> = [];
  const markers: TradeMarker[] = [];

  if (openTrade) {
    priceLines.push(
      { price: openTrade.entry, color: "#3b82f6", label: `Entry ${openTrade.entry.toFixed(2)}` },
      { price: openTrade.sl, color: "#ef4444", label: `SL ${openTrade.sl.toFixed(2)}`, dashed: true },
      { price: openTrade.tp, color: "#22c55e", label: `TP ${openTrade.tp.toFixed(2)}`, dashed: true },
    );
    markers.push({
      time: openTrade.open_time, side: openTrade.side as "long" | "short",
      kind: "open", price: openTrade.entry,
      label: `${openTrade.side.toUpperCase()} ${openTrade.variant}`,
    });
  } else if (lastClosed) {
    priceLines.push(
      { price: lastClosed.entry, color: "#3b82f6", label: `Entry ${lastClosed.entry.toFixed(2)}` },
      { price: lastClosed.sl, color: "#ef4444", label: `SL ${lastClosed.sl.toFixed(2)}`, dashed: true },
      { price: lastClosed.tp, color: "#22c55e", label: `TP ${lastClosed.tp.toFixed(2)}`, dashed: true },
    );
    markers.push(
      {
        time: lastClosed.open_time, side: lastClosed.side as "long" | "short",
        kind: "open", price: lastClosed.entry,
        label: `${lastClosed.side.toUpperCase()} ${lastClosed.variant}`,
      },
      {
        time: lastClosed.close_time, side: lastClosed.side as "long" | "short",
        kind: "close", price: lastClosed.exit,
        label: `${lastClosed.exit_reason} ${lastClosed.r_multiple >= 0 ? "+" : ""}${lastClosed.r_multiple.toFixed(2)}R`,
      },
    );
  }

  async function handleSave() {
    if (!stats) return;
    setSaving(true);
    setSaveErr(null);
    setSaveOk(null);
    const patch: BotConfigPatch = {};
    const newRr = parseFloat(rr);
    const newRisk = parseFloat(risk) / 100;
    const newLb = parseInt(lookback, 10);
    const newTick = parseInt(tick, 10);
    if (!isNaN(newRr) && newRr !== stats.rr_threshold) patch.rr_threshold = newRr;
    if (!isNaN(newRisk) && Math.abs(newRisk - stats.risk_pct) > 1e-9) patch.risk_pct = newRisk;
    if (!isNaN(newLb) && newLb !== stats.sweep_lookback) patch.sweep_lookback = newLb;
    if (!isNaN(newTick) && newTick !== stats.tick_interval_s) patch.tick_interval_s = newTick;

    if (Object.keys(patch).length === 0) {
      setSaveErr("Keine Änderung");
      setSaving(false);
      return;
    }
    try {
      await patchBotConfig(instrument, patch);
      setSaveOk(`Übernommen — Bot läuft weiter (${Object.keys(patch).join(", ")})`);
      setEditMode(false);
      await load();
      await onRefresh();
    } catch (e) {
      setSaveErr(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100,
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        background: "var(--bg)", border: "1px solid var(--border)",
        borderRadius: 12, maxWidth: 980, width: "92%",
        maxHeight: "90vh", display: "flex", flexDirection: "column",
      }}>
        {/* Header */}
        <div style={{
          display: "flex", justifyContent: "space-between", alignItems: "center",
          padding: "14px 20px", borderBottom: "1px solid var(--border)",
        }}>
          <div>
            <h2 style={{ fontSize: 17, fontWeight: 700, margin: 0 }}>
              {instrument} — Detail-Statistiken
              {stats?.running && (
                <span style={{
                  marginLeft: 10, fontSize: 10, fontWeight: 700, padding: "2px 7px",
                  borderRadius: 12, background: "rgba(34,197,94,0.15)", color: "var(--green)",
                }}>● RUN</span>
              )}
            </h2>
            <p style={{ fontSize: 11, color: "var(--text-dim)", margin: "2px 0 0 0" }}>
              {stats?.last_tick ? `letzter Tick ${fmtDate(stats.last_tick)} · alle 5s refresht` : "Daten lädt…"}
            </p>
          </div>
          <button onClick={onClose} style={{
            background: "transparent", border: "1px solid var(--border)",
            color: "var(--text-dim)", padding: "4px 12px", borderRadius: 6,
            cursor: "pointer", fontSize: 12,
          }}>✕ Close</button>
        </div>

        <div style={{ overflow: "auto", padding: "16px 20px", flex: 1 }}>
          {loading && <p style={{ color: "var(--text-dim)" }}>Lädt…</p>}
          {stats && (
            <>
              {/* Top-Row: Equity + P&L Hero */}
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
                <DetailStat label="Equity" value={`€${fmtMoney(stats.equity)}`} sub={`start: €${fmtMoney(stats.initial_capital)}`} />
                <DetailStat label="P&L" value={`${stats.pnl_abs >= 0 ? "+" : ""}€${fmtMoney(stats.pnl_abs)}`}
                  sub={`${stats.pnl_pct >= 0 ? "+" : ""}${stats.pnl_pct.toFixed(2)}%`}
                  color={stats.pnl_abs >= 0 ? "var(--green)" : "var(--red)"} />
                <DetailStat label="Trades" value={String(stats.n_closed)} sub={`+ ${stats.n_open} offen`} />
                <DetailStat label="Win-Rate"
                  value={stats.win_rate !== null ? `${(stats.win_rate * 100).toFixed(0)}%` : "—"}
                  sub={`${stats.wins}W / ${stats.losses}L`} />
                <DetailStat label="Avg R"
                  value={stats.avg_r !== null ? `${stats.avg_r >= 0 ? "+" : ""}${stats.avg_r.toFixed(2)}R` : "—"}
                  sub={stats.expectancy_r !== null ? `Expectancy ${stats.expectancy_r.toFixed(2)}R` : "—"}
                  color={stats.avg_r !== null && stats.avg_r >= 0 ? "var(--green)" : "var(--red)"} />
              </div>

              {/* Chart mit Entry/SL/TP */}
              <SectionHeader>
                Chart {openTrade ? "— offener Trade" : lastClosed ? "— letzter Trade" : ""}
                <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
                  {(["1m", "5m", "15m", "1h"] as Timeframe[]).map((tf) => (
                    <button
                      key={tf}
                      onClick={() => setChartTf(tf)}
                      style={{
                        background: chartTf === tf ? "var(--accent)" : "transparent",
                        color: chartTf === tf ? "#fff" : "var(--text-dim)",
                        border: `1px solid ${chartTf === tf ? "var(--accent)" : "var(--border)"}`,
                        padding: "2px 10px", borderRadius: 4,
                        fontSize: 10, fontWeight: 600, cursor: "pointer",
                      }}
                    >{tf}</button>
                  ))}
                </div>
              </SectionHeader>
              <div style={{
                background: "var(--surface)", border: "1px solid var(--border)",
                borderRadius: 8, padding: 8, marginBottom: 18, position: "relative",
              }}>
                {chartLoading && candles.length === 0 && (
                  <div style={{ height: 280, display: "flex", alignItems: "center", justifyContent: "center",
                    color: "var(--text-dim)", fontSize: 12 }}>Lade Candles…</div>
                )}
                {chartErr && (
                  <div style={{ height: 280, display: "flex", alignItems: "center", justifyContent: "center",
                    color: "var(--red)", fontSize: 12 }}>Chart-Fehler: {chartErr}</div>
                )}
                {!chartErr && candles.length > 0 && (
                  <TradeChart
                    candles={candles}
                    height={280}
                    priceLines={priceLines}
                    markers={markers}
                  />
                )}
                {(openTrade || lastClosed) && (
                  <div style={{
                    display: "flex", gap: 10, fontSize: 10, color: "var(--text-dim)",
                    padding: "4px 8px 0", flexWrap: "wrap",
                  }}>
                    <span><span style={{ color: "#3b82f6" }}>━</span> Entry</span>
                    <span><span style={{ color: "#ef4444" }}>┄</span> SL</span>
                    <span><span style={{ color: "#22c55e" }}>┄</span> TP</span>
                    {openTrade && (
                      <span style={{ marginLeft: "auto", color: openTrade.side === "long" ? "var(--green)" : "var(--red)" }}>
                        OFFEN {openTrade.side.toUpperCase()} · {openTrade.variant} · RR {openTrade.rr_at_open.toFixed(2)}
                      </span>
                    )}
                    {!openTrade && lastClosed && (
                      <span style={{ marginLeft: "auto",
                        color: lastClosed.r_multiple > 0 ? "var(--green)" : "var(--red)" }}>
                        {lastClosed.side.toUpperCase()} · {lastClosed.exit_reason} · {lastClosed.r_multiple >= 0 ? "+" : ""}{lastClosed.r_multiple.toFixed(2)}R
                      </span>
                    )}
                  </div>
                )}
                {!openTrade && !lastClosed && candles.length > 0 && (
                  <div style={{
                    fontSize: 10, color: "var(--text-dim)", textAlign: "center", padding: "4px 0 0",
                  }}>
                    Noch keine Trades — Chart zeigt aktuelle Kurse.
                  </div>
                )}
              </div>

              {/* R-Stats Row */}
              <SectionHeader>R-Multiple Verteilung</SectionHeader>
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
                <MiniStat label="Best" value={stats.best_r !== null ? `+${stats.best_r.toFixed(2)}R` : "—"} color="var(--green)" />
                <MiniStat label="Worst" value={stats.worst_r !== null ? `${stats.worst_r.toFixed(2)}R` : "—"} color="var(--red)" />
                <MiniStat label="Avg Win" value={stats.avg_win_r !== null ? `+${stats.avg_win_r.toFixed(2)}R` : "—"} color="var(--green)" />
                <MiniStat label="Avg Loss" value={stats.avg_loss_r !== null ? `${stats.avg_loss_r.toFixed(2)}R` : "—"} color="var(--red)" />
                <MiniStat label="Hold Ø" value={stats.avg_hold_minutes !== null ? `${stats.avg_hold_minutes.toFixed(0)}min` : "—"} />
              </div>

              {/* Trade-Mix */}
              <SectionHeader>Trade-Mix</SectionHeader>
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
                <MiniStat label="Long" value={String(stats.longs)} color="var(--green)" />
                <MiniStat label="Short" value={String(stats.shorts)} color="var(--red)" />
                <MiniStat label="TP-Exits" value={String(stats.tp_exits)} color="var(--green)" />
                <MiniStat label="SL-Exits" value={String(stats.sl_exits)} color="var(--red)" />
                <MiniStat label="Manual" value={String(stats.manual_exits)} />
              </div>

              {/* Variant-Breakdown */}
              {Object.keys(stats.variant_breakdown).length > 0 && (
                <>
                  <SectionHeader>Pro Setup-Variante</SectionHeader>
                  <table style={{ width: "100%", fontSize: 12, marginBottom: 18, borderCollapse: "collapse" }}>
                    <thead>
                      <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
                        <th style={{ padding: "5px 8px", borderBottom: "1px solid var(--border)" }}>Variant</th>
                        <th style={{ padding: "5px 8px", borderBottom: "1px solid var(--border)" }}>n</th>
                        <th style={{ padding: "5px 8px", borderBottom: "1px solid var(--border)" }}>Win-Rate</th>
                        <th style={{ padding: "5px 8px", borderBottom: "1px solid var(--border)" }}>Avg R</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(stats.variant_breakdown).map(([name, v]) => (
                        <tr key={name} style={{ borderBottom: "1px solid var(--border)" }}>
                          <td style={{ padding: "5px 8px", fontWeight: 600 }}>{name}</td>
                          <td style={{ padding: "5px 8px" }}>{v.n}</td>
                          <td style={{ padding: "5px 8px" }}>{v.win_rate !== null ? `${(v.win_rate * 100).toFixed(0)}%` : "—"}</td>
                          <td style={{ padding: "5px 8px", color: v.avg_r !== null && v.avg_r >= 0 ? "var(--green)" : "var(--red)" }}>
                            {v.avg_r !== null ? `${v.avg_r >= 0 ? "+" : ""}${v.avg_r.toFixed(2)}R` : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}

              {/* Equity-Kurve als Inline-SVG */}
              {stats.equity_curve.length > 1 && (
                <>
                  <SectionHeader>Equity-Kurve</SectionHeader>
                  <EquityChart curve={stats.equity_curve} initialCapital={stats.initial_capital} />
                </>
              )}

              {/* Hot-Reload-Config */}
              <SectionHeader>
                Parameter (Hot-Reload — Bot läuft weiter)
                {!editMode && (
                  <button onClick={() => setEditMode(true)} style={{
                    marginLeft: 10, background: "transparent", border: "1px solid var(--accent)",
                    color: "var(--accent)", padding: "2px 10px", borderRadius: 5,
                    fontSize: 11, cursor: "pointer",
                  }}>✎ Bearbeiten</button>
                )}
              </SectionHeader>

              <div style={{
                background: "var(--surface)", border: `1px solid ${editMode ? "var(--accent)" : "var(--border)"}`,
                borderRadius: 8, padding: 14, marginBottom: 12,
              }}>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
                  <ParamField label="RR-Threshold" value={rr} setValue={setRr}
                    suffix=":1" disabled={!editMode} step={0.1} hint="min Risk-Reward für Direct-Entry" />
                  <ParamField label="Risk pro Trade" value={risk} setValue={setRisk}
                    suffix=" %" disabled={!editMode} step={0.1} hint="% Equity je Trade" />
                  <ParamField label="Sweep-Lookback" value={lookback} setValue={setLookback}
                    suffix=" bars" disabled={!editMode} step={1} hint="HTF-Sweep-Fenster" />
                  <ParamField label="Tick-Intervall" value={tick} setValue={setTick}
                    suffix=" s" disabled={!editMode} step={5} hint="Loop-Frequenz" />
                </div>
                {editMode && (
                  <div style={{ display: "flex", gap: 8, marginTop: 14, justifyContent: "flex-end" }}>
                    {saveErr && (
                      <span style={{ color: "var(--red)", fontSize: 11, alignSelf: "center" }}>{saveErr}</span>
                    )}
                    <button onClick={() => { setEditMode(false); setSaveErr(null); }}
                      disabled={saving}
                      style={btn("transparent", "var(--text-dim)")}>Abbrechen</button>
                    <button onClick={handleSave} disabled={saving} style={btn("var(--green)")}>
                      {saving ? "…" : "Übernehmen"}
                    </button>
                  </div>
                )}
                {saveOk && !editMode && (
                  <div style={{
                    marginTop: 10, padding: "6px 10px", background: "rgba(34,197,94,0.1)",
                    border: "1px solid var(--green)", borderRadius: 5, fontSize: 11, color: "var(--green)",
                  }}>✓ {saveOk}</div>
                )}
              </div>

              {stats.last_error && (
                <div style={{
                  padding: "8px 12px", background: "rgba(239,68,68,0.1)",
                  border: "1px solid var(--red)", borderRadius: 6, fontSize: 11, color: "var(--red)",
                  marginBottom: 10,
                }}>
                  <b>letzter Fehler:</b> {stats.last_error}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function DetailStat({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div style={{
      background: "var(--surface)", border: "1px solid var(--border)",
      borderRadius: 8, padding: "10px 14px", minWidth: 130, flex: "1 1 130px",
    }}>
      <div style={{ fontSize: 10, color: "var(--text-dim)", marginBottom: 3 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: color ?? "var(--text)" }}>{value}</div>
      {sub && <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

function MiniStat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{
      background: "var(--surface)", border: "1px solid var(--border)",
      borderRadius: 6, padding: "6px 10px", minWidth: 80,
    }}>
      <div style={{ fontSize: 9, color: "var(--text-dim)" }}>{label}</div>
      <div style={{ fontSize: 13, fontWeight: 700, color: color ?? "var(--text)" }}>{value}</div>
    </div>
  );
}

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 600, color: "var(--text-dim)",
      textTransform: "uppercase", letterSpacing: 0.5,
      marginBottom: 8, display: "flex", alignItems: "center",
    }}>{children}</div>
  );
}

function ParamField({ label, value, setValue, suffix, disabled, step, hint }: {
  label: string; value: string; setValue: (v: string) => void;
  suffix?: string; disabled: boolean; step: number; hint?: string;
}) {
  return (
    <div>
      <div style={{ fontSize: 10, color: "var(--text-dim)", marginBottom: 3 }}>{label}</div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 4 }}>
        <input
          type="number"
          step={step}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={disabled}
          style={{
            background: disabled ? "transparent" : "var(--bg)",
            border: `1px solid ${disabled ? "transparent" : "var(--border)"}`,
            color: "var(--text)", padding: "4px 8px", borderRadius: 4,
            fontSize: 15, fontWeight: 700, width: "100%", outline: "none",
            cursor: disabled ? "default" : "text",
          }}
        />
        {suffix && <span style={{ fontSize: 11, color: "var(--text-dim)" }}>{suffix}</span>}
      </div>
      {hint && <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

function EquityChart({ curve, initialCapital }: { curve: { time: string | null; equity: number; pnl_cum: number }[]; initialCapital: number }) {
  const W = 800;
  const H = 160;
  const PAD = 8;
  const equities = curve.map((p) => p.equity);
  const minE = Math.min(initialCapital, ...equities);
  const maxE = Math.max(initialCapital, ...equities);
  const range = maxE - minE || 1;
  const stepX = (W - 2 * PAD) / Math.max(1, curve.length - 1);
  const points = curve.map((p, i) => {
    const x = PAD + i * stepX;
    const y = PAD + (1 - (p.equity - minE) / range) * (H - 2 * PAD);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const baselineY = PAD + (1 - (initialCapital - minE) / range) * (H - 2 * PAD);
  const lastP = curve[curve.length - 1];
  const goingUp = lastP.equity >= initialCapital;

  return (
    <div style={{
      background: "var(--surface)", border: "1px solid var(--border)",
      borderRadius: 8, padding: 12, marginBottom: 18,
    }}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: H }}>
        <line x1={PAD} y1={baselineY} x2={W - PAD} y2={baselineY}
          stroke="var(--text-dim)" strokeDasharray="3 3" strokeWidth={1} opacity={0.5} />
        <polyline
          fill="none"
          stroke={goingUp ? "var(--green)" : "var(--red)"}
          strokeWidth={2}
          points={points}
        />
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-dim)", marginTop: 4 }}>
        <span>start €{fmtMoney(initialCapital)}</span>
        <span>{curve.length - 1} closes</span>
        <span>jetzt €{fmtMoney(lastP.equity)}</span>
      </div>
    </div>
  );
}

// ─── Single Bot-Card ────────────────────────────────────────────────────

function BotCard({ inst, onChange, onShowTicks, onShowDetails }: {
  inst: BotInstance;
  onChange: () => Promise<void>;
  onShowTicks: () => void;
  onShowDetails: () => void;
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
    <div
      onClick={(e) => {
        // Klick auf Card öffnet Details, außer der Klick kam von einem Button
        if ((e.target as HTMLElement).closest("button")) return;
        onShowDetails();
      }}
      style={{
      background: "var(--surface)",
      border: `1px solid ${isRunning ? "var(--green)" : "var(--border)"}`,
      borderRadius: 10, padding: "14px 18px",
      flex: "1 1 280px", minWidth: 280,
      cursor: "pointer",
      transition: "border-color 0.15s, transform 0.05s",
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
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6 }}>
        <span style={{ fontSize: 10, color: "var(--text-dim)" }}>
          tick: {fmtDate(inst.last_tick)}{inst.tick_interval_s ? ` (${inst.tick_interval_s}s)` : ""}
        </span>
        <div style={{ display: "flex", gap: 4 }}>
          <button onClick={onShowDetails} style={{
            background: "transparent", border: "1px solid var(--accent)",
            color: "var(--accent)", padding: "3px 8px", borderRadius: 5,
            fontSize: 10, cursor: "pointer", fontWeight: 600,
          }}>
            📊 Stats
          </button>
          <button onClick={onShowTicks} style={{
            background: "transparent", border: "1px solid var(--border)",
            color: "var(--accent)", padding: "3px 8px", borderRadius: 5,
            fontSize: 10, cursor: "pointer",
          }}>
            🔍 Reasoning ({inst.n_tick_log})
          </button>
        </div>
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
  const [detailModalFor, setDetailModalFor] = useState<Instrument | null>(null);

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
      {detailModalFor && (
        <BotDetailModal
          instrument={detailModalFor}
          instance={instances.find((i) => i.instrument === detailModalFor)}
          onClose={() => setDetailModalFor(null)}
          onRefresh={reload}
        />
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
            onShowTicks={() => setTickModalFor(inst.instrument as Instrument)}
            onShowDetails={() => setDetailModalFor(inst.instrument as Instrument)} />
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
