"""Package webot / wvbot into a zip archive for Termux deployment.

    python tools/package.py          # build wvbot.zip
    python tools/package.py --check  # verify manifest completeness only

The archive contains the entire ``wvbot`` package, ``run.py``, ``setcreds.py``,
``config.json``, phone diagnostic helpers, and ``payload.js``.  ``deploy.py``
and ``deploy_phone.sh`` copy and unpack it into ``~/wv-bot`` on the device.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "wvbot.zip"

ROOT_FILES = (
    "run.py",
    "setcreds.py",
    "config.json",
)

TOOL_FILES = (
    "probe_phone.py",
    "probe_socket.py",
    "check_hosts.py",
    "show_lines.py",
    "probe_own_id.py",
    "probe_rewards.py",
    "roster.py",
    "drill_watchdog.py",
    "temp_email.py",
)


def build_members(root: Path = ROOT) -> list[tuple[Path, str]]:
    """Return ordered (source_path, archive_name) pairs for wvbot.zip."""
    wvbot_dir = root / "wvbot"
    members: list[tuple[Path, str]] = [
        (path, f"wvbot/{path.name}")
        for path in sorted(wvbot_dir.glob("*.py"))
    ]
    for name in ROOT_FILES:
        members.append((root / name, name))
    for name in TOOL_FILES:
        members.append((root / "tools" / name, name))
    members.append((root / "web" / "payload.js", "payload.js"))
    return members


MEMBERS = build_members()


def verify_manifest(
    members: list[tuple[Path, str]] | None = None,
    root: Path = ROOT,
) -> list[str]:
    """Validate that all wvbot modules exist and there are no duplicates."""
    entries = build_members(root) if members is None else members
    errors: list[str] = []
    seen_arc: set[str] = set()
    seen_src: set[Path] = set()

    for path, arcname in entries:
        resolved = path.resolve()
        if arcname in seen_arc:
            errors.append(f"duplicate archive entry: {arcname}")
        seen_arc.add(arcname)
        if resolved in seen_src:
            errors.append(f"duplicate source path: {path}")
        seen_src.add(resolved)
        if not path.is_file():
            errors.append(f"missing member: {path}")

    expected_pkg = {f"wvbot/{p.name}" for p in sorted((root / "wvbot").glob("*.py"))}
    missing_pkg = sorted(expected_pkg - seen_arc)
    for item in missing_pkg:
        errors.append(f"missing wvbot module from manifest: {item}")

    return errors


def build_zip(output: Path = OUT, root: Path = ROOT) -> Path:
    """Create the deployment zip after verifying its manifest."""
    members = build_members(root)
    errors = verify_manifest(members, root=root)
    if errors:
        raise RuntimeError("; ".join(errors))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in members:
            zf.write(path, arcname)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package wvbot for Termux deployment")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify manifest completeness without writing wvbot.zip",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=OUT,
        help="Output archive path (default: wvbot.zip at repo root)",
    )
    args = parser.parse_args(argv)

    members = build_members(ROOT)
    errors = verify_manifest(members, root=ROOT)
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    if args.check:
        print(f"manifest OK ({len(members)} members)")
        return 0

    archive = build_zip(output=args.output, root=ROOT)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    print(f"{archive}  {archive.stat().st_size} bytes  sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
