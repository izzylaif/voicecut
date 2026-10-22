"""``voicecut-editor``: launch the browser-based VoiceCut editor.

The command starts the FastAPI server with uvicorn, optionally imports one
media file first, and opens the browser once ``/api/health`` answers.

The VoiceCut desktop app runs it as its engine with ``--desktop``: projects
are ``.vc`` documents, API keys live in the macOS Keychain, and the server
exits (stopping Voice Cut) when the app that started it is gone.
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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .. import processes
from ..language_profiles import SUPPORTED_LANGUAGE_CODES
from ..planner_backends import PLANNER_BACKENDS
from .projects import DEFAULT_PROJECTS_DIR, PROJECT_ROOT, ProjectError
from .settings import KEYCHAIN_SERVICE, EnvFileKeys, system_keys

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATIC_DIR = Path(__file__).resolve().parent / "static"
HEALTH_TIMEOUT_SECONDS = 30.0
GRACEFUL_SHUTDOWN_SECONDS = 2


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
        "--keys",
        choices=("env-file", "keychain"),
        default="env-file",
        help="Where API keys are kept: the --env-file (default), or the system's "
        f"credential store ({KEYCHAIN_SERVICE!r} items in the macOS Keychain, the "
        "Windows Credential Manager or a Secret Service keyring).",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Serve the API only; run the Vite dev server for the frontend.",
    )
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="Run as the engine of the VoiceCut desktop app (.vc documents).",
    )
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help="Stop when the process with this id exits.",
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


def exit_with_parent(parent_pid: int, stop: Callable[[], None]) -> threading.Thread:
    """Stop Voice Cut and exit once ``parent_pid`` has exited.

    The desktop app normally stops its engine itself; this covers the app
    crashing or being force-quit.
    """

    def leave() -> None:
        stop()
        os._exit(0)

    return processes.when_parent_exits(parent_pid, leave)


def ensure_media_tools() -> None:
    """Put VoiceCut's own FFmpeg on PATH when the system has none."""

    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    environment = PROJECT_ROOT / ".mfa-env"
    bundled = environment / ("Library/bin" if sys.platform == "win32" else "bin")
    if any((bundled / name).is_file() for name in ("ffmpeg", "ffmpeg.exe")):
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

    env_file = args.env_file.expanduser().absolute()
    app = create_app(
        projects_dir=args.projects_dir,
        env_file=env_file,
        defaults=defaults_from_args(args),
        static_dir=None if args.dev else STATIC_DIR,
        keys=(
            system_keys(args.projects_dir / "keys.env")
            if args.keys == "keychain"
            else EnvFileKeys(env_file)
        ),
        desktop=args.desktop,
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
    if not (args.no_browser or args.dev or args.desktop):
        open_browser_when_ready(base_url, page_url)
    if args.parent_pid is not None:
        exit_with_parent(args.parent_pid, app.state.service.shutdown)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        # Open event streams must not hold up stopping Voice Cut.
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
