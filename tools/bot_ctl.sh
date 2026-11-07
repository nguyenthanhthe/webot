#!/data/data/com.termux/files/usr/bin/sh
# Control the bot service.
#
#     sh ~/bot_ctl.sh start | stop | restart | status
#
# The bot runs inside the Termux background service, so it survives
# closing every Termux tab.
#
# The bot processes live in the SELinux domain of the Termux app, so
# neither adb nor "run-as" may kill them.  Stopping is done with a flag
# file that the runner itself watches.
set -u

HOME_DIR=/data/data/com.termux/files/home
BOT_DIR="$HOME_DIR/wv-bot"
RUNNER="$HOME_DIR/service/runner.sh"
LOG="$HOME_DIR/wv-bot.log"
PIDFILE="$HOME_DIR/wv-bot.pid"
LOCK="$HOME_DIR/service/bot.lock"
STOPFLAG="$HOME_DIR/service/bot.stop"
SERVICE=com.termux/.app.RunCommandService

export PATH="/data/data/com.termux/files/usr/bin:$PATH"
export HOME="$HOME_DIR"

start_service() {
  rm -f "$STOPFLAG"

  am startservice -n "$SERVICE" \
    -a com.termux.RUN_COMMAND \
    --es com.termux.RUN_COMMAND_PATH /data/data/com.termux/files/usr/bin/sh \
    --esa com.termux.RUN_COMMAND_ARGUMENTS "$RUNNER" \
    --ez com.termux.RUN_COMMAND_BACKGROUND true \
    --es com.termux.RUN_COMMAND_WORKDIR "$BOT_DIR" \
    --es com.termux.RUN_COMMAND_STDOUT "$HOME_DIR/wv-bot.service.log" \
    --es com.termux.RUN_COMMAND_STDERR "$HOME_DIR/wv-bot.service.log" \
    >/dev/null 2>&1
  echo "service start requested"
}

stop_service() {
  # The runner notices the flag and ends the bot by itself.
  touch "$STOPFLAG"
  # The runner SIGTERMs the bot (it announces its leave and exits)
  # and only falls back to SIGKILL after a grace.  20s covers the
  # leave emit, the socket close and the clean shutdown.
  for _ in $(seq 1 20); do
    [ -d "$LOCK" ] || break
    sleep 1
  done
  rm -rf "$LOCK" 2>/dev/null
  rm -f "$PIDFILE" 2>/dev/null
  am stopservice -n "$SERVICE" >/dev/null 2>&1 || true
  echo "bot stopped"
}

status_service() {
  if [ -d "$LOCK" ]; then
    runner_pid=$(cat "$LOCK/pid" 2>/dev/null || echo "?")
    echo "runner: active (pid $runner_pid)"
  else
    echo "runner: not active"
  fi

  running=no
  for entry in /proc/[0-9]*; do
    pid=${entry#/proc/}
    [ -r "$entry/cmdline" ] || continue
    cmd=$(tr '\0' ' ' < "$entry/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *"run.py"*|*"wvbot"*) running="yes (pid $pid)" ;;
    esac
  done
  echo "bot: $running"

  echo "--- last log lines ---"
  tail -15 "$LOG" 2>/dev/null
}

case "${1:-status}" in
  start) start_service ;;
  stop) stop_service ;;
  restart) stop_service; sleep 2; start_service ;;
  status) status_service ;;
  *) echo "usage: sh ~/bot_ctl.sh start|stop|restart|status" ;;
esac
