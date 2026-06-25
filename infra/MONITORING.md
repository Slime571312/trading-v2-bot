# Trading-Bot v2 — Monitoring & Observability Setup

Vollständige Anleitung zum Starten von **Prometheus + Grafana** für Live-Überwachung des
Trading-Bots.

## Quick Start

```bash
cd ~/trading-v2/infra
bash start-monitoring.sh
```

Das startet:
- ✅ **Prometheus** — Metrics-Scraper (http://localhost:9090)
- ✅ **Grafana** — Dashboard & Visualization (http://localhost:3001)
- ✅ **node_exporter** — System-Metriken (CPU, RAM, Disk)

**Credentials:**
- Grafana: `admin` / `simontrading123`

---

## Architektur

### 4-Tier Metrics

| Tier | Komponente | Quelle | Grafana | Update |
|---|---|---|---|---|
| **1: Application** | Tick-Latenz, Signal-Eval, Trades/min | Bot API (`/metrics`) | Status Dashboard | 15s |
| **2: System** | CPU, Memory, Disk, FDs | node_exporter | System-Health Dashboard | 30s |
| **3: Business** | Win-Rate, Sharpe, Profit-Factor | API (`/stats`) | Analytics Dashboard | 1h |
| **4: Health** | Loop OK?, DB OK?, Equity OK? | `/health` endpoint | Status-Badges | 15s |

### 3 Dashboards

1. **Trading-Bot Status** — Morgens aufmachen, grün = alles OK
   - Equity-Kurve (24h)
   - Tick-Latency Trend
   - Open Positions (Tabelle)
   - Drawdown Gauge
   - Status-Indicator

2. **Performance-Analytics** — Wöchentlicher Review
   - Win-Rate Trend (7d / 30d / 90d)
   - Sharpe Ratio
   - Profit Factor
   - Avg RR per Instrument (Balken)
   - Trade Duration Distribution

3. **System-Health** — Ops-Dashboard
   - CPU / Memory / Disk Gauges
   - API Latency (p50/p95/p99)
   - Error Rate (last 1h)
   - Circuit Breaker Status
   - Database Connection Pool

---

## Files

```
infra/
├── docker-compose-monitoring.yml   ← Services (Prometheus, Grafana, node_exporter)
├── prometheus.yml                  ← Prometheus Config
├── prometheus-rules.yml            ← Alert Rules (15 Regeln)
├── start-monitoring.sh             ← Start-Skript
├── MONITORING.md                   ← Diese Datei
└── grafana/
    └── provisioning/
        ├── datasources/
        │   └── prometheus.yml      ← Datasource-Konfiguration
        └── dashboards/
            └── (Dashboard-JSONs hier eintragen)
```

---

## Verwendung

### 1. Services starten

```bash
cd ~/trading-v2/infra
bash start-monitoring.sh
```

### 2. Grafana öffnen

Öffne http://localhost:3001 im Browser.

**Erste Schritte:**
1. Links oben: **Configuration** → **Data Sources**
2. Prometheus sollte bereits dort sein (`http://prometheus:9090`)
3. **Test** → sollte grün sein

### 3. Dashboards erstellen

Gehe zu **Create** → **Dashboard** → **Add Panel**

Für jedes Panel: **Datasource** = Prometheus, dann einen PromQL-Query eintragen.

**Beispiele:**

```promql
# Equity (Gauge)
bot_equity_usd

# Tick Latency (Histogram)
histogram_quantile(0.95, rate(bot_tick_latency_ms_bucket[5m]))

# Win Rate (Gauge)
bot_win_rate_7d * 100

# Trades opened today (Counter)
increase(bot_trades_opened_total[24h])

# CPU Usage
(100 - (avg(irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))
```

### 4. Alerts konfigurieren

Alerts sind in `prometheus-rules.yml` definiert (15 Regeln).

Sie werden als **Prometheus-Alerts** evaluiert, aber die tatsächlichen **Push-Benachrichtigungen**
gehen über **structlog + Telegram** (siehe `Bot/Alert-System.md`).

**Prometheus Alert-UI:** http://localhost:9090/alerts

---

## Troubleshooting

### Docker Services starten nicht

```bash
# Logs prüfen
docker-compose -f infra/docker-compose-monitoring.yml logs prometheus
docker-compose -f infra/docker-compose-monitoring.yml logs grafana
```

### Prometheus scrapet keine Metriken

1. **Backend läuft?** http://localhost:8000/metrics sollte Metriken zeigen
2. **Prometheus-Config richtig?** `localhost:9090` → **Configuration** → **Targets** prüfen
3. **Firewall?** Port 8000 für Docker-Bridge freigeben

### Grafana-Dashboards leer

1. **Datasource verbunden?** Configuration → Data Sources → Prometheus → **Test**
2. **Metriken da?** Prometheus UI http://localhost:9090 → **Graph** → Query ausführen

### Services stoppen

```bash
docker-compose -f infra/docker-compose-monitoring.yml down
```

Daten bleiben in `prometheus_data/` + `grafana_data/` erhalten.

---

## Performance-Baseline

| Metrik | Normal | Warning | Critical |
|---|---|---|---|
| Tick Latency (p95) | < 200ms | 200–500ms | > 500ms |
| Signal Eval (p95) | < 100ms | 100–250ms | > 250ms |
| Win Rate (7d) | 50–65% | 40–49% | < 40% |
| Drawdown | 0 to −5% | −5% to −10% | < −10% |
| CPU | < 40% | 40–70% | > 70% |
| Memory | < 50% | 50–70% | > 70% |
| Loop Age | < 15s | 15–30s | > 30s |

---

## Integration mit Backend

### 1. prometheus_fastapi_instrumentator

In `backend/src/api/main.py`:

```python
from prometheus_fastapi_instrumentator import Instrumentator

Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
```

**Wichtig:** Muss VOR dem Starten aufgerufen werden.

```bash
pip install prometheus-fastapi-instrumentator
```

### 2. Custom Trading-Metriken

In `backend/src/api/metrics.py`:

```python
from prometheus_client import Counter, Gauge, Histogram

TICK_LATENCY = Histogram(
    "bot_tick_latency_ms",
    "Full tick evaluation latency in ms",
    buckets=(50, 100, 200, 300, 500, 750, 1000, 2000),
    labelnames=("instrument",)
)

EQUITY = Gauge("bot_equity_usd", "Current portfolio equity in USD")
DRAWDOWN_PCT = Gauge("bot_drawdown_pct", "Current drawdown from peak, in %")
```

### 3. Im Live-Loop integrieren

In `backend/src/live/loop.py`:

```python
import time
from src.api.metrics import TICK_LATENCY, EQUITY, DRAWDOWN_PCT

async def _do_tick(instrument: str):
    t0 = time.perf_counter()
    
    # ... Tick-Logik ...
    
    TICK_LATENCY.labels(instrument=instrument).observe(time.perf_counter() - t0)
    EQUITY.set(instance.equity)
    DRAWDOWN_PCT.set(instance.max_dd_pct * 100)
```

---

## Tägliches Monitoring-Runbook

### Morgens (08:00)

```bash
# Dashboard öffnen
open http://localhost:3001/d/trading-bot-status

# Checks:
# ✓ Status = 🟢 healthy?
# ✓ Equity-Trend: seitwärts oder up?
# ✓ Drawdown < 5%?
# ✓ Loop läuft (Last Tick < 15s)?
# ✓ Errors < 5?
```

### Abends (21:00)

```bash
# Analytics-Dashboard öffnen
open http://localhost:3001/d/performance-analytics

# Notieren:
# - Daily PnL: $X
# - Win Rate: Y%
# - Max DD: -Z%
# - Error Count: [count]
```

### Wöchentlich (Montag 08:00)

```bash
# Analytics-Dashboard
# - Win Rate Trend: up/down/stable?
# - Profit Factor: > 1.5?
# - Sharpe: > 1.0?
# - Avg RR: > 1.8?
# → Falls rot: Reoptimierung triggern
```

---

## Referenzen

- **Dokumentation:** `Trading/Bot/Monitoring-Dashboards.md`
- **Observability-Stack:** `Trading/Bot/Observability.md`
- **Alert-System:** `Trading/Bot/Alert-System.md`
- **Live-Monitoring:** `Trading/Bot/Live-Monitoring.md`
- **Prometheus Docs:** https://prometheus.io/docs/
- **Grafana Docs:** https://grafana.com/docs/grafana/latest/

---

## Notizen

- **Datenaufbewahrung:** Prometheus behält 30 Tage (konfigurierbar in docker-compose)
- **Scrape-Intervall:** 15s für Trading-Bot, 30s für System
- **Alert-Evaluation:** Alle 30s (schneller Feedback)
- **Grafana-Speicher:** ~200MB Daten lokal in `grafana_data/`
