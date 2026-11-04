"""Keep DNS working inside Termux.

On this device Android hands Termux a nameserver that stops answering as
soon as Tailscale is up, which breaks every API call.  This module probes a
short list of resolvers and writes the first working one into the Termux
``resolv.conf`` - the local router first, because it is the most stable
option, public resolvers only as a fallback.
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
        socket.setdefaulttimeout(5)
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
    RESOLV.parent.mkdir(parents=True, exist_ok=True)
    RESOLV.write_text(
        f"nameserver {nameserver}\noptions timeout:2 attempts:1\n",
        encoding="utf-8",
    )


def ensure_dns(force: bool = False) -> str:
    """Make sure name resolution works. Returns the nameserver in use."""
    if not force and _resolve_works():
        current = ""
        try:
            for line in RESOLV.read_text(encoding="utf-8").splitlines():
                if line.startswith("nameserver"):
                    current = line.split()[1]
                    break
        except OSError:
            current = ""
        return current or "system"

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
