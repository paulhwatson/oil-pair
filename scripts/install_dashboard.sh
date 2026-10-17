#!/usr/bin/env bash
# Keeps the dashboard (dashboard/app.py) running with launchd, at http://localhost:8501.
#   ./scripts/install_dashboard.sh install | install --phone | uninstall | status
# --phone also serves it to other devices on your network (e.g. your phone on
# home Wi-Fi, at http://<this Mac's name>.local:8501) - read-only, but anyone on
# the same network could view it, so only on a network you trust.
set -euo pipefail
LABEL="com.paulwatson.oil-pair.dashboard"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=8501
mkdir -p "$REPO/logs/dashboard"

case "${1:-status}" in
  install)
    ADDRESS="localhost"
    [ "${2:-}" = "--phone" ] && ADDRESS="0.0.0.0"
    {
      echo '<?xml version="1.0" encoding="UTF-8"?>'
      echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
      echo '<plist version="1.0"><dict>'
      echo "  <key>Label</key><string>$LABEL</string>"
      echo "  <key>ProgramArguments</key><array>"
      for arg in "$REPO/.venv/bin/streamlit" run "$REPO/dashboard/app.py" --server.headless true \
                 --server.port "$PORT" --server.address "$ADDRESS" --browser.gatherUsageStats false; do
        echo "    <string>$arg</string>"
      done
      echo "  </array>"
      echo "  <key>WorkingDirectory</key><string>$REPO</string>"
      echo "  <key>RunAtLoad</key><true/>"
      echo "  <key>KeepAlive</key><true/>"
      echo "  <key>StandardOutPath</key><string>$REPO/logs/dashboard/launchd.out.log</string>"
      echo "  <key>StandardErrorPath</key><string>$REPO/logs/dashboard/launchd.err.log</string>"
      echo '</dict></plist>'
    } > "$PLIST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "installed: http://localhost:$PORT (address $ADDRESS) -> $PLIST"
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $LABEL"
    ;;
  status)
    launchctl list | grep "$LABEL" || echo "$LABEL is not loaded"
    ;;
esac
