"""Show the http_client lines around the failure (diagnostic)."""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from wvbot import http_client  # noqa: E402

lines = http_client.__file__
with open(lines, encoding="utf-8") as fh:
    content = fh.readlines()

for number in list(range(95, 107)) + list(range(163, 173)):
    if 1 <= number <= len(content):
        print(f"{number}: {content[number - 1].rstrip()}")
