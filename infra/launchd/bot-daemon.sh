#!/usr/bin/env bash
# Foreground-Uvicorn für launchd. launchd erwartet einen Prozess der NICHT
# im Hintergrund läuft — sonst denkt er, das Programm sei fertig und respawned.
# KEIN --reload (würde child-Prozess spawnen → launchd kann nicht tracken).
#
# Sleep-Schutz: caffeinate wickelt uvicorn ein, damit der Mac nicht in
# System-Sleep geht solange der Bot läuft. Flags:
#   -i  System-Idle-Sleep blockieren (Bot tickt weiter)
#   -m  Disk-Sleep blockieren (State-Persist sofort)
#   -s  System-Sleep verhindern (nur bei Strom; ohne -s schläft Mac auf Akku)
# Wenn der User Akku-Sleep wirklich will, kann er `-s` rausnehmen.

set -euo pipefail

ROOT="/Users/simonzernahle/trading-v2"
cd "$ROOT"

# .env laden falls vorhanden
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# Backend muss venv haben — sonst ist die Installation kaputt
if [ ! -x backend/.venv/bin/uvicorn ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] FATAL: backend/.venv/bin/uvicorn fehlt" >&2
  exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starte Bot-Daemon auf ${HOST:-127.0.0.1}:${PORT:-8000} (caffeinate aktiv)"

# Log-Rotation: launchd-StandardOut/Err landen in /tmp/trading-v2-bot.launchd.*log.
# Bei jedem Boot-Daemon-Start: wenn aktuelles Log > 10 MB → wegrotieren (max 7
# Backups). Verhindert dass die Log-Datei unbegrenzt wächst, ohne externes
# logrotate-Setup (das auf macOS user-domain nicht zuverlässig läuft).
rotate_log() {
    local f="$1"
    [ -f "$f" ] || return 0
    local size=$(stat -f%z "$f" 2>/dev/null || echo 0)
    if [ "$size" -gt 10485760 ]; then   # > 10 MB
        # Vorhandene Rotationen weiterschieben (.6 → .7, .5 → .6, …)
        for i in 6 5 4 3 2 1; do
            [ -f "$f.$i" ] && mv "$f.$i" "$f.$((i+1))"
        done
        # .8 löschen falls vorhanden (max 7 Backups behalten)
        rm -f "$f.8"
        mv "$f" "$f.1"
        echo "[$(date '+%H:%M:%S')] Log rotiert: $f → $f.1 ($size bytes)"
    fi
}
rotate_log "/tmp/trading-v2-bot.launchd.log"
rotate_log "/tmp/trading-v2-bot.launchd.err"

# `exec caffeinate -ims` macht caffeinate zu DIESEM Prozess und das letzte
# Argument (uvicorn) wird als child gestartet. Wenn uvicorn beendet, beendet
# caffeinate auch — launchd respawnt entsprechend KeepAlive-Regel.
exec /usr/bin/caffeinate -ims \
  backend/.venv/bin/uvicorn src.api.main:app \
  --app-dir backend \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-8000}" \
  --log-level "${LOG_LEVEL:-info}"
