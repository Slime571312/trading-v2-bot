#!/usr/bin/env bash
# Installiert + startet den Bot-Daemon als launchd-Job (24/7 mit KeepAlive).
#
# Was er macht:
#   - kopiert plist nach ~/Library/LaunchAgents/
#   - bootstrap (load) in die User-launchd-Domain
#   - kickstart (= sofortiger Start)
#
# Vorher: sicherstellen, dass kein anderer uvicorn auf Port 8000 läuft!
#         (z.B. bash infra/stop.sh aufrufen)

set -euo pipefail

LABEL="com.simonzernahle.trading-v2.bot"
PLIST_SRC="/Users/simonzernahle/trading-v2/infra/launchd/${LABEL}.plist"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

if [ ! -f "$PLIST_SRC" ]; then
  echo "✗ plist nicht gefunden: $PLIST_SRC" >&2
  exit 1
fi

# Falls Service schon läuft, erst rausnehmen (idempotent)
if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  echo "→ Service bereits registriert, bootout zuerst…"
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
  sleep 1
fi

# Falls noch ein manuell gestarteter uvicorn auf Port 8000 hängt, warnen
if lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✗ Port 8000 ist belegt — bitte zuerst 'bash infra/stop.sh' aufrufen" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"
echo "✓ plist kopiert nach $PLIST_DST"

launchctl bootstrap "$DOMAIN" "$PLIST_DST"
echo "✓ Service registriert in $DOMAIN"

launchctl kickstart "${DOMAIN}/${LABEL}"
echo "✓ Service gestartet"

echo
echo "Nützliche Kommandos:"
echo "  Status:    launchctl print ${DOMAIN}/${LABEL} | head -30"
echo "  Logs:      tail -f /tmp/trading-v2-bot.launchd.log"
echo "  Fehler:    tail -f /tmp/trading-v2-bot.launchd.err"
echo "  Stop:      bash /Users/simonzernahle/trading-v2/infra/launchd/uninstall-bot.sh"
