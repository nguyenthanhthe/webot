# ~/.bashrc - Termux profile + wv-bot autostart
#
# The real Termux home is /data/data/com.termux/files/home - never override
# $HOME here, or the app, the boot receiver and the bot end up in different
# directories.

export PREFIX=/data/data/com.termux/files/usr
export PATH="$PREFIX/bin:$PREFIX/sbin:$PATH"
export TMPDIR="$PREFIX/tmp"
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

# --- wv-bot: unpack the shipped bundle, then keep the bot running ---------
BOT_ROOT="$HOME/wv-bot"
if [ -f "$PREFIX/tmp/wvbot.zip" ]; then
  mkdir -p "$BOT_ROOT" "$HOME/.wv-bot"
  unzip -o "$PREFIX/tmp/wvbot.zip" -d "$BOT_ROOT" >/dev/null 2>&1
  if [ ! -f "$HOME/.wv-bot/config.json" ]; then
    cp "$BOT_ROOT/config.json" "$HOME/.wv-bot/config.json" >/dev/null 2>&1
  fi
fi

if [ -f "$BOT_ROOT/run.py" ]; then
  PIDFILE="$PREFIX/tmp/bot.pid"
  RUNNING=0
  if [ -f "$PIDFILE" ]; then
    kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null && RUNNING=1
  fi
  if [ "$RUNNING" -eq 0 ]; then
    "$PREFIX/bin/termux-wake-lock" >/dev/null 2>&1
    cd "$BOT_ROOT" || exit 0
    nohup "$PREFIX/bin/python3" run.py >> "$PREFIX/tmp/bot.log" 2>&1 &
    echo $! > "$PIDFILE"
  fi
fi
