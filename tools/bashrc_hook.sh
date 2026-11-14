#!/data/data/com.termux/files/usr/bin/sh
# Optional ~/.bashrc hook to ensure the wv-bot service is running on login.
HOME_DIR=/data/data/com.termux/files/home
if [ ! -d "$HOME_DIR/service/bot.lock" ] && [ -f "$HOME_DIR/bot_ctl.sh" ]; then
  sh "$HOME_DIR/bot_ctl.sh" start >/dev/null 2>&1
fi
