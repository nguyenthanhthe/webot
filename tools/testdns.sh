#!/data/data/com.termux/files/usr/bin/sh
# Probe which DNS resolver the Termux environment can actually reach.
export PREFIX=/data/data/com.termux/files/usr
export PATH="$PREFIX/bin:$PATH"

try() {
  printf 'nameserver %s\noptions timeout:2 attempts:1\n' "$1" > "$PREFIX/etc/resolv.conf"
  if [ "$2" = "vc" ]; then
    printf 'options usevc\n' >> "$PREFIX/etc/resolv.conf"
  fi
  printf '%-18s %-4s -> ' "$1" "${2:-udp}"
  python3.14 -c "import socket;print(socket.gethostbyname('game.api-wolvesville.com'))" 2>&1 | tail -1
}

try 192.168.1.1 udp
try 192.168.1.1 vc
try 8.8.8.8 vc
try 1.1.1.1 vc
try 192.168.1.57 udp
