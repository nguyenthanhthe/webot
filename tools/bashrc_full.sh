# ~/.bashrc - Termux profile + wv-bot autostart
#
# The real Termux home is /data/data/com.termux/files/home - never override
# $HOME here, or the app, the boot receiver and the bot end up in different
# directories.

export PREFIX=/data/data/com.termux/files/usr
export PATH="$PREFIX/bin:$PREFIX/sbin:$PATH"
export TMPDIR="${TMPDIR:-$PREFIX/tmp}"
export LD_PRELOAD="$PREFIX/lib/libtermux-exec.so"

alias ls='ls --color=auto -h'
alias ll='ls -lAh'
alias la='ls -A'
alias rm='rm -i'
alias cp='cp -i'
alias mv='mv -i'
alias mkdir='mkdir -pv'
alias ..='cd ..'
alias ...='cd ../..'

PS1='\[\e[1;32m\]\u@\h\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]\$ '

# --- wv-bot: unpack staged bundle if present, then ensure service runs ------
HOME_DIR=/data/data/com.termux/files/home
BOT_ROOT="$HOME_DIR/wv-bot"
STAGE_ZIP=""
if [ -f "$HOME_DIR/wvbot.zip" ]; then
  STAGE_ZIP="$HOME_DIR/wvbot.zip"
elif [ -f "$PREFIX/tmp/wvbot.zip" ]; then
  STAGE_ZIP="$PREFIX/tmp/wvbot.zip"
fi

if [ -n "$STAGE_ZIP" ]; then
  mkdir -p "$BOT_ROOT" "$HOME_DIR/.wv-bot"
  unzip -o "$STAGE_ZIP" -d "$BOT_ROOT" >/dev/null 2>&1
  if [ ! -f "$HOME_DIR/.wv-bot/config.json" ] && [ -f "$BOT_ROOT/config.json" ]; then
    cp "$BOT_ROOT/config.json" "$HOME_DIR/.wv-bot/config.json" >/dev/null 2>&1
  fi
fi

if [ -f "$BOT_ROOT/run.py" ] && [ ! -d "$HOME_DIR/service/bot.lock" ]; then
  if [ -f "$HOME_DIR/bot_ctl.sh" ]; then
    sh "$HOME_DIR/bot_ctl.sh" start >/dev/null 2>&1
  else
    PIDFILE="$HOME_DIR/wv-bot.pid"
    RUNNING=0
    if [ -f "$PIDFILE" ]; then
      kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null && RUNNING=1
    fi
    if [ "$RUNNING" -eq 0 ]; then
      "$PREFIX/bin/termux-wake-lock" >/dev/null 2>&1 || true
      cd "$BOT_ROOT" || exit 0
      nohup "$PREFIX/bin/python3" run.py >> "$HOME_DIR/wv-bot.log" 2>&1 &
      echo $! > "$PIDFILE"
    fi
  fi
fi
