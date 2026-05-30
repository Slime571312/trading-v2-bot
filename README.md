# trading-v2 — Greenfield Bot v2

ICT-Multi-TF-Stack-Bot + Backtest-Engine + Chat-Interface. Komplett-Neubau,
nichts geteilt mit v1 (`~/trading-ui`, `~/freqtrade`, `~/trading-ui/backtest-py`
— alle eingefroren).

**Strategie-Spec** lebt im Obsidian-Vault, nicht hier: siehe
`~/Desktop/tradingbot/Trading/Bot/Playbook.md` + `Architektur-v2.md`.

## Stack

| Schicht | Tech | Pfad |
|---|---|---|
| Frontend | Next.js 16 + TypeScript + Tailwind v4 + TradingView Lightweight Charts | `frontend/` |
| Backend | Python 3.11 + FastAPI + Pydantic | `backend/` |
| Broker | Capital.com (Demo Paper → Live möglich) | `backend/src/brokers/capital_com.py` |
| Chat | Anthropic Claude API (Sonnet 4.6 + Opus 4.7) | `backend/src/chat/` |

## Roadmap — 7 Etappen

Jede Etappe wird **erst abgeschlossen + verifiziert** bevor die nächste startet.

| ✓ | # | Etappe | Verifikations-Kriterium |
|---|---|---|---|
| ✓ | **0** | Skeleton | `curl :8000/health` antwortet, Vault-Doku verlinkt |
| ✓ | **1** | Data-Layer | Capital.com-Adapter (Session + 401/429-Retry), OHLCV für DE40/NASDAQ/SP500/BTC auf 1d/1h/30m/15m/5m/1m, CSV-Cache, `/data/{instrument}`-Endpoint — 24/24 Smoke-Test grün |
| ✓ | **2** | Strategy-Core | 9 stateless Module (pivots/bias/liquidity/sweep/structure/zones/rr/sessions/engine) + 21/21 Unit-Tests grün; `/signal/{instrument}` + `/diagnose/{instrument}`-Endpoints; Live-Test gegen Capital lieferte konkretes ob_retest-Setup auf BTC mit RR 2.54 |
| ✓ | **3** | Backtest-Engine | Bar-für-Bar-Replay, realistisches Slippage/Spread-Modell, Equity-Curve, HTML-Report (Bokeh), Walk-Forward-Skeleton (rollende OOS-Fenster); `POST /backtest`-Endpoint; 31/31 Tests grün |
| ✓ | **4** | Frontend-Skeleton | Next.js 15 + Tailwind v4; Dashboard (4 Signal-Cards, Auto-Refresh 30s), Backtest-View (Form + Metriken-Grid + TradingView-Equity-Chart + Trade-Liste + WFO-Badges), Live/Chat-Stubs; Build grün (5 Routen) |
| ✓ | **5** | Live-Paper-Bot | Asyncio-Tick-Loop (REST-Poll, 60s default) ruft Strategy-Core, Paper-Broker mit Slippage-Modell (shared mit Backtest), intrabar SL/TP-Check, atomar persistierte JSON-State; FastAPI-WebSocket `/ws/live` mit Fan-Out/Backpressure/Heartbeat → Browser-Live-Push; `/bot/{state,start,stop,reset}` Endpoints; Live-View mit Auto-Reconnect-Hook; 48/48 Tests; Live-Smoke: BTC short @ 75370 mit RR 3.47 opened |
| ✓ | **6** | Chat-Bot | **Claude Agent SDK** via Claude-Code-Subscription (KEIN ANTHROPIC_API_KEY); 6 Tools als MCP-Server (`mcp__bot_tools__*`): `read_status`, `get_recent_trades`, `get_signal`, `diagnose`, `run_backtest`, `propose_config_diff`; Multi-Turn-Tool-Loop + Session-Resumption von der SDK gemanaged; Conversation-Persistenz mit Turn-Schema + session_id; Proposal-Queue (Read+Trigger+Vorschlag-Scope, Direkt-Apply braucht User-Klick); Frontend: Chat-UI mit Tool-Use-Badges + Proposals-Panel + Model-Toggle (sonnet/opus/haiku); Live-Smoke: 7s response, Opus 4.7, read_status korrekt aufgerufen |
| ✓ | **7** | Self-Improve-Tuner | Param-Grid-Search (rr_threshold × sweep_lookback = 12 Combos pro Instrument), Score = expectancy_r + log(PF) mit min-5-trades-Filter; vergleicht Best-Combo mit Live-Params, erzeugt Proposal in der Etappe-6-Queue (Approval-Flow shared); optional Claude-Opus-4.7-Reasoning für die Rationale (Fallback Template wenn kein API-Key); launchd-Plist für nächtlichen 03:00-Auto-Run; Frontend `/tuner` mit Run-Form + History + Combo-Tabellen mit ★best ●live; 71/71 Tests; Smoke: BTC-Run 17s, fand +2.10R Expectancy Combo (rr=2.5 sl=20) |

## Setup

Einmalig (schon erledigt für Etappe 0):

```bash
cd ~/trading-v2
/opt/homebrew/bin/python3.11 -m venv backend/.venv
backend/.venv/bin/pip install -e backend/
cp .env.example .env  # Capital.com + Anthropic Credentials eintragen
```

## Starten

```bash
cd ~/trading-v2
bash infra/start.sh
```

Backend läuft auf `http://localhost:8000`, Docs auf `http://localhost:8000/docs`,
Frontend (ab Etappe 4) auf `http://localhost:3000`.

## Stoppen

```bash
bash infra/stop.sh
```

## Verzeichnisstruktur

```
backend/
  src/
    api/              # FastAPI-Routen
    strategy_core/    # Playbook-Logik — shared Backtest+Live
    backtest/         # Historische Replays, Reports
    live/             # Live-Paper-Bot-Loop
    brokers/          # Capital.com + Paper-Broker
    data/             # OHLCV fetcher + cache + resampler
    tuner/            # Nightly Self-Improve
    chat/             # Anthropic-Integration
    config.py
  tests/

frontend/             # Next.js (ab Etappe 4)

shared/               # Pydantic-Schemas → TS-Types

infra/                # start.sh, stop.sh, launchd-plists
```

## Spec-Quellen

- Strategie-Logik → `~/Desktop/tradingbot/Trading/Bot/Playbook.md`
- System-Architektur → `~/Desktop/tradingbot/Trading/Bot/Architektur-v2.md`
- Decision-Log → `~/Desktop/tradingbot/Trading/Bot/Decisions.md`
- ICT-Konzepte → `~/Desktop/tradingbot/Trading/` (FVG, BOS, LQS, …)
