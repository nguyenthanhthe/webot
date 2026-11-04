#!/data/data/com.termux/files/usr/bin/sh
# Watchdog for the bot service.
#
# It is registered with the Android JobScheduler (termux-job-scheduler)
# and runs every 15 minutes.  If the bot is not running it is started
# again, so the bot survives the system killing the Termux app.
set -u

HOME_DIR=/data/data/com.termux/files/home
LOCK="$HOME_DIR/service/bot.lock"

export PATH="/data/data/com.termux/files/usr/bin:$PATH"
export HOME="$HOME_DIR"

if [ -d "$LOCK" ]; then
  runner_pid=$(cat "$LOCK/pid" 2>/dev/null || echo "?")
  echo "the bot runner is active (pid $runner_pid)"
  exit 0
fi

echo "the bot runner is not active - starting it again"
sh "$HOME_DIR/bot_ctl.sh" start
