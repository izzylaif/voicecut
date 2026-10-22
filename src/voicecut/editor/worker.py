"""Run one VoiceCut pipeline job as a subprocess with structured progress.

The editor server launches ``python -m voicecut.editor.worker <request.json>``
where the request is ``{"argv": [...]}`` (the ``voicecut`` command line).  The worker
reserves its original standard output for JSON-lines progress events,
redirects every other print (its own and its children's) to standard error,
and relays the pipeline's ``progress_callback`` stages to the editor UI.
"""

from __future__ import annotations

import _thread
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any

from .. import processes

STAGE_LABELS: dict[str, str] = {
    "preflight": "Checking runtimes",
    "media_input": "Preparing media",
    "analysis": "Analyzing speech",
    "transcription": "Transcribing",
    "hidden_retry_recovery": "Recovering hidden retries",
    "gap_recovery": "Recovering skipped speech",
    "semantic_plan": "Choosing takes",
    "final_render": "Aligning and rendering the edit",
    "publication": "Publishing",
}
STAGE_STATUSES = frozenset({"started", "completed", "cached", "skipped", "failed"})
CANCELLED_EXIT_STATUS = 130
# Set by the editor: "cancel" on standard input (or its end) stops the run.
STDIN_CONTROL_ENV = "VOICECUT_WORKER_STDIN_CONTROL"


class WorkerCancelled(BaseException):
    """Raised inside the worker when SIGTERM asks it to stop."""


class EventSink:
    """Serialise JSON-lines events to one file descriptor from any thread."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()

    def stage(
        self,
        stage: str,
        status: str,
        *,
        detail: str | None = None,
    ) -> None:
        if status not in STAGE_STATUSES:
            raise ValueError(f"unknown stage status: {status}")
        self.emit(
            {
                "type": "stage",
                "stage": stage,
                "status": status,
                "label": STAGE_LABELS.get(stage, stage),
                "detail": detail,
                "at": time.time(),
            }
        )

    def log(self, stream: str, line: str) -> None:
        self.emit({"type": "log", "stream": stream, "line": line.rstrip("\r\n")})


class ChildRegistry:
    """Remember the currently running pipeline child so SIGTERM can stop it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: subprocess.Popen[str] | None = None

    def set(self, process: subprocess.Popen[str] | None) -> None:
        with self._lock:
            self._current = process

    def terminate(self) -> None:
        with self._lock:
            process = self._current
        if process is not None:
            processes.terminate_tree(process)


def _pump(
    pipe: IO[str] | None,
    on_line: Callable[[str], None],
) -> None:
    if pipe is None:
        return
    with pipe:
        for line in pipe:
            on_line(line)


