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

chmod 700 "$HOME_DIR/bot_start.sh" "$HOME_DIR/deploy.sh" 2>/dev/null || true
chmod 600 "$HOME_DIR/refresh_token.txt" 2>/dev/null || true

# The service runner, the control script and the boot hook.
# The runner is what the Termux background service executes, so the bot
# does not need a terminal session.
mkdir -p "$HOME_DIR/service" "$HOME_DIR/.termux/boot"
if [ -f "$STAGE/service_runner.sh" ]; then
  cp "$STAGE/service_runner.sh" "$HOME_DIR/service/runner.sh"
  chmod 700 "$HOME_DIR/service/runner.sh"
fi
if [ -f "$STAGE/bot_ctl.sh" ]; then
  cp "$STAGE/bot_ctl.sh" "$HOME_DIR/bot_ctl.sh"
  chmod 700 "$HOME_DIR/bot_ctl.sh"
fi
if [ -f "$STAGE/boot_wvbot" ]; then
  cp "$STAGE/boot_wvbot" "$HOME_DIR/.termux/boot/wvbot"
  chmod 700 "$HOME_DIR/.termux/boot/wvbot"
  echo "boot hook installed"
fi
if [ -f "$STAGE/job_watchdog.sh" ]; then
  cp "$STAGE/job_watchdog.sh" "$HOME_DIR/service/watchdog.sh"
  chmod 700 "$HOME_DIR/service/watchdog.sh"
  echo "watchdog installed"
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

# 3. the durable credential.  A token the bot rotated is only in the
# state file - the home copy would be the stale one, so the state file
# wins once it exists.
mkdir -p "$STATE"
chmod 700 "$STATE"
if [ -f "$HOME_DIR/refresh_token.txt" ]; then
  if [ ! -f "$STATE/refresh_token.txt" ]; then
    cp "$HOME_DIR/refresh_token.txt" "$STATE/refresh_token.txt"
    chmod 600 "$STATE/refresh_token.txt"
    echo "refresh token stored"
  else
    echo "refresh token already in the state dir - keeping it"
  fi
  rm -f "$HOME_DIR/refresh_token.txt"
fi

echo "install done"
ls "$BOT_DIR"
