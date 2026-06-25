# Trading-Bot v2 — Monitoring Quick-Start

Willkommen zur **Production-Ready Observability** für deinen Trading-Bot!

## 60-Second Start

```bash
cd ~/trading-v2/infra
bash start-monitoring.sh
```

**Services:**
- Prometheus: http://localhost:9090 (Metriken sammeln)
- Grafana: http://localhost:3001 (Dashboards, Credentials: admin/simontrading123)

## Was du jetzt hast

### 3 Dashboards (zum Selber-Erstellen)

1. **Status** — Aufmachen morgens, "Alles grün?" → weggehen
   - Equity-Trend (24h)
   - Tick-Latency
   - Open Positions
   - Drawdown
   - Error-Count

2. **Analytics** — Wöchentlicher Review
   - Win-Rate-Trend
   - Sharpe Ratio
   - Profit Factor
   - Avg RR per Pair
   - Trade Duration Histogram

3. **System-Health** — Ops-Monitoring
   - CPU / Memory / Disk
   - API Latency (p50/p95/p99)
   - Error Rate
   - Circuit Breaker Status
   - DB Connection Pool

### 27 Metriken (4-Tier Architektur)

- **Tier 1 (Application):** Tick-Latency, Signal-Eval, Trades/min, Equity, Drawdown, Win-Rate
- **Tier 2 (System):** CPU, Memory, Disk, File-Descriptors
- **Tier 3 (Business):** Sharpe, Profit-Factor, Avg-RR, Trade-Duration
- **Tier 4 (Health):** API-OK?, DB-OK?, Loop-OK?, Equity-OK?, Circuit-Breaker-OK?

### 15 Alert-Rules

Prometheus evaluiert diese kontinuierlich → Alerts bei Problemen.

| Schwere | Trigger | Action |
|---|---|---|
| 🔴 Critical | Loop stuck, API down, Drawdown < −10% | Sofort handeln |
| 🟡 Warning | High latency, Low win-rate, High error-rate | Beobachten |
| 🟢 Info | Trade opened/closed | Notieren |

## Daily Runbook

### Morgens (08:00)
```bash
# Dashboard öffnen
open http://localhost:3001/d/trading-bot-status

# Schnell checken:
✓ Status 🟢?
✓ Equity up oder seitwärts?
✓ Errors < 5?
✓ Loop läuft (Last Tick < 15s)?
```

### Abends (21:00)
```bash
# Notieren:
Daily P/L: $X
Trades: Y
Win Rate (7d): Z%
Max DD: −W%
```

### Wöchentlich (Montag)
```bash
# Analytics Dashboard prüfen
Win-Rate Trend: up/down/stable?
Profit-Factor > 1.5?
Sharpe > 1.0?
→ Falls rot: Nightly-Tuner triggern
```

## Next Steps (für Simon)

### 1. Backend instrumentieren (15 min)

```bash
cd ~/trading-v2/backend

# Install
pip install prometheus-fastapi-instrumentator

# Dann:
# 1. Kopiere backend/src/api/metrics.py (aus Dokumentation)
# 2. Edit backend/src/api/main.py: Füge Instrumentator ein
# 3. Edit backend/src/live/loop.py: Integriere TICK_LATENCY, EQUITY, etc.
```

### 2. Test (2 min)

```bash
# Backend neu starten
cd ~/trading-v2/backend && python -m uvicorn src.api.main:app --reload

# Dann:
curl http://localhost:8000/metrics | head -20
# → sollte Prometheus-Metriken zeigen
```

### 3. Dashboards erstellen (30 min)

Öffne Grafana: http://localhost:3001

**Pro Dashboard:**
1. Create → Dashboard → Add Panel
2. Datasource: Prometheus
3. Trage PromQL-Query ein (siehe `infra/MONITORING.md`)
4. Save

## Baseline-Werte (zum Merken)

| Metrik | Grün | Orange | Rot |
|---|---|---|---|
| Tick Latency (p95) | < 200ms | 200–500ms | > 500ms |
| Win Rate (7d) | 50–65% | 40–49% | < 40% |
| Drawdown | ±5% | ±10% | < −10% |
| CPU | < 40% | 40–70% | > 70% |
| Memory | < 50% | 50–70% | > 70% |
| Loop Age (Tick) | < 15s | 15–30s | > 30s |

## Troubleshooting

### Services starten nicht
```bash
docker-compose -f infra/docker-compose-monitoring.yml logs prometheus
docker-compose -f infra/docker-compose-monitoring.yml logs grafana
```

### Prometheus scrapet keine Metriken
1. Läuft Backend? `http://localhost:8000/metrics` öffnen
2. Prometheus-Config prüfen: `http://localhost:9090/targets`
3. Falls rot: Firewall prüfen oder Backend-Port anpassen

### Grafana-Dashboards leer
1. Data Source prüfen: Configuration → Data Sources → Prometheus → Test
2. PromQL-Query in Graph-UI testen: `http://localhost:9090/graph`

## Dokumentation

**Vollständige Doku siehe:**
- `Trading/Bot/Monitoring-Dashboards.md` — 4-Tier Architektur, 3 Dashboards, 15 Alerts, Setup
- `Trading/Bot/Monitoring-Runbook.md` — Tägliche/Wöchentliche Checks
- `infra/MONITORING.md` — Docker-Compose, Troubleshooting, Backend-Integration
- `infra/prometheus-rules.yml` — Alert-Definitionen (15 Rules)

## Schnelle Links

- Prometheus Graph: http://localhost:9090/graph
- Prometheus Targets: http://localhost:9090/targets
- Prometheus Alerts: http://localhost:9090/alerts
- Grafana Dashboards: http://localhost:3001/dashboards
- Grafana Data Sources: http://localhost:3001/datasources

## FAQ

**Q: Warum nicht Datadog/New Relic?**
A: Kostenlos, lokal, keine externen Dependencies.

**Q: Wann sollte ich Alerts konfigurieren?**
A: Prometheus evaluiert Alert-Rules automatisch (alle 30s). Telegram-Pushes gehen über `notifier.py`.

**Q: Kann ich mehr Metriken hinzufügen?**
A: Ja! Edit `backend/src/api/metrics.py`, definiere neue Gauge/Counter/Histogram, dann in `loop.py` integrieren.

**Q: Was passiert wenn Prometheus stoppt?**
A: Alte Daten bleiben in `prometheus_data/` (Persistierung). Neu starten mit `start-monitoring.sh`.

---

✅ **Setup Complete** — Du bist bereit für Production Monitoring!
