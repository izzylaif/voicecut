#!/usr/bin/env python3
"""Stage what the VoiceCut app bundles besides itself.

    python3 desktop/scripts/stage_runtime.py [--platform linux-x86_64 ...]

writes desktop/build/runtime/: runtime.json, the platforms' lock files (the
universal macOS app carries both Macs'), and VoiceCut's own source
(src/voicecut with the built editor frontend, pyproject.toml, README.md,
LICENSE) with its hash in source.sha256.  The app installs these on first
launch and updates the source when the hash changes.  It also copies the app
mark to desktop/ui/mark.svg.  Build the frontend first (npm run build
--prefix frontend).  Runs on macOS, Linux and Windows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sys
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent.parent
ROOT = DESKTOP.parent
SKIPPED = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def host_platform() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine == "arm64":
        return "macos-aarch64"
    if sys.platform == "darwin" and machine == "x86_64":
        return "macos-x86_64"
    if sys.platform == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if sys.platform == "win32" and machine in {"x86_64", "amd64"}:
        return "windows-x86_64"
    raise SystemExit(f"VoiceCut has no runtime for {sys.platform} on {machine}")


def source_hash(source: Path) -> str:
    """The hash `find . -type f | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256` gives."""

    paths = sorted(
        (
            "./" + path.relative_to(source).as_posix()
            for path in source.rglob("*")
            if path.is_file()
        ),
        key=lambda name: name.encode(),
    )
    listing = "".join(
        f"{hashlib.sha256((source / name).read_bytes()).hexdigest()}  {name}\n"
        for name in paths
    )
    return hashlib.sha256(listing.encode()).hexdigest()


def stage(targets: list[str], output: Path) -> None:
    spec_file = DESKTOP / "runtime" / "runtime.json"
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    if not (ROOT / "src/voicecut/editor/static/index.html").is_file():
        raise SystemExit(
            "build the editor frontend first: npm run build --prefix frontend"
        )
    files = []
    for target in targets:
        if target not in spec["platforms"]:
            raise SystemExit(f"runtime.json has no platform {target}")
        entry = spec["platforms"][target]
        files += [*entry["requirements"].values(), entry["mfa_lock"]]
        if "main_base" in entry:
            files.append(entry["main_base"])

    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    shutil.copy2(spec_file, output / "runtime.json")
    for name in files:
        shutil.copy2(DESKTOP / "runtime" / name, output / name)
    source = output / "source"
    shutil.copytree(ROOT / "src/voicecut", source / "src/voicecut", ignore=SKIPPED)
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, source / name)
    (output / "source.sha256").write_text(source_hash(source) + "\n", encoding="utf-8")
    shutil.copy2(DESKTOP / "icons/voicecut.svg", DESKTOP / "ui/mark.svg")
    print(f"staged the {' and '.join(targets)} runtime in {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--platform", action="append", help="repeatable; default: this computer's"
    )
    parser.add_argument("--output", type=Path, default=DESKTOP / "build" / "runtime")
    args = parser.parse_args()
    stage(args.platform or [host_platform()], args.output)


if __name__ == "__main__":
    main()
