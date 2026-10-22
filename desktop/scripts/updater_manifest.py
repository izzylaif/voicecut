"""Write latest.json, the manifest VoiceCut's updater reads (src-tauri/src/updates.rs).

    python3 desktop/scripts/updater_manifest.py DIRECTORY --version 0.2.1 \\
        --base-url https://github.com/vectozavr/voicecut/releases/download/v0.2.1

DIRECTORY holds a release's files with the updater signatures (`<file>.sig`)
that `tauri build --config src-tauri/tauri.updater.conf.json` writes when
TAURI_SIGNING_PRIVATE_KEY is set.  Every signed package becomes its system's
entry in DIRECTORY/latest.json, downloaded from BASE_URL/<file name>.  With
no signature at all, no manifest is written and the script says so.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import sys
import urllib.parse
from pathlib import Path

# File name ending -> the updater's targets for it: `{os}-{arch}-{bundle}`,
# then `{os}-{arch}`, which older updaters and unknown bundle types look up.
PACKAGES = [
    (".app.tar.gz", ["darwin-aarch64-app", "darwin-aarch64"]),
    (".AppImage", ["linux-x86_64-appimage", "linux-x86_64"]),
    (".deb", ["linux-x86_64-deb"]),
    ("-setup.exe", ["windows-x86_64-nsis", "windows-x86_64"]),
]


def signed_version(signature: str) -> str | None:
    """The app version `tauri build` put in a signature's trusted comment."""
    text = base64.b64decode(signature).decode("utf-8", "replace")
    for line in text.splitlines():
        if line.startswith("trusted comment:"):
            for field in line.removeprefix("trusted comment:").strip().split("\t"):
                if field.startswith("version:"):
                    return field.removeprefix("version:")
    return None


def manifest(directory: Path, version: str, base_url: str, notes: str) -> dict | None:
    platforms: dict[str, dict[str, str]] = {}
    for suffix, targets in PACKAGES:
        for package in sorted(directory.iterdir()):
            if not package.name.endswith(suffix):
                continue
            signature_file = package.with_name(package.name + ".sig")
            if not signature_file.is_file():
                continue
            signature = signature_file.read_text().strip()
            signed = signed_version(signature)
            if signed is not None and signed != version:
                raise SystemExit(f"{signature_file.name} is for version {signed}, not {version}")
            for target in targets:
                if target in platforms:
                    raise SystemExit(f"two packages for {target}")
                platforms[target] = {
                    "signature": signature,
                    "url": f"{base_url.rstrip('/')}/{urllib.parse.quote(package.name)}",
                }
    if not platforms:
        return None
    return {
        "version": version,
        "notes": notes,
        "pub_date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": platforms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--notes", default="")
    arguments = parser.parse_args()
    result = manifest(arguments.directory, arguments.version, arguments.base_url, arguments.notes)
    if result is None:
        print(f"no updater signatures in {arguments.directory}: no latest.json", file=sys.stderr)
        return 0
    target = arguments.directory / "latest.json"
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(f"{target}: {', '.join(result['platforms'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
