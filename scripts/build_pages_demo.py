from __future__ import annotations

import argparse
import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITE_SOURCE = PROJECT_ROOT / "docs" / "demo"
MEDIA_SOURCE = PROJECT_ROOT / "examples" / "media"
README_ASSETS = PROJECT_ROOT / "docs" / "assets" / "readme"
SCREENSHOTS = PROJECT_ROOT / "docs" / "assets" / "screenshots"
SOCIAL_CARD = PROJECT_ROOT / "docs" / "assets" / "social-card.png"
APP_ICONS = PROJECT_ROOT / "desktop" / "src-tauri" / "icons"
REQUIRED_ASSETS = (
    "pipeline.svg",
    "video-before.jpg",
    "video-after.jpg",
)
# The app icon at the sizes the page uses: published name -> rendered icon.
ICONS = {
    "icon.png": "icon.png",
    "icon-256.png": "128x128@2x.png",
    "icon-128.png": "128x128.png",
}
REPOSITORY = "vectozavr/voicecut"
RELEASE_FIELDS = ("tag_name", "name", "html_url", "published_at")
ASSET_FIELDS = ("name", "size", "browser_download_url")


def _safe_output_path(value: str) -> Path:
    output = Path(value)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    output = output.resolve()
    allowed_root = (PROJECT_ROOT / ".pages-site").resolve()
    if output != allowed_root and allowed_root not in output.parents:
        raise ValueError("demo output must be .pages-site or one of its subdirectories")
    return output


def latest_release(repository: str, token: str | None) -> dict | None:
    """The latest published release with its assets, or None when GitHub
    cannot be reached; the page then asks GitHub itself."""

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "voicecut-pages"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/latest", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
    except (OSError, urllib.error.URLError, ValueError) as error:
        print(f"warning: no release.json, the latest release is unavailable: {error}")
        return None
    return {
        **{field: release.get(field) for field in RELEASE_FIELDS},
        "assets": [
            {field: asset.get(field) for field in ASSET_FIELDS}
            for asset in release.get("assets", [])
        ],
    }


def build_demo(output: Path, release: dict | None = None) -> None:
    required_media = (
        MEDIA_SOURCE / "audio" / "example_en.wav",
        MEDIA_SOURCE / "audio" / "example_en_edited.wav",
        MEDIA_SOURCE / "audio" / "example_ru.wav",
        MEDIA_SOURCE / "audio" / "example_ru_edited.wav",
        MEDIA_SOURCE / "video" / "video.mp4",
        MEDIA_SOURCE / "video" / "video_edited.mp4",
    )
    screenshots = sorted(SCREENSHOTS.glob("*.webp"))
    required_files = (
        SITE_SOURCE / "index.html",
        SITE_SOURCE / "styles.css",
        SITE_SOURCE / "site.js",
        SOCIAL_CARD,
        *(README_ASSETS / name for name in REQUIRED_ASSETS),
        *(APP_ICONS / name for name in ICONS.values()),
        *required_media,
    )
    missing = [
        path.relative_to(PROJECT_ROOT) for path in required_files if not path.is_file()
    ]
    if not screenshots:
        missing.append(SCREENSHOTS.relative_to(PROJECT_ROOT) / "*.webp")
    if missing:
        joined = ", ".join(str(path) for path in missing)
        raise FileNotFoundError(f"cannot build the demo; missing: {joined}")

    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(SITE_SOURCE, output)
    for source in required_media:
        destination = output / "media" / source.relative_to(MEDIA_SOURCE)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    assets_output = output / "assets"
    (assets_output / "screenshots").mkdir(parents=True)
    for name in REQUIRED_ASSETS:
        shutil.copy2(README_ASSETS / name, assets_output / name)
    for screenshot in screenshots:
        shutil.copy2(screenshot, assets_output / "screenshots" / screenshot.name)
    for name, source in ICONS.items():
        shutil.copy2(APP_ICONS / source, assets_output / name)
    shutil.copy2(SOCIAL_CARD, assets_output / SOCIAL_CARD.name)
    if release:
        (output / "release.json").write_text(
            json.dumps(release, indent=1) + "\n", encoding="utf-8"
        )
    (output / ".nojekyll").write_text("", encoding="utf-8")

    relative_output = output.relative_to(PROJECT_ROOT)
    print(f"GitHub Pages demo built at {relative_output}/index.html")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the static VoiceCut demo site")
    parser.add_argument(
        "--output",
        default=".pages-site",
        help="repository-local output directory (default: .pages-site)",
    )
    parser.add_argument(
        "--repository",
        default=REPOSITORY,
        help=f"GitHub repository whose latest release the page offers (default: {REPOSITORY})",
    )
    parser.add_argument(
        "--no-release",
        action="store_true",
        help="do not ask GitHub for the latest release (no release.json)",
    )
    args = parser.parse_args()
    release = (
        None
        if args.no_release
        else latest_release(args.repository, os.environ.get("GITHUB_TOKEN"))
    )
    build_demo(_safe_output_path(args.output), release)


if __name__ == "__main__":
    main()
