"""``voicecut-editor``: launch the browser-based VoiceCut editor.

The command starts the FastAPI server with uvicorn, optionally imports one
media file first, and opens the browser once ``/api/health`` answers.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..language_profiles import SUPPORTED_LANGUAGE_CODES
from ..planner_backends import PLANNER_BACKENDS
from .projects import DEFAULT_PROJECTS_DIR, PROJECT_ROOT, ProjectError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATIC_DIR = Path(__file__).resolve().parent / "static"
HEALTH_TIMEOUT_SECONDS = 30.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voicecut-editor",
        description="Open the VoiceCut editor in your browser.",
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        help="Optional audio or video file to import before opening the editor.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser window automatically.",
    )
    parser.add_argument(
        "--projects-dir",
        type=Path,
        default=DEFAULT_PROJECTS_DIR,
        help=f"Where editor projects are stored (default: {DEFAULT_PROJECTS_DIR}).",
    )
    parser.add_argument(
        "--language",
        choices=SUPPORTED_LANGUAGE_CODES,
        default="en",
        help="Default source language for new projects.",
    )
    parser.add_argument(
        "--planner-backend",
        choices=PLANNER_BACKENDS,
        default="gemini",
        help="Default semantic planner backend for new projects.",
    )
    parser.add_argument(
        "--planner-model",
        default=None,
        help="Default planner model (defaults to the backend's model).",
    )
    parser.add_argument(
        "--breath-cleanup",
        choices=("off", "replace"),
        default=None,
        help="Default breath cleanup mode (defaults to the language profile).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="Environment file holding planner API keys.",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Serve the API only; run the Vite dev server for the frontend.",
    )
    return parser


def defaults_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "language": args.language,
        "planner_backend": args.planner_backend,
        "planner_model": args.planner_model,
        "breath_cleanup": args.breath_cleanup,
    }


def wait_for_health(url: str, *, timeout: float = HEALTH_TIMEOUT_SECONDS) -> bool:
    """Poll ``url`` until it answers 200 or ``timeout`` seconds elapse."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.2)
    return False


def open_browser_when_ready(base_url: str, page_url: str) -> threading.Thread:
    """Open ``page_url`` in a background thread once the API is healthy."""

    def target() -> None:
        if wait_for_health(f"{base_url}/api/health"):
            with contextlib.suppress(Exception):
                webbrowser.open(page_url)

    thread = threading.Thread(target=target, daemon=True, name="open-browser")
    thread.start()
    return thread


def ensure_media_tools() -> None:
    """Put VoiceCut's own FFmpeg on PATH when the system has none."""

    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    bundled = PROJECT_ROOT / ".mfa-env" / "bin"
    if (bundled / "ffmpeg").is_file():
        os.environ["PATH"] = f"{bundled}{os.pathsep}{os.environ.get('PATH', '')}"


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ImportError:
        parser.error(
            "the editor server needs the 'editor' extra: "
            "pip install -e '.[editor]' (or run scripts/install.sh)"
        )
    ensure_media_tools()
    if shutil.which("ffmpeg") is None:
        parser.error("FFmpeg is required; install it (brew install ffmpeg)")
    from .server import create_app

    app = create_app(
        projects_dir=args.projects_dir,
        env_file=args.env_file.expanduser().absolute(),
        defaults=defaults_from_args(args),
        static_dir=None if args.dev else STATIC_DIR,
    )
    base_url = f"http://{args.host}:{args.port}"
    page_url = f"{base_url}/"
    if args.input is not None:
        try:
            project = app.state.service.store.import_path(args.input)
        except (FileNotFoundError, ProjectError) as error:
            parser.error(str(error))
        page_url = f"{base_url}/?project={project.id}"
        print(f"opened {args.input} as project {project.id}", flush=True)
    if args.dev:
        print("Development mode: serving the API only.")
        print("In another terminal run:  cd frontend && npm run dev")
    elif not (STATIC_DIR / "index.html").is_file():
        print(
            "The frontend is not built yet; run "
            "`cd frontend && npm install && npm run build` or ./scripts/install.sh.",
            file=sys.stderr,
        )
    print(f"VoiceCut editor: {page_url}", flush=True)
    if not args.no_browser and not args.dev:
        open_browser_when_ready(base_url, page_url)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
