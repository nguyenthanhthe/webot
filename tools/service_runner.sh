#!/data/data/com.termux/files/usr/bin/sh
# The bot runner.
#
# It is started by the Termux background service
# (com.termux/.app.RunCommandService) instead of a terminal session, so
# the bot keeps running after every Termux tab is closed.
#
# The runner owns the lock file, so a second runner can never start
# while this one is alive.  It also watches the stop flag: the bot can
# only be stopped from inside the Termux app itself, because the
# processes run in the app's own SELinux domain and neither adb nor
# "run-as" is allowed to kill them.
set -u

HOME_DIR=/data/data/com.termux/files/home
BOT_DIR="$HOME_DIR/wv-bot"
LOG="$HOME_DIR/wv-bot.log"
PIDFILE="$HOME_DIR/wv-bot.pid"
LOCK="$HOME_DIR/service/bot.lock"
STOPFLAG="$HOME_DIR/service/bot.stop"

export PATH="/data/data/com.termux/files/usr/bin:$PATH"
export HOME="$HOME_DIR"
export TMPDIR="$HOME_DIR/tmp"
export PYTHONUNBUFFERED=1
export LANG=en_US.UTF-8

cd "$BOT_DIR" || exit 1
mkdir -p "$TMPDIR"

# Kill leftover bots and runners of an earlier start.  Only the
# processes of this app are visible here, and they share the SELinux
# domain of this script, so killing them is allowed.
for entry in /proc/[0-9]*; do
  pid=${entry#/proc/}
  [ "$pid" = "$$" ] && continue
  [ -r "$entry/cmdline" ] || continue
  cmd=$(tr '\0' ' ' < "$entry/cmdline" 2>/dev/null || true)
  case "$cmd" in
    *"run.py --mode protocol"*|*"service/runner.sh"*)
      echo "stopping the leftover pid $pid" >> "$LOG"
      kill -9 "$pid" 2>/dev/null || true
      ;;
  esac
done
sleep 1

# The lock is a directory with the pid inside.  A lock left behind by a
# crashed runner is taken over, a lock held by a live runner is not.
if mkdir "$LOCK" 2>/dev/null; then
  :
else
  old_pid=$(cat "$LOCK/pid" 2>/dev/null || echo "")
  if [ -n "$old_pid" ] && [ -d "/proc/$old_pid" ]; then
    echo "another runner is active (pid $old_pid) - exiting"
    exit 0
  fi
  echo "taking over a stale lock from pid $old_pid" >> "$LOG"
  rm -rf "$LOCK"
  mkdir "$LOCK" || exit 1
fi

echo $$ > "$LOCK/pid"
echo $$ > "$PIDFILE"

# The bot has to answer the game server on time, so the CPU must not
# go to sleep while it runs.
termux-wake-lock >/dev/null 2>&1 || true

# The bot runs as a child, so the stop flag can end it at any time.
python3 run.py --mode protocol >> "$LOG" 2>&1 &
bot_pid=$!

while kill -0 "$bot_pid" 2>/dev/null; do
  if [ -f "$STOPFLAG" ]; then
    echo "--- stop flag found, stopping the bot ---" >> "$LOG"
    kill -9 "$bot_pid" 2>/dev/null || true
    break
  fi
  sleep 2
done

wait "$bot_pid" 2>/dev/null
echo "--- bot runner exiting ---" >> "$LOG"
rm -rf "$LOCK" 2>/dev/null
rm -f "$PIDFILE" 2>/dev/null
