#!/data/data/com.termux/files/usr/bin/sh
# Start the bot as a background service and show the last log lines.
#
#     sh bot_start.sh
#
# The bot runs inside the Termux background service, so closing every
# Termux tab does not stop it.  Use ~/bot_ctl.sh for stop/restart/status.
set -e

HOME_DIR=/data/data/com.termux/files/home

export PATH="/data/data/com.termux/files/usr/bin:$PATH"
export HOME="$HOME_DIR"

sh "$HOME_DIR/bot_ctl.sh" start
sleep 5
sh "$HOME_DIR/bot_ctl.sh" status