def make_streaming_runner(
    sink: EventSink,
    registry: ChildRegistry | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a ``subprocess.run`` stand-in that streams child output as events.

    ``full_pipeline._run`` calls it as ``runner(command, check=True, cwd=...,
    stderr=subprocess.PIPE, text=True, env=...)``.  Standard output lines are
    emitted as ``log`` events for ``stdout``; standard error lines are emitted
    as ``log`` events for ``stderr`` and also accumulated so the pipeline can
    keep its concise failure summary.
    """

    def runner(
        command: Sequence[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        check = bool(kwargs.pop("check", False))
        kwargs.pop("stdout", None)
        kwargs.pop("stderr", None)
        kwargs.pop("text", None)
        kwargs.pop("capture_output", None)
        for key, value in processes.own_group().items():
            kwargs.setdefault(key, value)
        stderr_lines: list[str] = []

        def on_stdout(line: str) -> None:
            sink.log("stdout", line)

        def on_stderr(line: str) -> None:
            stderr_lines.append(line)
            sink.log("stderr", line)

        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **kwargs,
        )
        if registry is not None:
            registry.set(process)
        readers = [
            threading.Thread(
                target=_pump,
                args=(process.stdout, on_stdout),
                daemon=True,
            ),
            threading.Thread(
                target=_pump,
                args=(process.stderr, on_stderr),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait()
        finally:
            if registry is not None:
                registry.set(None)
        for reader in readers:
            reader.join()
        stderr_text = "".join(stderr_lines)
        if check and returncode != 0:
            raise subprocess.CalledProcessError(
                returncode,
                list(command),
                output="",
                stderr=stderr_text,
            )
        return subprocess.CompletedProcess(
            list(command),
            returncode,
            stdout="",
            stderr=stderr_text,
        )

    return runner


class StageTracker:
    """Adapt ``run_full_pipeline`` progress events into editor stage events."""

    def __init__(self, sink: EventSink) -> None:
        self.sink = sink
        self.current_stage: str | None = None

    def __call__(self, event: dict[str, Any]) -> None:
        stage = str(event.get("stage"))
        status = str(event.get("status"))
        if status not in STAGE_STATUSES:
            return
        if status == "started":
            self.current_stage = stage
        elif self.current_stage == stage:
            self.current_stage = None
        self.sink.stage(stage, status)

    def fail(self) -> None:
        """Mark whichever stage was running when the pipeline raised."""

        if self.current_stage is not None:
            self.sink.stage(self.current_stage, "failed")
            self.current_stage = None


def load_request(path: Path) -> dict[str, Any]:
    """Read and validate the worker request file."""

    request = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise TypeError("worker request must be a JSON object")
    argv = request.get("argv")
    if not isinstance(argv, list) or not all(isinstance(v, str) for v in argv):
        raise TypeError("worker request argv must be a list of strings")
    return request


_registry = ChildRegistry()
_cancel_requested = threading.Event()


def request_cancel() -> None:
    """Stop the run as SIGTERM does, also where there is no SIGTERM to send.

    Windows cannot signal a process; there the stage's process tree is ended
    first, so the main thread stops waiting for it, then the SIGTERM handler
    runs in the main thread.
    """

    _cancel_requested.set()
    if processes.WINDOWS:
        _registry.terminate()
        _thread.interrupt_main(signal.SIGTERM)
    else:
        os.kill(os.getpid(), signal.SIGTERM)


def cancel_on_stdin() -> threading.Thread:
    """Stop when the editor writes "cancel" or goes away (standard input ends)."""

    def watch() -> None:
        for line in sys.stdin:
            if line.strip() == "cancel":
                break
        request_cancel()

    thread = threading.Thread(target=watch, daemon=True, name="worker-control")
    thread.start()
    return thread


def run_job(request: dict[str, Any], sink: EventSink) -> dict[str, Any]:
    """Run the pipeline described by ``request`` and return its ledger."""

    from .. import full_pipeline

    registry = _registry
    tracker = StageTracker(sink)

    def on_sigterm(signum: int, frame: object) -> None:
        registry.terminate()
        raise WorkerCancelled

    signal.signal(signal.SIGTERM, on_sigterm)
    if _cancel_requested.is_set():
        raise WorkerCancelled
    parser = full_pipeline.build_parser()
    args = parser.parse_args(list(request["argv"]))
    try:
        return full_pipeline.run_full_pipeline(
            args,
            runner=make_streaming_runner(sink, registry),
            progress_callback=tracker,
        )
    except WorkerCancelled:
        raise
    except BaseException:
        tracker.fail()
        raise


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m voicecut.editor.worker``."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: python -m voicecut.editor.worker REQUEST_JSON", file=sys.stderr)
        return 2
    if argv is None:
        if os.environ.get(STDIN_CONTROL_ENV) == "1":
            cancel_on_stdin()
        processes.keep_awake()
    events_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    events_stream = os.fdopen(events_fd, "w", encoding="utf-8", buffering=1)
    sink = EventSink(events_stream)
    try:
        request = load_request(Path(arguments[0]))
        result = run_job(request, sink)
    except WorkerCancelled:
        sink.emit({"type": "cancelled"})
        return CANCELLED_EXIT_STATUS
    except SystemExit as error:
        # argparse rejects invalid argv with SystemExit; report it as an error.
        message = f"pipeline arguments were rejected (exit status {error.code})"
        sink.emit({"type": "error", "message": message})
        return 1
    except Exception as error:  # noqa: BLE001 - every failure becomes an event
        sink.emit({"type": "error", "message": str(error) or type(error).__name__})
        return 1
    sink.emit({"type": "complete", "result": result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
