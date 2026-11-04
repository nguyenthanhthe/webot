#!/data/data/com.termux/files/usr/bin/sh
export PREFIX=/data/data/com.termux/files/usr
export PATH="$PREFIX/bin:$PATH"
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
OUT=$PREFIX/tmp/probe_out.txt
U="https://game.api-wolvesville.com/api/public/game/custom?language=en"
mkdir -p "$PREFIX/tmp"

{
echo "=== curl protocols ==="
curl --version 2>&1 | sed -n '3p'
t() {
  printf "%-46s -> " "$1"
  shift
  curl -sS -o /dev/null -w "%{http_code} (http=%{http_version} tls=%{ssl_verify_result})\n" \
    --max-time 20 -H "User-Agent: $UA" "$@" "$U" 2>&1 | tail -1
}
t "default"                 
t "--http1.1"               --http1.1
t "--http2"                 --http2
t "--tlsv1.2 --tls-max 1.2" --tlsv1.2 --tls-max 1.2
t "--http1.1 + tls1.2"      --http1.1 --tlsv1.2 --tls-max 1.2
t "--http1.1 + ciphers"     --http1.1 --ciphers "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305" --tlsv1.3
echo "=== github reachable (for curl-impersonate) ==="
curl -sS -o /dev/null -w "%{http_code}\n" --max-time 20 "https://github.com" 2>&1 | tail -1
echo "=== done ==="
} > "$OUT" 2>&1
