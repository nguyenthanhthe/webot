#!/data/data/com.termux/files/usr/bin/sh
# Legacy Termux boot helper aligned with tools/boot_wvbot and bot_ctl.sh.
set -u

PREFIX=/data/data/com.termux/files/usr
HOME=/data/data/com.termux/files/home
LOCK="$HOME/service/bot.lock"
PIDFILE="$HOME/wv-bot.pid"
export PATH="$PREFIX/bin:$PATH"
export HOME

if [ -d "$LOCK" ]; then
  exit 0
fi

if [ -f "$HOME/bot_ctl.sh" ]; then
  sh "$HOME/bot_ctl.sh" start
  exit 0
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  exit 0
fi

"$PREFIX/bin/termux-wake-lock" >/dev/null 2>&1 || true
mkdir -p "$HOME/wv-bot" "$HOME/.wv-bot"
cd "$HOME/wv-bot" || exit 1
nohup "$PREFIX/bin/python3" run.py >> "$HOME/wv-bot.log" 2>&1 &
echo $! > "$PIDFILE"
