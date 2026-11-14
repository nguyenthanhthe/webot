#!/data/data/com.termux/files/usr/bin/sh
# Fix outdated Termux libc++ when native binaries require a newer NDK runtime.
PREFIX=/data/data/com.termux/files/usr
export PATH="$PREFIX/bin:$PATH"
export HOME=/data/data/com.termux/files/home
OUT="$PREFIX/tmp/libcxx.log"
mkdir -p "$PREFIX/tmp"

{
  echo "=== installed libc++ ==="
  dpkg -l libc++ 2>/dev/null | tail -2
  echo "=== upgrade libc++ ==="
  apt install --only-upgrade -y libc++ 2>&1 | tail -10
  echo "=== reconfigure broken packages ==="
  dpkg --configure -a 2>&1 | tail -6
  echo "=== python3 ==="
  python3 -V 2>&1 | head -2
  echo "=== adb ==="
  adb version 2>&1 | head -2
  echo "=== firefox ==="
  firefox --version 2>&1 | head -2
  echo "=== done ==="
} > "$OUT" 2>&1
