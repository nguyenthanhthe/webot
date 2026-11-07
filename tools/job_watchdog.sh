#!/data/data/com.termux/files/usr/bin/sh
# Watchdog for the bot service.
#
# It is registered with the Android JobScheduler (termux-job-scheduler)
# and runs every 15 minutes.  If the bot is not running it is started
# again, so the bot survives the system killing the Termux app.
#
# Two extra duties for the 24/7 run:
# - a heartbeat file tells a hung bot from a working one; a bot that
#   holds the lock but stopped beating is killed so the next run of
#   this watchdog starts it fresh,
# - the log is rotated in place before it grows without bound.
set -u

HOME_DIR=/data/data/com.termux/files/home
LOCK="$HOME_DIR/service/bot.lock"
LOG="$HOME_DIR/wv-bot.log"
HEARTBEAT="$HOME_DIR/.wv-bot/heartbeat.txt"
# The bot may legally sit in one wait for up to two hours (a server
# ban); anything beyond that is a hang.
STALE_SEC=8000
MAX_LOG_BYTES=5242880

export PATH="/data/data/com.termux/files/usr/bin:$PATH"
export HOME="$HOME_DIR"

rotate_log() {
  [ -f "$LOG" ] || return 0
  size=$(wc -c < "$LOG" 2>/dev/null || echo 0)
  [ "$size" -gt "$MAX_LOG_BYTES" ] || return 0
  # Truncate IN PLACE: the runner holds an append fd on this inode,
  # a rename would send every further line to the orphaned file.
  tail -n 2000 "$LOG" > "$LOG.rot" 2>/dev/null || true
  cat "$LOG.rot" > "$LOG" 2>/dev/null || true
  rm -f "$LOG.rot"
  echo "rotated wv-bot.log ($size bytes)"
}

if [ -d "$LOCK" ]; then
  runner_pid=$(cat "$LOCK/pid" 2>/dev/null || echo "?")
  if [ -f "$HEARTBEAT" ]; then
    beat=$(cat "$HEARTBEAT" 2>/dev/null || echo 0)
    now=$(date +%s)
    age=$((now - beat))
    if [ "$age" -gt "$STALE_SEC" ]; then
      echo "the bot heartbeat is ${age}s old - killing the hung run (pid $runner_pid)"
      kill -9 "$runner_pid" 2>/dev/null || true
      for entry in /proc/[0-9]*; do
        pid=${entry#/proc/}
        [ "$pid" = "$$" ] && continue
        [ -r "$entry/cmdline" ] || continue
        cmd=$(tr '\0' ' ' < "$entry/cmdline" 2>/dev/null || true)
        case "$cmd" in
          *"run.py"*|*"wvbot"*)
            kill -9 "$pid" 2>/dev/null || true
            ;;
        esac
      done
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
