#!/data/data/com.termux/files/usr/bin/sh
# Started by Termux:Boot after every device boot.
# Brings the Wolvesville farming bot back up if it is not running.
PREFIX=/data/data/com.termux/files/usr
HOME=$PREFIX/files/home
PIDFILE=$PREFIX/tmp/bot.pid

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  exit 0
fi

"$PREFIX/bin/termux-wake-lock" >/dev/null 2>&1
mkdir -p "$HOME/wv-bot" "$HOME/.wv-bot"
cd "$HOME/wv-bot" || exit 1
nohup "$PREFIX/bin/python3" run.py >> "$PREFIX/tmp/bot.log" 2>&1 &
echo $! > "$PIDFILE"
