"""Watches the game for changes that could break the bot.

The reference bot sells "auto updates" - it keeps working after the
game is patched.  The protocol here is re-implemented in this
repository, so a patch means the official client changed and a few
lines have to follow.

Two things are checked:

* the build number of the game, read from the core API.  When it
  changes, the game was patched.
* the name of the client bundle on the website.  It carries a hash of
  the file, so a changed hash means the protocol may have changed
  with it.

The check runs now and then and only reports - it never rewrites
anything on its own.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .http_client import HttpClient

log = logging.getLogger("wvbot.updates")

CORE_URL = "https://core.api-wolvesville.com"
SITE_URL = "https://www.wolvesville.com"

# The bundle name looks like "template/main.a3393cef.js" or
# "static/js/main.a3393cef.js"; only the hash is interesting.
BUNDLE_RE = re.compile(r"main\.([a-z0-9]{6,12})\.js")


class UpdateWatcher:
    """Notices when the game or the client bundle changed."""

    def __init__(self, cfg: Any, http: HttpClient, auth_headers: dict[str, str] | None = None):
        self.cfg = cfg
        self.http = http
        self.auth_headers = dict(auth_headers or {})
        self.build_number: str = ""
        self.bundle_hash: str = ""
        self.changed = False

    # ------------------------------------------------------------------
    def _read_build_number(self) -> str:
        """The build number the server expects.

        The endpoint answers with the version the client should have.
        A change there means the game was patched.
        """
        data = self.http.request(
            "PUT",
            f"{CORE_URL}/appVersions/check",
            body={"versionNumber": "0", "platform": "web"},
            headers=self.auth_headers,
        )
        if not isinstance(data, dict):
            return ""
        latest = data.get("latestAppVersion")
        if isinstance(latest, dict):
            number = latest.get("versionNumber")
            if number is not None:
                return str(number)
            version = latest.get("versionString")
            if version:
                return str(version)
        return ""

    def _read_bundle_hash(self) -> str:
        """The hash in the name of the client bundle.

        The website is behind a proof-of-work shield, so this often
        fails.  That is not a problem: the build number is the
        reliable signal.
        """
        try:
            raw = self.http.request("GET", f"{SITE_URL}/", headers={})
        except Exception as exc:  # noqa: BLE001 - the shield blocks most of the time
            log.debug("could not read the bundle hash: %s", exc)
            return ""
        if not isinstance(raw, str):
            return ""
        match = BUNDLE_RE.search(raw)
        return match.group(1) if match else ""

    # ------------------------------------------------------------------
    def check(self) -> dict[str, Any]:
        """Read both signals and report what changed."""
        build = self._read_build_number()
        bundle = self._read_bundle_hash()

        result: dict[str, Any] = {
            "build_number": build,
            "bundle_hash": bundle,
            "build_changed": False,
            "bundle_changed": False,
        }
        if self.build_number and build and build != self.build_number:
            result["build_changed"] = True
            result["previous_build"] = self.build_number
        if self.bundle_hash and bundle and bundle != self.bundle_hash:
            result["bundle_changed"] = True
            result["previous_bundle"] = self.bundle_hash

        if build:
            self.build_number = build
        if bundle:
            self.bundle_hash = bundle
        result["changed"] = bool(result["build_changed"] or result["bundle_changed"])
        return result
