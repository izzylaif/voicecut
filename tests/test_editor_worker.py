from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from voicecut import processes
from voicecut.editor import worker
from voicecut.editor.worker import (
    STAGE_LABELS,
    ChildRegistry,
    EventSink,
    StageTracker,
    WorkerCancelled,
    make_streaming_runner,
)


def _events(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line]


def _stage_pairs(events: list[dict]) -> list[tuple[str, str]]:
    return [(e["stage"], e["status"]) for e in events if e["type"] == "stage"]


CHILD_SCRIPT = (
    "import sys\n"
    "print('out one'); print('out two', flush=True)\n"
    "print('err one', file=sys.stderr); print('err two', file=sys.stderr)\n"
    "sys.exit(int(sys.argv[1]))\n"
)


def test_streaming_runner_streams_output_and_raises_on_failure() -> None:
    buffer = io.StringIO()
    runner = make_streaming_runner(EventSink(buffer), ChildRegistry())
    command = [sys.executable, "-c", CHILD_SCRIPT, "3"]
    with pytest.raises(subprocess.CalledProcessError) as raised:
        runner(
            command,
            check=True,
            cwd=Path.cwd(),
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
    assert raised.value.returncode == 3
    assert raised.value.cmd == command
    assert raised.value.stderr == "err one\nerr two\n"
    events = _events(buffer)
    assert all(event["type"] == "log" for event in events)
    assert [e["line"] for e in events if e["stream"] == "stdout"] == [
        "out one",
        "out two",
    ]
    assert [e["line"] for e in events if e["stream"] == "stderr"] == [
        "err one",
        "err two",
    ]


def test_streaming_runner_returns_completed_process_on_success() -> None:
    buffer = io.StringIO()
    registry = ChildRegistry()
    runner = make_streaming_runner(EventSink(buffer), registry)
    command = [sys.executable, "-c", CHILD_SCRIPT, "0"]
    completed = runner(command, check=True, stderr=subprocess.PIPE, text=True)
    assert isinstance(completed, subprocess.CompletedProcess)
    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == "err one\nerr two\n"
    assert len(_events(buffer)) == 4
    # The registry forgets the child once it has exited.
    registry.terminate()


def test_child_registry_terminates_process_group() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        **processes.own_group(),
    )
    registry = ChildRegistry()
    registry.set(process)
    registry.terminate()
    status = process.wait(timeout=10)
    # POSIX asks the group to stop; Windows ends the tree.
    assert status == (1 if processes.WINDOWS else -signal.SIGTERM)


def test_event_sink_stage_events_carry_labels() -> None:
    buffer = io.StringIO()
    sink = EventSink(buffer)
    sink.stage("transcription", "started")
    sink.stage("hidden_retry_recovery", "skipped", detail="ru")
    with pytest.raises(ValueError, match="unknown stage status"):
        sink.stage("analysis", "bogus")
    events = _events(buffer)
    assert events[0]["type"] == "stage"
    assert events[0]["label"] == "Transcribing"
    assert events[0]["detail"] is None
    assert isinstance(events[0]["at"], float)
    assert events[1]["detail"] == "ru"
    assert set(STAGE_LABELS) >= {
        "preflight",
        "media_input",
        "analysis",
        "transcription",
        "hidden_retry_recovery",
        "gap_recovery",
        "semantic_plan",
        "final_render",
        "publication",
    }


def test_stage_tracker_translates_pipeline_events() -> None:
    buffer = io.StringIO()
    tracker = StageTracker(EventSink(buffer))
    tracker({"stage": "preflight", "status": "started"})
    tracker({"stage": "preflight", "status": "completed"})
    tracker({"stage": "media_input", "status": "cached"})
    tracker({"stage": "hidden_retry_recovery", "status": "skipped"})
    tracker({"stage": "final_render", "status": "cached"})
    tracker({"stage": "publication", "status": "started"})
    tracker.fail()
    pairs = _stage_pairs(_events(buffer))
    assert pairs[:4] == [
        ("preflight", "started"),
        ("preflight", "completed"),
        ("media_input", "cached"),
        ("hidden_retry_recovery", "skipped"),
    ]
    assert pairs[4:] == [
        ("final_render", "cached"),
        ("publication", "started"),
        ("publication", "failed"),
    ]
    labels = {e["stage"]: e["label"] for e in _events(buffer) if e["type"] == "stage"}
    assert labels["preflight"] == "Checking runtimes"
    assert labels["final_render"] == "Aligning and rendering the edit"


