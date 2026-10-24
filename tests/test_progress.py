from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from voicecut import progress
from voicecut.editor.speeds import RunSpeeds, stage_durations
from voicecut.editor.worker import EventSink, StageTracker, make_streaming_runner


def test_progress_lines_become_events_of_the_running_stage() -> None:
    stream = io.StringIO()
    sink = EventSink(stream)
    tracker = StageTracker(sink)
    tracker({"stage": "transcription", "status": "started"})
    runner = make_streaming_runner(sink, current_stage=lambda: tracker.current_stage)
    runner(
        [sys.executable, "-c", "print('[progress] 3/4 atoms'); print('hello')"],
        check=True,
    )
    tracker({"type": "estimate", "audio_seconds": 60.0, "stages": {"transcription": 12.0}})
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    kinds = [event["type"] for event in events]
    assert kinds == ["stage", "stage_progress", "log", "estimate"]
    assert events[1]["stage"] == "transcription" and events[1]["fraction"] == 0.75
    assert events[2]["line"] == "hello"  # ordinary output stays in the log
    assert events[3]["stages"] == {"transcription": 12.0}


def test_the_estimate_grows_with_the_recording_and_the_planner() -> None:
    short = progress.stage_seconds(60, planner_backend="openrouter", planner_model="google/gemma-4-31b-it")
    long = progress.stage_seconds(3600, planner_backend="openrouter", planner_model="google/gemma-4-31b-it")
    assert long["transcription"] > 10 * short["transcription"]
    local = progress.stage_seconds(3600, planner_backend="ollama", planner_model="gemma4:31b-mlx")
    kev = progress.stage_seconds(3600, planner_backend="ollama", planner_model="jaredpalmer/kev-4b")
    jev = progress.stage_seconds(3600, planner_backend="openrouter", planner_model="typesafe/jev-1.14")
    assert local["semantic_plan"] > kev["semantic_plan"] > jev["semantic_plan"]


def test_this_computers_speeds_replace_the_reference(tmp_path: Path) -> None:
    speeds = RunSpeeds(tmp_path / "run_speeds.json")
    reference = progress.stage_seconds(600, planner_backend="ollama", planner_model="jaredpalmer/kev-4b")
    stages, learned = speeds.estimate(600, "jaredpalmer/kev-4b", reference)
    assert stages == reference and learned is False
    events = [
        {"type": "stage", "stage": "transcription", "status": "started", "at": 10.0},
        {"type": "stage", "stage": "transcription", "status": "completed", "at": 318.0},
        {"type": "stage", "stage": "analysis", "status": "cached", "at": 318.0},
        {"type": "stage", "stage": "semantic_plan", "status": "started", "at": 320.0},
        {"type": "stage", "stage": "semantic_plan", "status": "completed", "at": 425.0},
    ]
    assert stage_durations(events) == {"transcription": 308.0, "semantic_plan": 105.0}
    speeds.record(events, 600, "jaredpalmer/kev-4b")
    stages, learned = speeds.estimate(1200, "jaredpalmer/kev-4b", reference)
    assert learned is True
    # 300 s of transcription work (beyond its fixed 8 s) per 600 s of recording
    assert stages["transcription"] == 608.0
    assert stages["semantic_plan"] == 205.0
    assert stages["final_render"] == reference["final_render"]  # not measured yet
    # another planner model has its own speed
    other, _ = speeds.estimate(1200, "jaredpalmer/kev-9b", reference)
    assert other["semantic_plan"] == reference["semantic_plan"]
