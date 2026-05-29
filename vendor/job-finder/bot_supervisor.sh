#!/bin/bash
# Hourly supervisor — checks if the job-finder bot is running, restarts if not.
# Logs to bot_health.log. Run with: nohup ./bot_supervisor.sh &
set -u

BOT_DIR="/Users/sagarverma/Pictures/Claude-experiments/job-finder"
HEALTH_LOG="$BOT_DIR/bot_health.log"
RUN_LOG="$BOT_DIR/bot_runs.log"
PROCESS_PATTERN="main.py.*--mode date.*--nonstop"

cd "$BOT_DIR" || exit 1

while true; do
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  pid="$(pgrep -f "$PROCESS_PATTERN" | head -1 || true)"

  if [ -n "$pid" ]; then
    # Bot alive — count applies in current run
    out_file="$(ls -t /private/tmp/claude-501/-Users-sagarverma-Pictures-Claude-experiments/*/tasks/*.output 2>/dev/null | head -1)"
    applies=0
    if [ -n "$out_file" ]; then
      applies=$(grep -cE "Applied: " "$out_file" 2>/dev/null || echo 0)
    fi
    echo "[$ts] ALIVE pid=$pid applies-since-start=$applies" >> "$HEALTH_LOG"
  else
    # Bot dead — find reason from most recent bot_runs.log entries
    reason="unknown"
    if [ -f "$RUN_LOG" ]; then
      last="$(tail -200 "$RUN_LOG" 2>/dev/null)"
      if echo "$last" | grep -q "Session expired"; then
        reason="Session expired — needs setup_chrome_profile.py"
      elif echo "$last" | grep -q "Acquired Seek lock.*RuntimeError"; then
        reason="Lock conflict (another seek session running)"
      elif echo "$last" | grep -qE "Traceback \(most recent"; then
        reason="$(echo "$last" | grep -E "^[A-Z][a-zA-Z]+Error|^Exception" | tail -1 | head -c 200)"
      elif echo "$last" | grep -qE "Sleeping [0-9]+s before next scrape"; then
        reason="Process exited after a normal scrape cycle (likely OS killed or manual stop)"
      else
        reason="No clear error in run log; check $RUN_LOG"
      fi
    fi
    echo "[$ts] DEAD reason=$reason — attempting restart" >> "$HEALTH_LOG"

    # Clear stale Chrome locks + orphaned Chrome processes
    pkill -f "Google Chrome.*seek_chrome_profile" 2>/dev/null || true
    sleep 2
    rm -f "$BOT_DIR/sessions/seek_chrome_profile/SingletonLock" \
          "$BOT_DIR/sessions/seek_chrome_profile/SingletonCookie" \
          "$BOT_DIR/sessions/seek_chrome_profile/SingletonSocket" \
          "$BOT_DIR/sessions/seek/.lock"

    # Restart bot
    cd "$BOT_DIR" || exit 1
    nohup venv/bin/python main.py --mode date --nonstop >> "$RUN_LOG" 2>&1 &
    sleep 10

    # Verify
    new_pid="$(pgrep -f "$PROCESS_PATTERN" | head -1 || true)"
    if [ -n "$new_pid" ]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] RESTART OK new_pid=$new_pid" >> "$HEALTH_LOG"
    else
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] RESTART FAILED — manual intervention needed" >> "$HEALTH_LOG"
    fi
  fi

  sleep 3600
done
