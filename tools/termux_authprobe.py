#!/usr/bin/env python3
"""Diagnostic probe for Wolvesville auth and public custom game endpoints."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import urllib.error
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE if (_HERE / "wvbot").is_dir() else _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wvbot.http_client import HttpClient  # noqa: E402

DEFAULT_OUT = Path(os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp")) / "probe_out.txt"


def run_probe(out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        def log(*args: object) -> None:
            fh.write(" ".join(str(x) for x in args) + "\n")
            fh.flush()

        http = HttpClient(timeout=20, max_retries=1)
        try:
            log("--- signIn with dummy credentials ---")
            try:
                data = http.request(
                    "POST",
                    "https://auth.api-wolvesville.com/players/signInWithEmailAndPassword",
                    {"email": "nobody@example.invalid", "password": "wrong-password"},
                    {},
                )
                log("sign-in ->", json.dumps(data)[:400])
            except urllib.error.HTTPError as exc:
                log("sign-in -> HTTP", exc.code, exc.read()[:400])
            except Exception as exc:
                log("sign-in -> ERR", type(exc).__name__, exc)

            log("--- createIdToken with dummy refresh token ---")
            try:
                data = http.request(
                    "POST",
                    "https://auth.api-wolvesville.com/players/createIdToken",
                    {"refreshToken": "dummy-refresh-token"},
                    {},
                )
                log("createIdToken ->", json.dumps(data)[:400])
            except urllib.error.HTTPError as exc:
                log("createIdToken -> HTTP", exc.code, exc.read()[:400])
            except Exception as exc:
                log("createIdToken -> ERR", type(exc).__name__, exc)

            log("--- custom game list with dummy bearer ---")
            try:
                data = http.request(
                    "GET",
                    "https://game.api-wolvesville.com/api/public/game/custom?language=en",
                    None,
                    {"Authorization": "Bearer dummy-token"},
                )
                log("custom list ->", json.dumps(data)[:400])
            except urllib.error.HTTPError as exc:
                log("custom list -> HTTP", exc.code, exc.read()[:400])
            except Exception as exc:
                log("custom list -> ERR", type(exc).__name__, exc)
        except Exception:
            log("FATAL", traceback.format_exc())
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe Wolvesville auth & custom-game HTTP endpoints")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output log file path")
    args = parser.parse_args(argv)
    return run_probe(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
