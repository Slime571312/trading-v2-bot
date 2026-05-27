#!/usr/bin/env bash
# Beendet Backend + Frontend, falls .pid-Files vorhanden.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

for kind in backend frontend; do
  pidfile=".${kind}.pid"
  if [ -f "$pidfile" ]; then
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      echo "→ stoppe ${kind} (PID $pid)"
      kill "$pid" || true
    fi
    rm -f "$pidfile"
  fi
done

# Restliche uvicorn/next-Reste killen (Reload-Worker)
pkill -f 'uvicorn src.api.main' 2>/dev/null || true
pkill -f 'next dev' 2>/dev/null || true

echo "✓ trading-v2 gestoppt"
