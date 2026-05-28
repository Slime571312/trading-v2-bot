#!/usr/bin/env bash
# Wird vom launchd-Job aufgerufen — triggert den Tuner via HTTP, wenn Backend läuft.
# Logs nach /tmp/trading-v2-tuner.log
set -euo pipefail

LOG="/tmp/trading-v2-tuner.log"
exec >> "$LOG" 2>&1

echo "── $(date '+%Y-%m-%d %H:%M:%S') Tuner-Cron getriggert ──"

# Backend läuft?
if ! curl -s -f -m 3 http://127.0.0.1:8000/health > /dev/null; then
  echo "  Backend offline → skip"
  exit 0
fi

# Tuner schon am Laufen?
RUNNING=$(curl -s -m 5 http://127.0.0.1:8000/tuner/status | grep -o '"is_running":[^,]*' | cut -d: -f2)
if [ "$RUNNING" = "true" ]; then
  echo "  Tuner läuft bereits → skip"
  exit 0
fi

echo "  → POST /tuner/run (cron-triggered, 1000 bars, use_claude=true)"
HTTP_CODE=$(curl -s -o /tmp/tuner-response.json -w "%{http_code}" \
  -X POST http://127.0.0.1:8000/tuner/run \
  -H "Content-Type: application/json" \
  -d '{"bars": 1000, "use_claude": true, "triggered_by": "cron"}' \
  --max-time 1800)

if [ "$HTTP_CODE" = "200" ]; then
  RUN_ID=$(grep -o '"id":"[^"]*"' /tmp/tuner-response.json | head -1 | cut -d'"' -f4)
  N_PROP=$(grep -o '"proposal_ids":\[[^]]*\]' /tmp/tuner-response.json | tr ',' '\n' | wc -l | tr -d ' ')
  echo "  ✓ Tuner fertig: run=$RUN_ID, proposals~$N_PROP"
else
  echo "  ✗ Tuner gescheitert (HTTP $HTTP_CODE)"
  head -3 /tmp/tuner-response.json
fi
