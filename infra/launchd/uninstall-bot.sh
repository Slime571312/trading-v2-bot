#!/usr/bin/env bash
# Stoppt + deregistriert den Bot-Daemon.
# Damit läuft der Bot NICHT mehr automatisch bei Login.

set -euo pipefail

LABEL="com.simonzernahle.trading-v2.bot"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  launchctl bootout "${DOMAIN}/${LABEL}"
  echo "✓ Service deregistriert"
else
  echo "→ Service nicht registriert — nichts zu tun"
fi

if [ -f "$PLIST_DST" ]; then
  rm -f "$PLIST_DST"
  echo "✓ plist entfernt"
fi

echo "Bot-Daemon ist aus. Backend manuell starten: bash infra/start.sh"
