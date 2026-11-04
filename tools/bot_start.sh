#!/data/data/com.termux/files/usr/bin/sh
# Start the bot in the background and show the last lines of its log.
#
#     sh bot_start.sh
#
# The log lives in the Termux home so it survives a reboot.
set -e

HOME_DIR=/data/data/com.termux/files/home
BOT_DIR="$HOME_DIR/wv-bot"
LOG="$HOME_DIR/wv-bot.log"
PIDFILE="$HOME_DIR/wv-bot.pid"

export PATH="/data/data/com.termux/files/usr/bin:$PATH"
export HOME="$HOME_DIR"
export TMPDIR="$HOME_DIR/tmp"

cd "$BOT_DIR" || exit 1

# Stop every running instance, not just the one in the pid file: an old
# instance still holds the dashboard port.  Termux has no pkill, so the
# process list is read from /proc (the own-uid entries only).
for entry in /proc/[0-9]*; do
  pid=${entry#/proc/}
  cmd=$(tr '\0' ' ' < "$entry/cmdline" 2>/dev/null || true)
  case "$cmd" in
    *run.py*)
      echo "stopping pid $pid"
      kill -9 "$pid" 2>/dev/null || true
      ;;
  esac
done
sleep 3
rm -f "$PIDFILE"

nohup python3 run.py --mode protocol >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "started pid $(cat "$PIDFILE"), log: $LOG"
sleep 5
tail -20 "$LOG"
