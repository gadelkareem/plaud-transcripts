#!/usr/bin/env bash
# Run ./run.sh automatically in the background (macOS, per-user launchd job).
#
#   ./schedule.sh install [minutes]   # default: every 60 minutes, and at login
#   ./schedule.sh status
#   ./schedule.sh uninstall
#
# The job fetches new recordings, transcribes, summarises and tags them. It never opens
# the browser: if the Plaud login expires you get a notification to run ./run.sh once.
set -euo pipefail
cd "$(dirname "$0")"

LABEL="local.plaud-transcripts.sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LIBRARY="${PLAUD_LIBRARY:-$PWD/library}"
LOG="$LIBRARY/auto.log"
DOMAIN="gui/$(id -u)"

[ "$(uname)" = "Darwin" ] || { echo "ERROR: schedule.sh uses launchd and only works on macOS." >&2; exit 1; }

case "${1:-}" in
  install)
    minutes="${2:-60}"
    case "$minutes" in ''|*[!0-9]*) echo "ERROR: minutes must be a number." >&2; exit 1 ;; esac
    [ "$minutes" -ge 5 ] || { echo "ERROR: use 5 minutes or more." >&2; exit 1; }
    mkdir -p "$(dirname "$PLIST")" "$LIBRARY"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$PWD/run.sh</string></array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NO_OPEN</key><string>1</string>
    <key>NO_LOGIN</key><string>1</string>
    <key>PLAUD_LIBRARY</key><string>$LIBRARY</string>
    <key>HF_HUB_DISABLE_PROGRESS_BARS</key><string>1</string>
  </dict>
  <key>StartInterval</key><integer>$((minutes * 60))</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "Installed: runs every $minutes minutes while this Mac is awake, and at login."
    echo "Log: $LOG"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed the scheduled job."
    ;;
  status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl print "$DOMAIN/$LABEL" | grep -E "state =|run interval|last exit code|runs =" || true
      echo "Log: $LOG"; tail -n 8 "$LOG" 2>/dev/null || true
    else
      echo "Not installed. Run: ./schedule.sh install"
    fi
    ;;
  *) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