def test_run_job_runs_pipeline_with_streaming_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voicecut import full_pipeline

    buffer = io.StringIO()
    seen: dict[str, object] = {}

    def fake_run_full_pipeline(args, *, runner, progress_callback):
        seen["input"] = args.input
        seen["work_dir"] = args.work_dir
        progress_callback({"stage": "preflight", "status": "started"})
        progress_callback({"stage": "preflight", "status": "completed"})
        progress_callback({"stage": "final_render", "status": "started"})
        runner(
            [sys.executable, "-c", "print('child says hi')"],
            check=True,
            cwd=tmp_path,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        progress_callback({"stage": "final_render", "status": "completed"})
        return {"status": "complete", "output": str(args.output)}

    monkeypatch.setattr(full_pipeline, "run_full_pipeline", fake_run_full_pipeline)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        result = worker.run_job(
            {
                "argv": [
                    str(tmp_path / "in.wav"),
                    "-o",
                    str(tmp_path / "out.wav"),
                    "--work-dir",
                    str(tmp_path / "work"),
                ],
            },
            EventSink(buffer),
        )
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert result == {"status": "complete", "output": str(tmp_path / "out.wav")}
    assert seen["work_dir"] == tmp_path / "work"
    events = _events(buffer)
    pairs = _stage_pairs(events)
    assert pairs[:3] == [
        ("preflight", "started"),
        ("preflight", "completed"),
        ("final_render", "started"),
    ]
    assert pairs[-1] == ("final_render", "completed")
    assert {"type": "log", "stream": "stdout", "line": "child says hi"} in events


def test_run_job_marks_stage_failed_and_handles_sigterm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voicecut import full_pipeline

    request = {
        "argv": [str(tmp_path / "in.wav"), "--work-dir", str(tmp_path / "work")],
    }

    def failing(args, *, runner, progress_callback):
        progress_callback({"stage": "analysis", "status": "started"})
        raise full_pipeline.FullPipelineError("analysis exploded")

    def cancelled(args, *, runner, progress_callback):
        progress_callback({"stage": "transcription", "status": "started"})
        worker.request_cancel()
        time.sleep(1)  # the handler runs in this (main) thread
        raise AssertionError("SIGTERM handler should have interrupted")

    previous = signal.getsignal(signal.SIGTERM)
    try:
        buffer = io.StringIO()
        monkeypatch.setattr(full_pipeline, "run_full_pipeline", failing)
        with pytest.raises(full_pipeline.FullPipelineError):
            worker.run_job(request, EventSink(buffer))
        assert _stage_pairs(_events(buffer)) == [
            ("analysis", "started"),
            ("analysis", "failed"),
        ]

        buffer = io.StringIO()
        monkeypatch.setattr(full_pipeline, "run_full_pipeline", cancelled)
        with pytest.raises(WorkerCancelled):
            worker.run_job(request, EventSink(buffer))
        assert _stage_pairs(_events(buffer)) == [("transcription", "started")]
    finally:
        signal.signal(signal.SIGTERM, previous)
        worker._cancel_requested.clear()


def test_load_request_rejects_malformed_requests(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(TypeError):
        worker.load_request(path)
    path.write_text(json.dumps({"argv": ["a", 3]}), encoding="utf-8")
    with pytest.raises(TypeError, match="argv"):
        worker.load_request(path)


def test_worker_module_frames_events_on_stdout(tmp_path: Path) -> None:
    """The real module entry point writes JSON lines to fd 1 and nothing else."""

    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"argv": [str(tmp_path / "missing.wav")]}),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "voicecut.editor.worker", str(request)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 1
    events = [json.loads(line) for line in completed.stdout.splitlines() if line]
    assert [(e["type"], e.get("stage"), e.get("status")) for e in events] == [
        ("stage", "preflight", "started"),
        ("stage", "preflight", "failed"),
        ("error", None, None),
    ]
    assert "missing.wav" in events[-1]["message"]

    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "voicecut.editor.worker", str(bad)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 1
    event = json.loads(completed.stdout.strip())
    assert event["type"] == "error"
    assert "argv" in event["message"]
