"""Show specific line ranges from a source file (default: wvbot.http_client)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE if (_HERE / "wvbot").is_dir() else _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _parse_range(spec: str) -> list[int]:
    if ":" in spec:
        start_s, end_s = spec.split(":", 1)
        return list(range(int(start_s), int(end_s)))
    if "-" in spec:
        start_s, end_s = spec.split("-", 1)
        return list(range(int(start_s), int(end_s) + 1))
    return [int(spec)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print line ranges of a file")
    parser.add_argument("--file", default=None, help="Target file path (defaults to wvbot/http_client.py)")
    parser.add_argument(
        "--ranges",
        nargs="+",
        default=["95:107", "163:173"],
        help="Line ranges as start:end or start-end (default: 95:107 163:173)",
    )
    args = parser.parse_args(argv)

    if args.file:
        target = Path(args.file)
    else:
        from wvbot import http_client  # noqa: E402

        target = Path(http_client.__file__)

    lines = target.read_text(encoding="utf-8").splitlines()
    numbers: list[int] = []
    for spec in args.ranges:
        numbers.extend(_parse_range(spec))
    for number in numbers:
        if 1 <= number <= len(lines):
            print(f"{number}: {lines[number - 1].rstrip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
