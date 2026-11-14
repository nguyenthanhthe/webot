#!/data/data/com.termux/files/usr/bin/sh
# Watchdog for the bot service.
#
# It is registered with the Android JobScheduler (termux-job-scheduler)
# and runs every 15 minutes.  If the bot is not running it is started
# again, so the bot survives the system killing the Termux app.
#
# Two extra duties for the 24/7 run:
# - a heartbeat file tells a hung bot from a working one; a bot that
#   holds the lock but stopped beating is killed and restarted cleanly,
# - the log is rotated in place before it grows without bound.
set -u

HOME_DIR=/data/data/com.termux/files/home
STATE_DIR="${WV_BOT_STATE_DIR:-$HOME_DIR/.wv-bot}"
LOCK="$HOME_DIR/service/bot.lock"
PIDFILE="$HOME_DIR/wv-bot.pid"
LOG="$HOME_DIR/wv-bot.log"
# The bot may legally sit in one wait for up to two hours (a server
# ban); anything beyond that is a hang.
STALE_SEC=8000
MAX_LOG_BYTES=5242880

export PATH="/data/data/com.termux/files/usr/bin:$PATH"
export HOME="$HOME_DIR"

rotate_log() {
  [ -f "$LOG" ] || return 0
  size=$(wc -c < "$LOG" 2>/dev/null | tr -cd '0-9')
  [ -n "$size" ] || size=0
  [ "$size" -gt "$MAX_LOG_BYTES" ] || return 0
  # Truncate IN PLACE: the runner holds an append fd on this inode,
  # a rename would send every further line to the orphaned file.
  tail -n 2000 "$LOG" > "$LOG.rot" 2>/dev/null || true
  cat "$LOG.rot" > "$LOG" 2>/dev/null || true
  rm -f "$LOG.rot"
  echo "rotated wv-bot.log ($size bytes)"
}

latest_heartbeat() {
  best=""
  for hb in "$STATE_DIR/heartbeat.txt" "$STATE_DIR"/accounts/*/heartbeat.txt; do
    [ -f "$hb" ] || continue
    raw=$(cat "$hb" 2>/dev/null || true)
    # Heartbeats are float epoch seconds; strip decimal portion for POSIX sh.
    int_val=$(printf '%s' "${raw%%.*}" | tr -cd '0-9')
    [ -n "$int_val" ] || continue
    if [ -z "$best" ] || [ "$int_val" -gt "$best" ]; then
      best="$int_val"
    fi
  done
  printf '%s' "$best"
}

kill_hung_bot() {
  runner_pid="$1"
  case "$runner_pid" in
    ''|*[!0-9]*) ;;
    *) kill -9 "$runner_pid" 2>/dev/null || true ;;
  esac
  for entry in /proc/[0-9]*; do
    pid=${entry#/proc/}
    [ "$pid" = "$$" ] && continue
    [ -r "$entry/cmdline" ] || continue
    cmd=$(tr '\0' ' ' < "$entry/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *"run.py"*|*"wvbot"*|*"service/runner.sh"*)
        kill -9 "$pid" 2>/dev/null || true
        ;;
    esac
  done
  rm -rf "$LOCK" 2>/dev/null || true
  rm -f "$PIDFILE" 2>/dev/null || true
}

if [ -d "$LOCK" ]; then
  runner_pid=$(cat "$LOCK/pid" 2>/dev/null || echo "?")
  beat=$(latest_heartbeat)
  if [ -n "$beat" ]; then
    now=$(date +%s)
    age=$((now - beat))
    if [ "$age" -gt "$STALE_SEC" ]; then
      echo "the bot heartbeat is ${age}s old - killing hung run (pid $runner_pid) and restarting"
      kill_hung_bot "$runner_pid"
      rotate_log
      sh "$HOME_DIR/bot_ctl.sh" start
      exit 0
    fi
    echo "the bot runner is active (pid $runner_pid, heartbeat ${age}s old)"
  else
    echo "the bot runner is active (pid $runner_pid, no heartbeat yet)"
  fi
  rotate_log
  exit 0
fi

echo "the bot runner is not active - starting it again"
rotate_log
sh "$HOME_DIR/bot_ctl.sh" start
