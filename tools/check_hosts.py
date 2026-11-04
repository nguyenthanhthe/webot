"""Print the HTTP allowlist the phone sees (diagnostic)."""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from wvbot import http_client  # noqa: E402
from wvbot.config import load_config  # noqa: E402

print("module file:", http_client.__file__)

cfg = load_config()
print("config refresh token length:", len(cfg.refresh_token))

client = http_client.HttpClient()
print("client.allowed_hosts:", sorted(client.allowed_hosts))

try:
    data = client.request(
        "POST",
        "https://auth.api-wolvesville.com/players/createIdToken",
        body={"refreshToken": cfg.refresh_token},
    )
    print("request ok, idToken length:", len((data or {}).get("idToken", "")))
except Exception as exc:  # noqa: BLE001
    print("request failed:", exc)
    print("client.allowed_hosts at failure:", sorted(client.allowed_hosts))
