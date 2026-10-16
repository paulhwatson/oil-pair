#!/usr/bin/env bash
# Schedules scripts/pnl_spreadsheet.py --snapshot for weekdays at 18:05 (local time) with launchd.
#   ./scripts/install_pnl_spreadsheet.sh install | uninstall | status
# launchd rather than cron: a launchd agent runs a missed job when the Mac wakes.
set -euo pipefail
LABEL="com.paulwatson.oil-pair.pnl-spreadsheet"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$REPO/logs/pnl_spreadsheet"

case "${1:-status}" in
  install)
    {
      echo '<?xml version="1.0" encoding="UTF-8"?>'
      echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
      echo '<plist version="1.0"><dict>'
      echo "  <key>Label</key><string>$LABEL</string>"
      echo "  <key>ProgramArguments</key><array><string>$REPO/.venv/bin/python</string><string>$REPO/scripts/pnl_spreadsheet.py</string><string>--snapshot</string></array>"
      echo "  <key>WorkingDirectory</key><string>$REPO</string>"
      echo '  <key>StartCalendarInterval</key><array>'
      for day in 1 2 3 4 5; do
        echo "    <dict><key>Weekday</key><integer>$day</integer><key>Hour</key><integer>18</integer><key>Minute</key><integer>5</integer></dict>"
      done
      echo '  </array>'
      echo "  <key>StandardOutPath</key><string>$REPO/logs/pnl_spreadsheet/launchd.out.log</string>"
      echo "  <key>StandardErrorPath</key><string>$REPO/logs/pnl_spreadsheet/launchd.err.log</string>"
      echo '</dict></plist>'
    } > "$PLIST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "installed: weekdays 18:05 -> $PLIST"
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
