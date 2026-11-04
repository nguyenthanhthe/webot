#!/data/data/com.termux/files/usr/bin/sh
# Run a command inside Termux on the phone.
#
#     sh termux_run.sh "ls -la ~/wv-bot"
#
# The app session of Termux has network access, unlike run-as.
export PREFIX=/data/data/com.termux/files/usr
export PATH="$PREFIX/bin:$PREFIX/bin:/system/bin"
export HOME=/data/data/com.termux/files/home
export TMPDIR="$PREFIX/tmp"

cd "$HOME" || exit 1
eval "$1"
