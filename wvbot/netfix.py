"""Keep DNS working inside Termux.

On Termux Android can hand out a nameserver that stops answering when
Tailscale is up, which breaks API calls. This module probes resolvers
and writes the first working one into Termux's ``resolv.conf`` while
avoiding touching non-Termux hosts or mutating global socket timeouts.
"""

from __future__ import annotations

import logging
import socket
import subprocess
from pathlib import Path

log = logging.getLogger("wvbot.netfix")

RESOLV = Path("/data/data/com.termux/files/usr/etc/resolv.conf")
PROBE_HOST = "game.api-wolvesville.com"
PUBLIC_FALLBACKS = ("8.8.8.8", "1.1.1.1", "9.9.9.9")


def _resolve_works(host: str = PROBE_HOST) -> bool:
    try:
        socket.gethostbyname(host)
        return True
    except OSError:
        return False


def _default_gateway() -> str | None:
    """Return the IPv4 default gateway (the router) if we can see it."""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if "default" in parts and "via" in parts:
            index = parts.index("via")
            if index + 1 < len(parts):
                return parts[index + 1]
    return None


def candidate_nameservers() -> list[str]:
    """Router first, public resolvers afterwards, no duplicates."""
    candidates: list[str] = []
    gateway = _default_gateway()
    if gateway:
        candidates.append(gateway)
    candidates.extend(PUBLIC_FALLBACKS)
    unique: list[str] = []
    for item in candidates:
        if item not in unique:
            unique.append(item)
    return unique


def _write_resolv(nameserver: str) -> None:
    if not RESOLV.parent.is_dir():
        return
    RESOLV.write_text(
        f"nameserver {nameserver}\noptions timeout:2 attempts:1\n",
        encoding="utf-8",
    )


def _current_nameserver() -> str:
    try:
        for line in RESOLV.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                return parts[1]
    except OSError:
        pass
    return ""


def ensure_dns(force: bool = False) -> str:
    """Make sure name resolution works. Returns the nameserver in use."""
    if not force and _resolve_works():
        return _current_nameserver() or "system"

    if not RESOLV.parent.is_dir():
        return "system" if _resolve_works() else "failed"

    original = None
    try:
        original = RESOLV.read_text(encoding="utf-8")
    except OSError:
        original = None

    for nameserver in candidate_nameservers():
        try:
            _write_resolv(nameserver)
        except OSError as exc:
            log.warning("cannot write resolv.conf: %s", exc)
            return "failed"
        if _resolve_works():
            log.info("DNS fixed: using nameserver %s", nameserver)
            return nameserver

    if original is not None:
        try:
            RESOLV.write_text(original, encoding="utf-8")
        except OSError:
            pass
    log.warning("no working nameserver found")
    return "failed"
