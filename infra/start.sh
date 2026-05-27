#!/usr/bin/env bash
# Starte trading-v2 lokal — Backend (FastAPI/uvicorn) immer, Frontend (Next.js)
# nur wenn frontend/package.json existiert (kommt in Etappe 4).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# .env in den Prozess laden, falls vorhanden
if [ -f .env ]; then
  export $(grep -v '^#' .env | sed 's/\r$//' | xargs)
fi

# ── Backend ─────────────────────────────────────────────────────────────
if [ ! -d backend/.venv ]; then
  echo "✗ backend/.venv fehlt — erst venv anlegen und 'pip install -e backend/' laufen lassen."
  exit 1
fi
echo "→ Backend startet auf http://${HOST:-127.0.0.1}:${PORT:-8000} (Docs: /docs)"
backend/.venv/bin/uvicorn src.api.main:app \
  --app-dir backend \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-8000}" \
  --log-level "${LOG_LEVEL:-info}" \
  --reload &
BACKEND_PID=$!
echo "  Backend PID: $BACKEND_PID"
echo "$BACKEND_PID" > "$ROOT/.backend.pid"

# ── Frontend (ab Etappe 4) ──────────────────────────────────────────────
if [ -f frontend/package.json ]; then
  echo "→ Frontend startet auf http://localhost:3000"
  (cd frontend && npm run dev) &
  FRONTEND_PID=$!
  echo "  Frontend PID: $FRONTEND_PID"
  echo "$FRONTEND_PID" > "$ROOT/.frontend.pid"
fi

echo
echo "✓ Läuft. Stoppen: bash infra/stop.sh"
wait
