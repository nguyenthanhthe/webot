"""Drive the phone's UI through ``uiautomator`` and ``adb``.

This is the whole trick behind the app-based bot: no login, no Cloudflare,
no protocol reverse engineering - just read the screen and tap what we need.
The same module runs on the laptop (against a USB/wireless device) and on
the phone itself (against its own ``adbd`` on 127.0.0.1:5555).
"""

from __future__ import annotations

import logging
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass

log = logging.getLogger("wvbot.ui")

DUMP_TIMEOUT = 25


@dataclass
class Node:
    text: str
    desc: str
    cls: str
    clickable: bool
    bounds: tuple[int, int, int, int]
    resource_id: str = ""

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    @property
    def label(self) -> str:
        return self.text or self.desc

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"Node({self.label!r}, {self.cls}, clickable={self.clickable})"


class UiError(RuntimeError):
    pass


def _parse_xml(raw_xml: str) -> ET.Element:
    """Parse uiautomator output without expanding entities.

    The dump comes from the device, so it is treated as untrusted: entity
    declarations are rejected and the input size is capped.
    """
    import xml.parsers.expat

    if len(raw_xml) > 4 * 1024 * 1024:
        raise UiError("XML output is too large")
    lowered = raw_xml.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise UiError("XML with entity declarations is not accepted")

    def _reject_entity(*_args):
        raise UiError("entity declarations are not accepted")

    builder = ET.TreeBuilder()
    parser = xml.parsers.expat.ParserCreate()
    parser.EntityDeclHandler = _reject_entity
    parser.StartElementHandler = lambda name, attrs: builder.start(name, attrs)
    parser.EndElementHandler = lambda name: builder.end(name)
    parser.CharacterDataHandler = builder.data
    parser.Parse(raw_xml, True)
    return builder.close()


class Ui:
    """A small uiautomator driver."""

    def __init__(
        self,
        serial: str | None = None,
        adb: str = "adb",
        connect: str | None = None,
    ):
        self.serial = serial
        self.adb = adb
        self.connect = connect
        self._connected = False

    # ------------------------------------------------------------------
    def _run(self, args: list[str], timeout: int = DUMP_TIMEOUT) -> str:
        command = [self.adb]
        if self.serial:
            command += ["-s", self.serial]
        command += args
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise UiError(f"{self.adb} not found: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise UiError(f"adb timeout: {' '.join(command)}") from exc
        if result.returncode != 0:
            raise UiError(f"adb failed: {result.stderr.strip()[:200]}")
        return result.stdout

    def ensure_connected(self) -> None:
        if self.connect and not self._connected:
            self._run(["connect", self.connect])
            self._connected = True

    # ------------------------------------------------------------------
    def dump(self) -> list[Node]:
        """Return every node of the current screen."""
        self.ensure_connected()
        raw = self._run(["exec-out", "uiautomator", "dump", "/dev/tty"])
        start = raw.find("<?xml")
        end = raw.rfind("</hierarchy>")
        if start == -1 or end == -1:
            raise UiError("could not parse the uiautomator output")
        root = _parse_xml(raw[start : end + len("</hierarchy>")])

        nodes: list[Node] = []
        for element in root.iter("node"):
            nodes.append(self._to_node(element))
        return nodes

    @staticmethod
    def _to_node(element: ET.Element) -> Node:
        bounds = (0, 0, 0, 0)
        raw_bounds = element.get("bounds", "")
        numbers = re.findall(r"-?\d+", raw_bounds)
        if len(numbers) >= 4:
            bounds = tuple(int(number) for number in numbers[:4])  # type: ignore[assignment]
        else:
            log.debug("unparsable bounds: %r", raw_bounds)
        return Node(
            text=element.get("text", ""),
            desc=element.get("content-desc", ""),
            cls=element.get("class", ""),
            clickable=element.get("clickable", "false") == "true",
            bounds=bounds,
            resource_id=element.get("resource-id", ""),
        )

    # ------------------------------------------------------------------
    @staticmethod
    def find(
        nodes: list[Node],
        text: str | None = None,
        contains: str | None = None,
        desc_contains: str | None = None,
        clickable: bool | None = None,
        case_insensitive: bool = True,
    ) -> list[Node]:
        """Filter nodes by text / content-desc / clickable."""

        def norm(value: str) -> str:
            return value.lower() if case_insensitive else value

        result: list[Node] = []
        for node in nodes:
            if text is not None and norm(node.text) != norm(text):
                continue
            if contains is not None and norm(contains) not in norm(node.text):
                continue
            if desc_contains is not None and norm(desc_contains) not in norm(node.desc):
                continue
            if clickable is not None and node.clickable != clickable:
                continue
            result.append(node)
        return result

    def tap_node(self, node: Node) -> None:
        x, y = node.center
        self.tap(x, y)

    def tap(self, x: int, y: int) -> None:
        self.ensure_connected()
        self._run(["shell", "input", "tap", str(x), str(y)], timeout=15)

    def back(self) -> None:
        self.ensure_connected()
        self._run(["shell", "input", "keyevent", "4"], timeout=15)

    def launch_app(self, package: str) -> None:
        self.ensure_connected()
        self._run(
            ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
            timeout=25,
        )

    def screen_is_on(self) -> bool:
        self.ensure_connected()
        output = self._run(["shell", "dumpsys", "power"], timeout=20)
        for line in output.splitlines():
            if "mWakefulness=" in line:
                return "mWakefulness=Awake" in line
        return True


def describe(nodes: list[Node], limit: int = 60) -> str:
    """Human readable dump of the informative nodes."""
    lines = []
    for node in nodes:
        if not node.label:
            continue
        lines.append(
            f"{node.label[:70]!r} cls={node.cls.split('.')[-1]} "
            f"click={node.clickable} center={node.center}"
        )
        if len(lines) >= limit:
            break
    return "\n".join(lines)
