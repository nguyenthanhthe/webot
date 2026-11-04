#!/data/data/com.termux/files/usr/bin/sh
# Install the bot on the phone.
#
# The files are staged in /data/local/tmp by adb and copied into the
# Termux home here, because that directory is not writable from adb.
set -e

HOME_DIR=/data/data/com.termux/files/home
BOT_DIR="$HOME_DIR/wv-bot"
STATE="$HOME_DIR/.wv-bot"
STAGE=/data/local/tmp

cd "$HOME_DIR"

for name in wvbot.zip deploy.sh termux_run.sh refresh_token.txt bot_start.sh; do
  if [ -f "$STAGE/$name" ]; then
    cp "$STAGE/$name" "$HOME_DIR/$name"
  fi
done

# The helper scripts live in the home directory so they can be run from
# anywhere: sh ~/bot_start.sh
if [ -f "$STAGE/bot_start.sh" ]; then
  cp "$STAGE/bot_start.sh" "$HOME_DIR/bot_start.sh"
  chmod 700 "$HOME_DIR/bot_start.sh"
fi
if [ -f "$STAGE/deploy.sh" ]; then
  cp "$STAGE/deploy.sh" "$HOME_DIR/deploy.sh"
  chmod 700 "$HOME_DIR/deploy.sh"
fi

# 1. the package
if [ -f "$HOME_DIR/wvbot.zip" ]; then
  echo "unpacking wvbot.zip"
  rm -rf "$BOT_DIR"
  mkdir -p "$BOT_DIR"
  cd "$BOT_DIR"
  unzip "$HOME_DIR/wvbot.zip"
  cd "$HOME_DIR"
fi

# 2. the payload the bot injects (it is inside the package already)
if [ -f "$BOT_DIR/payload.js" ]; then
  echo "payload installed"
fi

# 3. the durable credential
mkdir -p "$STATE"
chmod 700 "$STATE"
if [ -f "$HOME_DIR/refresh_token.txt" ]; then
  cp "$HOME_DIR/refresh_token.txt" "$STATE/refresh_token.txt"
  chmod 600 "$STATE/refresh_token.txt"
  echo "refresh token stored"
  rm -f "$HOME_DIR/refresh_token.txt"
fi

echo "install done"
ls "$BOT_DIR"
