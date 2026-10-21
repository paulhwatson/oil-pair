#!/usr/bin/env bash
# Schedules the earnings-miss shorts (earnings_short/main.py, demo) with launchd.
#   ./scripts/install_earnings_short.sh install | uninstall | status
# It acts at about 15:45 New York, which is 20:45 UK most of the year but 19:45 UK in the weeks when the
# clocks change on different dates - so it fires at 19:40, 19:50, 20:40 and 20:50 UK each weekday and the
# run itself acts only inside its New York window (config/earnings_short.toml), once per session.
# launchd rather than cron: a launchd agent runs a missed job when the Mac wakes.
set -euo pipefail
LABEL="com.paulwatson.oil-pair.earnings-short"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$REPO/logs/earnings_short"

case "${1:-status}" in
  install)
    {
      echo '<?xml version="1.0" encoding="UTF-8"?>'
      echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
      echo '<plist version="1.0"><dict>'
      echo "  <key>Label</key><string>$LABEL</string>"
      echo "  <key>ProgramArguments</key><array><string>$REPO/.venv/bin/python</string><string>-m</string><string>earnings_short.main</string></array>"
      echo "  <key>WorkingDirectory</key><string>$REPO</string>"
      echo '  <key>StartCalendarInterval</key><array>'
      for day in 1 2 3 4 5; do
        for hm in "19 40" "19 50" "20 40" "20 50"; do
          set -- $hm
          echo "    <dict><key>Weekday</key><integer>$day</integer><key>Hour</key><integer>$1</integer><key>Minute</key><integer>$2</integer></dict>"
        done
      done
      echo '  </array>'
      echo "  <key>StandardOutPath</key><string>$REPO/logs/earnings_short/launchd.out.log</string>"
      echo "  <key>StandardErrorPath</key><string>$REPO/logs/earnings_short/launchd.err.log</string>"
      echo '</dict></plist>'
    } > "$PLIST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "installed: weekdays 19:40/19:50/20:40/20:50 UK (acts once, ~15:45 New York) -> $PLIST"
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
