import ssl, socket, sys, urllib.request, urllib.error

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HOST = "game.api-wolvesville.com"
URL = f"https://{HOST}/api/public/game/custom?language=en"
OUT = "/data/data/com.termux/files/usr/tmp/probe_out.txt"

fh = open(OUT, "w")


def log(*a):
    fh.write(" ".join(str(x) for x in a) + "\n")
    fh.flush()


def ctx12():
    c = ssl.create_default_context()
    c.maximum_version = ssl.TLSVersion.TLSv1_2
    return c


def http(label, ctx):
    req = urllib.request.Request(URL, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://app.wolvesville.com",
        "Referer": "https://app.wolvesville.com/",
        "ids": "1",
    })
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            log(f"HTTP {label:14s} -> {r.status}  {r.read()[:120]!r}")
    except urllib.error.HTTPError as e:
        log(f"HTTP {label:14s} -> {e.code}  {e.read()[:160]!r}")
    except Exception as e:
        log(f"HTTP {label:14s} -> ERR {type(e).__name__}: {e}")


def ws(label, ctx):
    path = "/socket.io/?EIO=4&transport=websocket&ids=1&apiV=1"
    try:
        raw = socket.create_connection((HOST, 443), timeout=20)
        s = ctx.wrap_socket(raw, server_hostname=HOST)
        ver = s.version()
        req = (f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
               f"Sec-WebSocket-Version: 13\r\nUser-Agent: {UA}\r\n"
               f"Origin: https://app.wolvesville.com\r\n\r\n")
        s.sendall(req.encode())
        data = s.recv(4096)
        head = data.split(b"\r\n", 1)[0].decode("latin1", "replace")
        log(f"WS   {label:14s} -> tls={ver} {head}")
        s.close()
    except Exception as e:
        log(f"WS   {label:14s} -> ERR {type(e).__name__}: {e}")


try:
    log("python", sys.version.split()[0], "| openssl", ssl.OPENSSL_VERSION)
    http("tls-default", ssl.create_default_context())
    http("tls1.2-max", ctx12())
    ws("tls-default", ssl.create_default_context())
    ws("tls1.2-max", ctx12())
except Exception:
    import traceback
    log("FATAL", traceback.format_exc())
finally:
    fh.close()
