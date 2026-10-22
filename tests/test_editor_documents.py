"""The desktop app's side of the editor: ``.vc`` documents, Keychain keys,
and the engine's guard against other web pages."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from voicecut import processes
from voicecut.common import read_json
from voicecut.editor import documents, server, settings
from voicecut.editor.projects import Project
from voicecut.editor.timeline import load_proposal

from .editor_support import (
    FRAMES,
    needs_ffmpeg,
    render_voicecut,
    source_audio,
    write_wav,
)

pytestmark = needs_ffmpeg


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return server.create_app(
        projects_dir=tmp_path / "engine",
        env_file=tmp_path / "engine" / "voicecut.env",
        static_dir=static,
        desktop=True,
    )


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:47615")


def _finish_voicecut(app, project_id: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a real Voice Cut result where a finished run leaves it."""

    store = app.state.service.store
    project: Project = store.get(project_id)
    work = project.voicecut_dir / ".voicecut-cache" / "talk-gemini-0123456789ab"
    wav = work / "00_media" / "source_audio.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(project.canonical_wav.read_bytes())
    render_voicecut(work, wav, monkeypatch=monkeypatch)

    def complete(current: Project) -> None:
        current.run.update(status="complete", work_dir=str(work))

    store.update(project_id, complete)
    return work


