#!/usr/bin/env python3
"""Diagnostic TLS 1.2 / 1.3 and WebSocket handshake probe against game API."""

from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HOST = "game.api-wolvesville.com"
URL = f"https://{HOST}/api/public/game/custom?language=en"
DEFAULT_OUT = Path(os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp")) / "probe_out.txt"


def _ctx12() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def run_probe(out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        def log(*args: object) -> None:
            fh.write(" ".join(str(x) for x in args) + "\n")
            fh.flush()

        def probe_http(label: str, ctx: ssl.SSLContext) -> None:
            req = urllib.request.Request(
                URL,
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://app.wolvesville.com",
                    "Referer": "https://app.wolvesville.com/",
                    "ids": "1",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                    log(f"HTTP {label:14s} -> {resp.status}  {resp.read()[:120]!r}")
            except urllib.error.HTTPError as exc:
                log(f"HTTP {label:14s} -> {exc.code}  {exc.read()[:160]!r}")
            except Exception as exc:
                log(f"HTTP {label:14s} -> ERR {type(exc).__name__}: {exc}")

        def probe_ws(label: str, ctx: ssl.SSLContext) -> None:
            path = "/socket.io/?EIO=4&transport=websocket&ids=1&apiV=1"
            try:
                raw = socket.create_connection((HOST, 443), timeout=20)
                with ctx.wrap_socket(raw, server_hostname=HOST) as sock:
                    ver = sock.version()
                    req = (
                        f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                        f"Sec-WebSocket-Version: 13\r\nUser-Agent: {UA}\r\n"
                        "Origin: https://app.wolvesville.com\r\n\r\n"
                    )
                    sock.sendall(req.encode())
                    data = sock.recv(4096)
                    head = data.split(b"\r\n", 1)[0].decode("latin1", "replace")
                    log(f"WS   {label:14s} -> tls={ver} {head}")
            except Exception as exc:
                log(f"WS   {label:14s} -> ERR {type(exc).__name__}: {exc}")

        try:
            log("python", sys.version.split()[0], "| openssl", ssl.OPENSSL_VERSION)
            probe_http("tls-default", ssl.create_default_context())
            probe_http("tls1.2-max", _ctx12())
            probe_ws("tls-default", ssl.create_default_context())
            probe_ws("tls1.2-max", _ctx12())
        except Exception:
            log("FATAL", traceback.format_exc())
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe TLS and WebSocket handshakes in Termux")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output log file path")
    args = parser.parse_args(argv)
    return run_probe(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
