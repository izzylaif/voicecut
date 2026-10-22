"""The editor's HTTP API end to end, with a stand-in pipeline worker."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from voicecut.editor import server
from voicecut.editor.projects import Project
from voicecut.editor.timeline import load_proposal

from .editor_support import (
    FRAMES,
    SAMPLE_RATE,
    needs_ffmpeg,
    render_voicecut,
    source_audio,
    write_wav,
)

pytestmark = needs_ffmpeg

FAKE_WORKER = """
import json, sys, time
request = json.load(open(sys.argv[1]))
argv = request["argv"]
emit = lambda event: print(json.dumps(event), flush=True)
mode = open(argv[0] + ".mode").read().strip() if __import__("os").path.exists(argv[0] + ".mode") else "ok"
emit({"type": "stage", "stage": "preflight", "status": "started", "label": "Checking runtimes"})
print("a log line", file=sys.stderr, flush=True)
if mode == "slow":
    time.sleep(30)
if mode == "fail":
    emit({"type": "stage", "stage": "preflight", "status": "failed", "label": "Checking runtimes"})
    emit({"type": "error", "message": "no GPU today"})
    sys.exit(1)
emit({"type": "stage", "stage": "preflight", "status": "completed", "label": "Checking runtimes"})
emit({"type": "complete", "result": {"work_dir": open(argv[0] + ".work").read(), "output": argv[2]}})
"""


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")

    def spawn(request_path: Path) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, str(script), str(request_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    monkeypatch.setattr(server, "spawn_worker", spawn)
    # A test key so Voice Cut may start; the real environment's keys are hidden.
    for name in ("GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("GEMINI_API_KEY=test-key-0000\n", encoding="utf-8")
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text(
        "<!doctype html><title>editor</title>", encoding="utf-8"
    )
    return server.create_app(
        projects_dir=tmp_path / "projects",
        env_file=tmp_path / ".env",
        static_dir=static,
    )


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def _wait(client: TestClient, job_id: str, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def _events(client: TestClient, job_id: str) -> list[dict[str, Any]]:
    text = client.get(f"/api/jobs/{job_id}/events").text
    return [
        json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")
    ]


def _render(
    client: TestClient, project_id: str, revision: str, frames: int
) -> np.ndarray:
    parts = []
    for start in range(0, frames, 50_000):
        response = client.get(
            f"/api/projects/{project_id}/render",
            params={
                "rev": revision,
                "start": start,
                "frames": min(50_000, frames - start),
            },
        )
        assert response.status_code == 200
        channels = int(response.headers["x-channels"])
        parts.append(np.frombuffer(response.content, "<f4").reshape(channels, -1).T)
    return np.concatenate(parts)


def _seed_voicecut(app, project_id: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Prepare what the fake worker reports as its work directory."""

    store = app.state.service.store
    project: Project = store.get(project_id)
    work = project.voicecut_dir / ".voicecut-cache" / "run"
    wav = work / "00_media" / "source_audio.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(project.canonical_wav.read_bytes())
    render_voicecut(work, wav, monkeypatch=monkeypatch)
    Path(str(project.source_path) + ".work").write_text(str(work), encoding="utf-8")
    return work


