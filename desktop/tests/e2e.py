"""End-to-end smoke test of a VoiceCut test build (cargo feature `automation`).

    python3 desktop/tests/e2e.py --app PATH [--recording WAV] [--full]

Starts the app (a binary, an AppImage, or an installed VoiceCut.exe) with an
empty data directory whose path has a space in it, and through its
automation port:

1. runs the first-launch setup as the Install button does, until the engine
   is up;
2. runs desktop/tests/pipeline_smoke.py in the runtime it installed (Whisper,
   CTC and MFA; the whole Voice Cut with --full and GEMINI_API_KEY set);
3. creates a project from the recording through File > New Project, checks
   the editor shows it, closes it and opens it again from its launcher file,
   once from inside the app and once by starting the app a second time;
4. quits, and checks that the app and its engine exit.

Prints what it saw as JSON.  On GitHub Actions a failure is also reported as
an annotation with the tail of the app's logs.  Linux needs a display and a
D-Bus session: run it under `xvfb-run -a dbus-run-session --`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
WINDOWS = sys.platform == "win32"
SETUP_TIMEOUT = 75 * 60
PIPELINE_TIMEOUT = 60 * 60


class Failure(Exception):
    pass


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(what: str, predicate: Any, timeout: float, step: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise Failure(f"timed out waiting for {what}")


class App:
    def __init__(self, app: Path, data: Path, log: Path) -> None:
        self.path = app
        self.data = data
        self.port = free_port()
        self.log = log
        self.environment = {
            **os.environ,
            "VOICECUT_DATA_DIR": str(data),
            "VOICECUT_AUTOMATION_PORT": str(self.port),
        }
        if not (Path("/dev/fuse").exists() and shutil.which("fusermount")):
            # Without FUSE an AppImage unpacks itself to a folder, which every
            # instance shares: keep it when a second instance exits.
            self.environment.update(APPIMAGE_EXTRACT_AND_RUN="1", NO_CLEANUP="1")
        self.process: subprocess.Popen[bytes] | None = None

    def start(self, *arguments: str) -> subprocess.Popen[bytes]:
        with self.log.open("ab") as log:
            return subprocess.Popen(
                [str(self.path), *arguments],
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

    def launch(self, *arguments: str) -> None:
        self.process = self.start(*arguments)
        wait_for("the automation port", self.reachable, 180)

    def reachable(self) -> bool:
        if self.process is not None and self.process.poll() is not None:
            raise Failure(f"the app exited with status {self.process.returncode}")
        try:
            self.call("/status", timeout=5)
        except OSError:
            return False
        return True

    def call(self, path: str, body: Any = None, timeout: float = 120) -> Any:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body or {}).encode(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def eval(
        self, script: str, label: str | None = None, timeout_ms: int = 30000
    ) -> Any:
        body: dict[str, Any] = {"script": script, "timeout_ms": timeout_ms}
        if label:
            body["label"] = label
        result = self.call("/eval", body, timeout=timeout_ms / 1000 + 15)
        if not result.get("ok"):
            raise Failure(
                f"script failed in {label or 'the app'}: {result.get('value')}"
            )
        return result.get("value")

    def windows(self) -> list[dict[str, Any]]:
        return self.call("/windows")

    def documents(self) -> list[dict[str, Any]]:
        return [w for w in self.windows() if isinstance(w.get("role"), dict)]


def runtime_python(data: Path) -> Path:
    venv = data / "runtime" / ".venv"
    return venv / "Scripts" / "python.exe" if WINDOWS else venv / "bin" / "python"


def engine_path(data: Path) -> str:
    """PATH as the app gives it to its engine (src-tauri/src/engine.rs)."""

    mfa = data / "runtime" / ".mfa-env"
    entries = [data / "tools"]
    if WINDOWS:
        entries += [mfa / "Library" / "bin", mfa / "Scripts", mfa]
    else:
        entries += [mfa / "bin"]
    system = os.environ.get("PATH", "") if WINDOWS else "/usr/bin:/bin:/usr/sbin:/sbin"
    return os.pathsep.join([*map(str, entries), system])


def run_setup(app: App, report: dict[str, Any]) -> None:
    status = wait_for(
        "the first status",
        lambda: (s := app.call("/status"))["phase"] != "checking" and s,
        120,
    )
    report["first_phase"] = status["phase"]
    if status["phase"] == "needs_setup":
        app.eval("await window.__TAURI__.core.invoke('start_setup'); return true;")
    started = time.monotonic()
    last = None
    while True:
        status = app.call("/status")
        phase = status["phase"]
        if phase == "ready":
            break
        if phase == "failed":
            raise Failure(
                f"setup failed during {status.get('during')}: {status.get('message')}"
            )
        steps = (status.get("setup") or {}).get("steps") or []
        running = [s["label"] for s in steps if s.get("state") == "running"]
        step = running[0] if running else phase
        if step != last:
            print(f"[{time.monotonic() - started:6.0f}s] {step}", flush=True)
            last = step
        if time.monotonic() - started > SETUP_TIMEOUT:
            raise Failure(f"setup took over {SETUP_TIMEOUT // 60} minutes ({phase})")
        time.sleep(5)
    report["setup_minutes"] = round((time.monotonic() - started) / 60, 1)
    report["engine"] = status["url"]
    with urllib.request.urlopen(status["url"], timeout=30) as response:
        if response.status != 200:
            raise Failure(f"the engine answered {response.status}")


def run_pipeline(data: Path, recording: Path, work: Path, full: bool) -> dict[str, Any]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX"}
    }
    environment.update(
        PATH=engine_path(data),
        PYTHONNOUSERSITE="1",
        PYTHONUNBUFFERED="1",
        PYTHONUTF8="1",
        MAMBA_ROOT_PREFIX=str(data / "mamba"),
    )
    command = [
        str(runtime_python(data)),
        str(HERE / "pipeline_smoke.py"),
        str(recording),
        str(work),
    ]
    if full:
        command.append("--full")
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=data / "runtime",
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PIPELINE_TIMEOUT,
        check=False,
    )
    output = completed.stdout
    summary_start = output.rfind("\n{\n")
    try:
        summary = json.loads(
            output[summary_start + 1 :] if summary_start >= 0 else output
        )
    except json.JSONDecodeError:
        summary = {"ok": False, "error": "no summary", "stdout": output[-3000:]}
    summary["minutes"] = round((time.monotonic() - started) / 60, 1)
    if not summary.get("ok"):
        summary["stderr"] = completed.stderr[-3000:]
        raise Failure(f"pipeline smoke failed: {json.dumps(summary)[:6000]}")
    return summary


def editor_state(app: App, label: str) -> dict[str, Any]:
    return app.eval(
        """
        for (let i = 0; i < 240; i++) {
          if (document.querySelector('.timeline canvas')) break;
          await new Promise((resolve) => setTimeout(resolve, 250));
        }
        return {
          title: document.title,
          timeline: !!document.querySelector('.timeline canvas'),
          text: document.body.innerText.slice(0, 200),
        };
        """,
        label=label,
        timeout_ms=90000,
    )


def same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(
        os.path.realpath(b)
    )


def check_documents(app: App, recording: Path, folder: Path, report: dict) -> None:
    document = folder / "E2E Project.vc"
    launcher = document / "E2E Project.vcproj"

    def project_window() -> dict[str, Any] | None:
        return next(
            (w for w in app.documents() if same_path(w["role"]["path"], document)),
            None,
        )

    def closed() -> bool:
        return project_window() is None

    def opened(what: str) -> dict[str, Any]:
        window = wait_for(what, project_window, 300, 2)
        state = editor_state(app, window["label"])
        if not state["timeline"]:
            raise Failure(f"{what} shows no timeline: {state}")
        return window

    app.call("/dialogs", {"answers": [str(recording), str(document)]})
    app.call("/menu", {"id": "new"})
    window = opened("the new project")
    if not launcher.is_file():
        raise Failure(f"the project has no launcher: {sorted(document.iterdir())}")
    report["new_project"] = {"title": window["title"], "path": window["role"]["path"]}

    if sys.platform == "darwin":
        # The app stays without windows; the Finder opens the document again.
        app.call("/close", {"label": window["label"]})
        wait_for("the project window to close", closed, 120)
        app.call("/open", {"paths": [str(launcher)]})
        opened("the project reopened from its launcher")
        report["reopened_from_launcher"] = True
        return

    # Opening the launcher again comes back to the same window, also when the
    # file manager starts a second VoiceCut for it: that one hands it over.
    app.call("/open", {"paths": [str(launcher)]})
    second = app.start(str(launcher))
    try:
        second.wait(timeout=120)
    except subprocess.TimeoutExpired:
        second.kill()
        raise Failure("a second VoiceCut did not hand its file over and quit") from None
    time.sleep(2)
    windows = app.documents()
    if len(windows) != 1 or project_window() is None:
        raise Failure(f"opening the project again left these windows: {windows}")
    report["second_instance"] = {"exit": second.returncode, "windows": len(windows)}

    # Closing the last window quits VoiceCut on Linux and Windows ...
    assert app.process is not None
    app.call("/close", {"label": window["label"]})
    try:
        app.process.wait(timeout=90)
    except subprocess.TimeoutExpired:
        raise Failure("closing the last window did not quit VoiceCut") from None
    report["closing_quits"] = app.process.returncode
    # ... and double-clicking the launcher starts it with the project.
    app.launch(str(launcher))
    wait_for("the engine", lambda: app.call("/status")["phase"] == "ready", 300, 2)
    opened("the project opened by starting VoiceCut with its launcher")
    report["started_with_launcher"] = True


def quit_app(app: App, report: dict[str, Any]) -> None:
    engine = report.get("engine")
    app.call("/quit")
    assert app.process is not None
    try:
        app.process.wait(timeout=90)
    except subprocess.TimeoutExpired:
        raise Failure("the app did not quit within 90 seconds") from None
    if engine:

        def engine_down() -> bool:
            try:
                urllib.request.urlopen(engine, timeout=3)
            except OSError:
                return True
            return False

        wait_for("the engine to stop", engine_down, 30)
    report["quit"] = app.process.returncode


def tail(path: Path, limit: int = 2500) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def annotate(title: str, message: str, level: str = "error") -> None:
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level} title={title}::{escaped[:3500]}", flush=True)


def check_appimage_integration(report: dict[str, Any]) -> None:
    """An AppImage adds itself to the user's applications and file types."""

    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    entry = data / "applications" / "voicecut-appimage.desktop"
    mime = data / "mime" / "packages" / "voicecut.xml"
    icon = data / "icons/hicolor/256x256/mimetypes/application-x-voicecut-project.png"
    wait_for("the AppImage's menu entry", entry.is_file, 30)
    text = entry.read_text(encoding="utf-8")
    if "MimeType=application/x-voicecut-project;" not in text or " %F" not in text:
        raise Failure(f"the AppImage's menu entry is wrong:\n{text}")
    for path in (mime, icon):
        if not path.is_file():
            raise Failure(f"the AppImage did not install {path}")
    report["appimage_integration"] = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument(
        "--recording", type=Path, default=ROOT / "examples/media/audio/example_en.wav"
    )
    parser.add_argument("--work", type=Path, default=None)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    work = args.work or Path(tempfile.mkdtemp(prefix="voicecut-e2e-"))
    data = work / "VoiceCut Data"
    projects = work / "My Projects"
    projects.mkdir(parents=True, exist_ok=True)
    recording = projects / args.recording.name
    shutil.copy2(args.recording, recording)
    app = App(args.app.absolute(), data, work / "app.log")
    report: dict[str, Any] = {"platform": sys.platform, "data": str(data)}
    step = "launch"
    try:
        app.launch()
        report["menu"] = [item.get("submenu") for item in app.call("/menu-bar") or []]
        if app.path.suffix == ".AppImage":
            check_appimage_integration(report)
        step = "setup"
        run_setup(app, report)
        step = "pipeline"
        full = args.full and bool(os.environ.get("GEMINI_API_KEY"))
        report["pipeline"] = run_pipeline(data, recording, work / "pipeline", full)
        step = "documents"
        check_documents(app, recording, projects, report)
        step = "quit"
        quit_app(app, report)
        report["ok"] = True
    except Exception as error:  # noqa: BLE001 - reported with the logs
        report["ok"] = False
        report["failed_step"] = step
        report["error"] = f"{type(error).__name__}: {error}"
        if app.process is not None:
            report["app_exit_status"] = app.process.poll()
        logs = data / "logs"
        annotate(
            f"e2e {step}: {type(error).__name__}",
            f"{error} (app exit status: {report.get('app_exit_status')})",
        )
        for name, path in (
            ("app output", app.log),
            ("setup.log", logs / "setup.log"),
            ("engine.log", logs / "engine.log"),
        ):
            text = tail(path)
            if text:
                annotate(f"e2e {step}: {name}", text)
                print(f"\n----- {name} -----\n{text}", flush=True)
    finally:
        if app.process is not None and app.process.poll() is None:
            app.process.kill()
    summary = json.dumps(report, indent=1, ensure_ascii=False)
    print(summary, flush=True)
    if report["ok"]:
        annotate(f"e2e passed on {sys.platform}", summary, level="notice")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