def _export(client: TestClient, project_id: str, destination: Path) -> np.ndarray:
    state = client.get(f"/api/projects/{project_id}").json()
    timeline = {
        "schema_version": 3,
        "proposal_sha256": state["proposal"]["plan_sha256"],
        "items": state["timeline"]["items"]
        if state["timeline"]
        else load_proposal(
            Path(state["run"]["work_dir"]) / "05_final" / "final_boundary_plan.json"
        ).timeline_items(),
    }
    started = client.post(
        f"/api/projects/{project_id}/export",
        json={"timeline": timeline, "destination": str(destination), "overwrite": True},
    ).json()
    deadline = time.monotonic() + 30
    while client.get(f"/api/jobs/{started['job_id']}").json()["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    audio, _ = sf.read(destination, dtype="float32", always_2d=True)
    return audio


def test_a_document_is_made_moved_copied_and_reopened(
    app, client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    episode = tmp_path / "Episode"
    source = write_wav(episode / "talk.wav", source_audio())
    document = episode / "My Talk.vc"

    state = client.post(
        "/api/documents/create", json={"source": str(source), "path": str(document)}
    ).json()
    project_id = state["id"]
    assert state["document"] == {"path": str(document.resolve()), "name": "My Talk"}
    assert state["media"]["frames"] == FRAMES and not state["source_missing"]
    assert (document / "project.json").is_file()
    assert (document / "media" / "source_audio.wav").is_file()
    assert not (tmp_path / "engine" / project_id).exists()
    # A document never replaces another one.
    again = client.post(
        "/api/documents/create", json={"source": str(source), "path": str(document)}
    )
    assert again.status_code == 400

    work = _finish_voicecut(app, project_id, monkeypatch)
    state = client.get(f"/api/projects/{project_id}").json()
    assert state["proposal"]["output_frames"] > 0
    items = load_proposal(
        work / "05_final" / "final_boundary_plan.json"
    ).timeline_items()
    timeline = {
        "schema_version": 3,
        "proposal_sha256": state["proposal"]["plan_sha256"],
        "items": items,
    }
    assert client.put(f"/api/projects/{project_id}/timeline", json=timeline).is_success
    exported = _export(client, project_id, tmp_path / "first.wav")
    # An export leaves its record in the document, not its render.
    assert list(document.glob("exports/*/export_result.json"))
    assert not list(document.glob("exports/*/final_cut.wav"))

    # Save As: a copy that keeps the edit, edited from now on.
    copy = tmp_path / "Elsewhere" / "Copy.vc"
    copy.parent.mkdir()
    saved = client.post(
        f"/api/projects/{project_id}/save-as", json={"path": str(copy)}
    ).json()
    assert saved["id"] == project_id and saved["document"]["name"] == "Copy"
    assert saved["timeline"]["items"] == items
    assert saved["run"]["work_dir"].startswith(str(copy.resolve()))
    assert read_json(copy / "media" / "media_input.json")["canonical_audio"] == str(
        copy.resolve() / "media" / "source_audio.wav"
    )
    assert read_json(document / "project.json")["document"]["root"] == str(
        document.resolve()
    )
    assert np.array_equal(_export(client, project_id, tmp_path / "copy.wav"), exported)

    # The original, moved in the Finder with the folder of its recording,
    # opens with its edit and its finished run, next to the open copy.
    client.post(f"/api/projects/{project_id}/close")
    moved_folder = tmp_path / "Archive" / "Episode 3"
    moved_folder.parent.mkdir()
    shutil.move(episode, moved_folder)
    moved = moved_folder / "My Talk.vc"
    reopened = client.post("/api/documents/open", json={"path": str(moved)}).json()
    assert reopened["document"]["path"] == str(moved.resolve())
    assert reopened["timeline"]["items"] == items
    assert reopened["proposal"]["plan_sha256"] == state["proposal"]["plan_sha256"]
    assert reopened["run"]["status"] == "complete"
    assert reopened["source_path"] == str((moved_folder / "talk.wav").resolve())
    assert not reopened["source_missing"]
    assert np.array_equal(
        _export(client, reopened["id"], tmp_path / "moved.wav"), exported
    )
    copy_state = client.post("/api/documents/open", json={"path": str(copy)}).json()
    assert copy_state["id"] != reopened["id"]
    # Opening an open document finds it again.
    assert (
        client.post("/api/documents/open", json={"path": str(moved)}).json()["id"]
        == reopened["id"]
    )


def test_a_lost_source_is_reported_and_located(
    app, client: TestClient, tmp_path: Path
) -> None:
    source = write_wav(tmp_path / "talk.wav", source_audio())
    document = tmp_path / "talk.vc"
    state = client.post(
        "/api/documents/create", json={"source": str(source), "path": str(document)}
    ).json()
    project_id = state["id"]
    hidden = tmp_path / "hidden" / "renamed.wav"
    hidden.parent.mkdir()
    shutil.move(source, hidden)
    state = client.get(f"/api/projects/{project_id}").json()
    assert state["source_missing"]
    refused = client.post(f"/api/projects/{project_id}/voicecut", json={})
    assert refused.status_code == 400
    other = write_wav(tmp_path / "other.wav", source_audio()[::-1].copy())
    wrong = client.post(
        f"/api/projects/{project_id}/locate-source", json={"path": str(other)}
    )
    assert wrong.status_code == 400 and "not the original" in wrong.json()["detail"]
    located = client.post(
        f"/api/projects/{project_id}/locate-source", json={"path": str(hidden)}
    ).json()
    assert not located["source_missing"]
    assert located["source_path"] == str(hidden.resolve())


def test_a_browser_project_opens_as_a_document(tmp_path: Path, monkeypatch) -> None:
    """A project of the browser editor, its run kept elsewhere, as a .vc."""

    browser = server.create_app(
        projects_dir=tmp_path / "browser", env_file=tmp_path / ".env"
    )
    source = write_wav(tmp_path / "talk.wav", source_audio())
    project = browser.state.service.store.import_path(source)
    outside = tmp_path / "runs" / "full_work"
    wav = outside / "00_media" / "source_audio.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(project.canonical_wav.read_bytes())
    render_voicecut(outside, wav, monkeypatch=monkeypatch)

    def complete(current: Project) -> None:
        current.run.update(status="complete", work_dir=str(outside))

    browser.state.service.store.update(project.id, complete)
    document = tmp_path / "talk.vc"
    documents.copy_tree(project.root, document)
    # A render an older export left behind.
    old_render = document / "exports" / "20260101-000000-abcd" / "final_cut.wav"
    old_render.parent.mkdir(parents=True)
    old_render.write_bytes(b"RIFF")

    store = documents.DocumentStore(tmp_path / "engine")
    opened = store.open_document(document)
    work = Path(opened.run["work_dir"])
    assert work.parent == document.resolve() / "voicecut" / ".voicecut-cache"
    assert (work / "05_final" / "final_boundary_plan.json").is_file()
    assert opened.plan_path == work / "05_final" / "final_boundary_plan.json"
    assert not old_render.exists()
    assert read_json(document / "media" / "media_input.json")["canonical_audio"] == str(
        document.resolve() / "media" / "source_audio.wav"
    )


def test_only_the_app_may_call_the_engine(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200
    same = client.get("/api/config", headers={"Origin": "http://127.0.0.1:47615"})
    assert same.status_code == 200 and same.json()["desktop"] is True
    assert (
        client.get("/api/config", headers={"Origin": "https://example.com"}).status_code
        == 403
    )
    assert (
        client.get("/api/config", headers={"Host": "evil.example:47615"}).status_code
        == 403
    )
    assert (
        client.get("/api/config", headers={"Host": "localhost:47615"}).status_code
        == 200
    )


FAKE_SECURITY = """
import json, sys
from pathlib import Path
store = Path(sys.argv[1])
items = json.loads(store.read_text()) if store.exists() else {}
args = sys.argv[2:]
def option(name):
    return args[args.index(name) + 1]
if args[0] == "-i":
    import shlex
    words = shlex.split(sys.stdin.read())
    args = words
    items[option("-a")] = option("-w")
elif args[0] == "find-generic-password":
    if option("-a") not in items:
        sys.exit(44)
    print(items[option("-a")])
elif args[0] == "delete-generic-password":
    if items.pop(option("-a"), None) is None:
        sys.exit(44)
store.write_text(json.dumps(items))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX shell stands in")
def test_keys_in_the_keychain_reach_the_run_not_the_page(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    script = tmp_path / "security.py"
    script.write_text(FAKE_SECURITY, encoding="utf-8")
    tool = tmp_path / "security"
    tool.write_text(
        f'#!/bin/sh\nexec {sys.executable} {script} {tmp_path / "keychain.json"} "$@"\n',
        encoding="utf-8",
    )
    tool.chmod(0o755)
    keys = settings.KeychainKeys("VoiceCut-test", tool=str(tool))
    assert keys.get("GEMINI_API_KEY") is None
    keys.set("GEMINI_API_KEY", "AIzaKEYCHAIN1234")
    assert keys.get("GEMINI_API_KEY") == "AIzaKEYCHAIN1234"
    assert settings.key_value(keys, "GEMINI_API_KEY") == (
        "AIzaKEYCHAIN1234",
        "keychain",
    )
    with pytest.raises(settings.SettingsError):
        keys.set("GEMINI_API_KEY", 'has"quote')

    app = server.create_app(
        projects_dir=tmp_path / "engine",
        env_file=tmp_path / "missing.env",
        keys=keys,
        desktop=True,
    )
    service = app.state.service
    state = service.settings_state()
    gemini = next(p for p in state["planners"] if p["id"] == "gemini")
    assert state["key_store"]["kind"] == "keychain"
    assert gemini["key"] == {"set": True, "masked": "••••1234", "source": "keychain"}
    chosen = {"planner_backend": "gemini"}
    assert service.worker_environment(chosen) == {"GEMINI_API_KEY": "AIzaKEYCHAIN1234"}
    # A key from the editor's own environment is inherited, not copied.
    monkeypatch.setenv("GEMINI_API_KEY", "from-the-environment")
    assert service.worker_environment(chosen) == {}
    monkeypatch.delenv("GEMINI_API_KEY")
    keys.set("GEMINI_API_KEY", None)
    assert keys.get("GEMINI_API_KEY") is None


def test_stopping_the_engine_stops_voice_cut(app, tmp_path: Path) -> None:
    service = app.state.service
    job = service.jobs.create("voicecut", "talk-1")
    job.process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], **processes.own_group()
    )
    service.stop_jobs()
    assert job.process.poll() is not None
    assert job.cancel_requested


def test_a_document_can_be_opened_from_its_launcher(tmp_path: Path) -> None:
    """Linux and Windows open a document by its ``.vcproj`` file."""

    source = write_wav(tmp_path / "talk.wav", source_audio())
    store = documents.DocumentStore(tmp_path / "engine")
    created = store.create_document(source, tmp_path / "Talk.vc")
    launcher = tmp_path / "Talk.vc" / "Talk.vcproj"
    assert launcher.is_file()
    store.close_document(created.id)
    renamed = tmp_path / "Renamed.vc"
    shutil.move(tmp_path / "Talk.vc", renamed)
    opened = store.open_document(renamed / "Talk.vcproj")
    assert opened.root == renamed.resolve()
    assert sorted(path.name for path in renamed.glob("*.vcproj")) == ["Renamed.vcproj"]


def test_paths_of_a_document_made_on_another_system_are_rebased() -> None:
    record = {"canonical": "C:\\Users\\me\\Talk.vc\\media\\a.wav", "other": "x"}
    rebased = documents.rebase(record, "C:\\Users\\me\\Talk.vc", "/home/me/Talk.vc")
    assert rebased["canonical"].replace(os.sep, "/") == "/home/me/Talk.vc/media/a.wav"
    assert rebased["other"] == "x"


def test_keys_in_a_credential_store(monkeypatch) -> None:
    """Windows and Linux keep keys with ``keyring`` (an in-memory one here)."""

    keyring = pytest.importorskip("keyring")
    from keyring.backend import KeyringBackend

    class Memory(KeyringBackend):
        priority = 1

        def __init__(self) -> None:
            super().__init__()
            self.items: dict[tuple[str, str], str] = {}

        def get_password(self, service, username):
            return self.items.get((service, username))

        def set_password(self, service, username, password):
            self.items[(service, username)] = password

        def delete_password(self, service, username):
            if self.items.pop((service, username), None) is None:
                raise keyring.errors.PasswordDeleteError(username)

    monkeypatch.setattr(keyring, "get_keyring", lambda: Memory())
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    keys = settings.CredentialStoreKeys("VoiceCut-test")
    keys.set("GEMINI_API_KEY", "AIzaSTORE5678")
    assert settings.key_value(keys, "GEMINI_API_KEY") == (
        "AIzaSTORE5678",
        "credential_store",
    )
    keys.set("GEMINI_API_KEY", None)
    keys.set("GEMINI_API_KEY", None)  # removing twice is fine
    assert keys.get("GEMINI_API_KEY") is None