def test_audio_project_from_import_to_export(
    app, client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    audio = source_audio()
    source = write_wav(tmp_path / "talk.wav", audio)
    state = client.post("/api/projects/import", json={"path": str(source)}).json()
    project_id = state["id"]
    assert state["media"]["frames"] == FRAMES and state["media"]["video"] is None
    assert state["proposal"] is None and state["timeline"] is None
    assert len(client.get(f"/api/projects/{project_id}/peaks").content) == 4 * -(
        -FRAMES // 256
    )

    # Before Voice Cut the timeline is the whole recording and plays as is.
    whole = {
        "schema_version": 3,
        "proposal_sha256": None,
        "items": state["source_items"],
    }
    saved = client.put(f"/api/projects/{project_id}/timeline", json=whole).json()
    assert saved["output_frames"] == FRAMES
    assert np.array_equal(_render(client, project_id, saved["revision"], FRAMES), audio)

    # Voice Cut, with live stages over SSE.
    work = _seed_voicecut(app, project_id, monkeypatch)
    job = client.post(
        f"/api/projects/{project_id}/voicecut", json={"language": "en"}
    ).json()
    assert _wait(client, job["job_id"])["status"] == "complete"
    events = _events(client, job["job_id"])
    assert [(e["stage"], e["status"]) for e in events if e["type"] == "stage"] == [
        ("preflight", "started"),
        ("preflight", "completed"),
    ]
    assert {"type": "log", "stream": "stderr", "line": "a log line"} in [
        {key: event[key] for key in ("type", "stream", "line")}
        for event in events
        if event["type"] == "log"
    ]
    assert events[-1]["type"] == "complete"
    state = client.get(f"/api/projects/{project_id}").json()
    assert state["run"]["status"] == "complete" and state["run"]["work_dir"] == str(
        work
    )
    proposal = state["proposal"]
    assert proposal["output_frames"] > 0
    assert state["timeline"] is None  # the whole-recording edit belonged to no proposal

    items = load_proposal(
        work / "05_final" / "final_boundary_plan.json"
    ).timeline_items()
    timeline = {
        "schema_version": 3,
        "proposal_sha256": proposal["plan_sha256"],
        "items": items,
    }
    saved = client.put(f"/api/projects/{project_id}/timeline", json=timeline).json()
    final, _ = sf.read(
        work / "05_final" / "final_cut.wav", dtype="float32", always_2d=True
    )
    assert np.array_equal(
        _render(client, project_id, saved["revision"], saved["output_frames"]), final
    )
    assert (
        client.get(f"/api/projects/{project_id}").json()["timeline"]["items"] == items
    )

    # An edit with an inserted pause, exported.
    first = next(item for item in items if item["kind"] == "clip")
    first["source_end_sample"] += 800
    first["end_origin"] = "manual"
    items.insert(
        items.index(first) + 1,
        {
            "kind": "pause",
            "id": "m-p",
            "length": 8000,
            "origin": "manual",
            "join": None,
        },
    )
    saved = client.put(f"/api/projects/{project_id}/timeline", json=timeline).json()
    preview = _render(client, project_id, saved["revision"], saved["output_frames"])
    destination = tmp_path / "exports" / "talk_edited.wav"
    destination.parent.mkdir()
    started = client.post(
        f"/api/projects/{project_id}/export",
        json={
            "timeline": timeline,
            "destination": str(destination),
            "overwrite": False,
        },
    ).json()
    job = _wait(client, started["job_id"])
    assert job["status"] == "complete", job
    exported, _ = sf.read(destination, dtype="float32", always_2d=True)
    assert len(exported) == len(preview) and np.max(np.abs(exported - preview)) < 2e-6
    download = client.get(
        f"/api/projects/{project_id}/exports/{started['export_id']}/download"
    )
    assert download.content == destination.read_bytes()
    again = client.post(
        f"/api/projects/{project_id}/export",
        json={
            "timeline": timeline,
            "destination": str(destination),
            "overwrite": False,
        },
    )
    assert again.status_code == 409


def test_api_rejects_bad_requests(client: TestClient, tmp_path: Path) -> None:
    source = write_wav(tmp_path / "talk.wav", source_audio())
    project_id = client.post("/api/projects/import", json={"path": str(source)}).json()[
        "id"
    ]
    assert (
        client.post(
            "/api/projects/import", json={"path": str(tmp_path / "missing.wav")}
        ).status_code
        == 404
    )
    bad = {"schema_version": 2, "proposal_sha256": None, "segments": [{"id": "a"}]}
    assert (
        client.put(f"/api/projects/{project_id}/timeline", json=bad).status_code == 400
    )
    assert (
        client.get(
            f"/api/projects/{project_id}/render",
            params={"rev": "nope", "start": 0, "frames": 10},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/projects/{project_id}/voicecut", json={"language": "xx"}
        ).status_code
        == 400
    )
    assert client.post(f"/api/projects/{project_id}/cancel").status_code == 409
    assert client.get("/api/projects/nope").status_code == 404
    assert (
        client.get(
            f"/api/projects/{project_id}/peaks/fine",
            params={"spp": 5, "start": 0, "count": 1},
        ).status_code
        == 400
    )
    timeline = {
        "schema_version": 2,
        "proposal_sha256": None,
        "segments": [
            {
                "id": "s",
                "source_start_sample": 0,
                "source_end_sample": FRAMES,
                "start_origin": "source",
                "end_origin": "source",
            }
        ],
    }
    response = client.post(
        f"/api/projects/{project_id}/export",
        json={"timeline": timeline, "destination": str(source), "overwrite": True},
    )
    assert response.status_code == 400 and "source" in response.json()["detail"]
    assert client.delete(f"/api/projects/{project_id}").status_code == 204
    assert client.get("/api/projects").json() == []


def test_failed_and_cancelled_runs_keep_the_project_usable(
    client: TestClient, tmp_path: Path
) -> None:
    source = write_wav(tmp_path / "talk.wav", source_audio())
    project_id = client.post("/api/projects/import", json={"path": str(source)}).json()[
        "id"
    ]
    project_source = Path(
        client.get(f"/api/projects/{project_id}").json()["source_path"]
    )
    Path(str(project_source) + ".mode").write_text("fail", encoding="utf-8")
    job = client.post(f"/api/projects/{project_id}/voicecut", json={}).json()
    assert _wait(client, job["job_id"])["status"] == "failed"
    run = client.get(f"/api/projects/{project_id}").json()["run"]
    assert run["status"] == "failed" and run["error"] == "no GPU today"

    Path(str(project_source) + ".mode").write_text("slow", encoding="utf-8")
    job = client.post(f"/api/projects/{project_id}/voicecut", json={}).json()
    deadline = time.monotonic() + 10
    while not any(
        event["type"] == "stage" for event in _events_so_far(client, job["job_id"])
    ):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert (
        client.post(f"/api/projects/{project_id}/voicecut", json={}).status_code == 409
    )
    assert client.post(f"/api/projects/{project_id}/cancel").json() == {
        "status": "cancelling"
    }
    assert _wait(client, job["job_id"])["status"] == "cancelled"
    assert (
        client.get(f"/api/projects/{project_id}").json()["run"]["status"] == "cancelled"
    )


def _events_so_far(client: TestClient, job_id: str) -> list[dict[str, Any]]:
    job = client.app.state.service.jobs.get(job_id)
    return job.events_since(0)


def test_upload_import_and_spa_fallback(client: TestClient, tmp_path: Path) -> None:
    source = write_wav(tmp_path / "upload.wav", source_audio())
    with source.open("rb") as handle:
        state = client.post(
            "/api/projects/import", files={"file": ("upload.wav", handle, "audio/wav")}
        ).json()
    assert state["uploaded"] is True and state["media"]["sample_rate"] == SAMPLE_RATE
    assert client.get("/").text.startswith("<!doctype html>")
    assert client.get("/some/client/route").text.startswith("<!doctype html>")
    assert client.get("/api/does-not-exist").status_code == 404
