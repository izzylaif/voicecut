"""API keys, planner choice and live model lists (voicecut.editor.settings)."""

from __future__ import annotations

import io
import json
import os
import stat
import time
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from voicecut.editor import server, settings
from voicecut.planner_backends import DEFAULT_GEMINI_MODEL

from .editor_support import needs_ffmpeg, source_audio, write_wav

GEMINI_PAYLOAD = {
    "models": [
        {
            "name": f"models/{name}",
            "displayName": label,
            "supportedGenerationMethods": methods,
            "inputTokenLimit": limit,
            "outputTokenLimit": 65536,
        }
        for name, label, methods, limit in [
            ("gemini-2.5-pro", "Gemini 2.5 Pro", ["generateContent"], 1048576),
            (
                "gemini-3.1-pro-preview",
                "Gemini 3.1 Pro Preview",
                ["generateContent"],
                1048576,
            ),
            ("gemini-3.5-flash", "Gemini 3.5 Flash", ["generateContent"], 1048576),
            ("gemini-3.8-flash", "Gemini 3.8 Flash", ["generateContent"], 1048576),
            (
                "gemini-3.5-flash-lite",
                "Gemini 3.5 Flash Lite",
                ["generateContent"],
                1048576,
            ),
            (
                "gemini-flash-latest",
                "Gemini Flash Latest",
                ["generateContent"],
                1048576,
            ),
            ("gemini-3.8-flash-tts", "TTS", ["generateContent"], 8192),
            ("gemini-3-pro-image", "Nano Banana Pro", ["generateContent"], 131072),
            ("lyria-3.5", "Lyria", ["generateContent"], 1048576),
            ("gemini-embedding-2", "Embedding", ["embedContent"], 2048),
            ("gemma-4-31b-it", "Gemma 4", ["generateContent"], 262144),
        ]
    ]
}


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_saving_a_key_keeps_the_rest_of_the_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# planner keys\nexport GEMINI_API_KEY=old\nOTHER=1 # note\n", encoding="utf-8"
    )
    os.chmod(env, 0o600)
    settings.save_key(env, "GEMINI_API_KEY", "AIzaNEW123456")
    settings.save_key(env, "OPENAI_API_KEY", "sk-abc/def+ghi")
    assert env.read_text(encoding="utf-8") == (
        "# planner keys\nGEMINI_API_KEY=AIzaNEW123456\nOTHER=1 # note\nOPENAI_API_KEY=sk-abc/def+ghi\n"
    )
    if os.name != "nt":  # Windows has no POSIX permission bits
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert settings.read_env_file(env)["OTHER"] == "1"
    settings.save_key(env, "GEMINI_API_KEY", None)
    assert "GEMINI_API_KEY" not in env.read_text(encoding="utf-8")
    with pytest.raises(settings.SettingsError):
        settings.save_key(env, "OPENAI_API_KEY", "two words")


def test_a_new_env_file_is_private(tmp_path: Path) -> None:
    env = tmp_path / "keys.env"
    settings.save_key(env, "DEEPSEEK_API_KEY", "ds-1234567890")
    if os.name != "nt":  # Windows has no POSIX permission bits
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert settings.key_value(env, "DEEPSEEK_API_KEY") == ("ds-1234567890", "env_file")


def test_the_environment_wins_over_the_file_like_in_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=from-file-1234\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "from-shell-9876")
    assert settings.key_value(env, "GEMINI_API_KEY") == (
        "from-shell-9876",
        "environment",
    )
    assert settings.mask("from-shell-9876") == "••••9876"
    assert settings.mask("short") == "••••"


def _fake_get(monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def get(url: str, headers: dict[str, str]) -> Any:
        calls.append((url, headers))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(settings, "_get_json", get)
    return calls


def test_gemini_models_are_planner_models_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=AIza-test-key\n", encoding="utf-8")
    calls = _fake_get(monkeypatch, GEMINI_PAYLOAD)
    catalog = settings.ModelCatalog(env)
    listed = catalog.models("gemini")
    ids = [model["id"] for model in listed["models"]]
    # Gemma models are left out: the Gemini API meters them at 16,000 input
    # tokens a minute.
    assert ids == [
        DEFAULT_GEMINI_MODEL,
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-pro-preview",
        "gemini-2.5-pro",
        "gemini-flash-latest",
    ]
    assert listed["models"][0]["default"] is True and listed["error"] is None
    # The key goes in a header, never in the URL.
    assert (
        "AIza" not in calls[0][0] and calls[0][1]["x-goog-api-key"] == "AIza-test-key"
    )
    catalog.models("gemini")
    assert len(calls) == 1  # cached
    catalog.models("gemini", refresh=True)
    assert len(calls) == 2


def test_openai_models_skip_audio_image_and_embedding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-test-1234\n", encoding="utf-8")
    ids = [
        "gpt-5.5",
        "gpt-5-mini",
        "gpt-4o-audio-preview",
        "gpt-image-1",
        "text-embedding-3-large",
        "o4-mini",
        "whisper-1",
        "gpt-realtime",
        "tts-1",
        "omni-moderation-latest",
    ]
    calls = _fake_get(monkeypatch, {"data": [{"id": model} for model in ids]})
    listed = settings.ModelCatalog(env).models("openai")
    assert [model["id"] for model in listed["models"]] == [
        "gpt-5-mini",
        "gpt-5.5",
        "o4-mini",
    ]
    assert calls[0][0] == "https://api.openai.com/v1/models"
    assert calls[0][1]["Authorization"] == "Bearer sk-test-1234"


def test_openrouter_checks_the_key_then_lists_text_models_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=sk-or-test-1234\n", encoding="utf-8")

    def model(name: str, created: int, *, context: int = 262144, out: str = "text"):
        return {
            "id": name,
            "name": name.split("/")[1],
            "created": created,
            "context_length": context,
            "architecture": {"input_modalities": ["text"], "output_modalities": [out]},
        }

    payload = {
        "data": [
            model("google/gemma-4-31b-it", 1775000000),
            model("qwen/qwen3.8-27b", 1786000000),
            model("mistralai/ministral-8b-2512", 1765000000),
            model("deepseek/deepseek-v4.1-flash:batch", 1787000000),
            model("qwen/qwen3.5-9b:free", 1772000000),
            model("google/gemini-3.1-flash-image", 1780000000, out="image"),
            model("microsoft/phi-4", 1730000000, context=16384),
        ]
    }
    calls: list[tuple[str, dict]] = []

    def get(url: str, headers: dict[str, str]) -> Any:
        calls.append((url, headers))
        return {"data": {"label": "sk-or-…1234"}} if url.endswith("/key") else payload

    monkeypatch.setattr(settings, "_get_json", get)
    listed = settings.ModelCatalog(env).models("openrouter")
    # Jev is served by OpenRouter's Decisions API, not listed among its chat
    # models: VoiceCut adds it right after the default.
    assert [entry["id"] for entry in listed["models"]] == [
        "google/gemma-4-31b-it",
        "typesafe/jev-1.14",
        "qwen/qwen3.8-27b",
        "mistralai/ministral-8b-2512",
    ]
    assert listed["models"][1]["decision"] is True
    assert listed["models"][0]["default"] is True
    assert listed["models"][0]["detail"] == "262K context"
    # The model list is public, so the key is checked on its own endpoint.
    assert [url for url, _ in calls] == [
        "https://openrouter.ai/api/v1/key",
        "https://openrouter.ai/api/v1/models",
    ]
    assert calls[0][1]["Authorization"] == "Bearer sk-or-test-1234"

    def reject(url: str, headers: dict[str, str]) -> Any:
        raise urllib.error.HTTPError(url, 401, "No auth", None, io.BytesIO())

    monkeypatch.setattr(settings, "_get_json", reject)
    refused = settings.ModelCatalog(env).models("openrouter", refresh=True)
    assert refused["error"]["kind"] == "invalid_key"


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        (
            urllib.error.HTTPError("u", 400, "API key not valid", None, io.BytesIO()),
            "invalid_key",
        ),
        (
            urllib.error.HTTPError("u", 403, "forbidden", None, io.BytesIO()),
            "invalid_key",
        ),
        (urllib.error.HTTPError("u", 503, "down", None, io.BytesIO()), "network"),
        (urllib.error.URLError("offline"), "network"),
    ],
)
def test_listing_failures_explain_themselves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: Exception, kind: str
) -> None:
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=AIza-test-key\n", encoding="utf-8")
    _fake_get(monkeypatch, answer)
    listed = settings.ModelCatalog(env).models("gemini")
    assert listed["error"]["kind"] == kind and listed["models"] == []
    assert listed["default"] == DEFAULT_GEMINI_MODEL


def test_missing_key_and_local_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voicecut import decision_models, ollama
    from voicecut.editor.local import LocalModels

    monkeypatch.setattr(decision_models, "weights_ready", lambda model: False)
    catalog = settings.ModelCatalog(tmp_path / "missing.env")
    assert catalog.models("deepseek")["error"]["kind"] == "missing_key"
    assert catalog.models("ollama")["error"]["kind"] == "unavailable"
    with pytest.raises(settings.SettingsError):
        catalog.models("local")  # the MLX-LM planner is gone
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")
    monkeypatch.delenv("VOICECUT_OLLAMA", raising=False)
    monkeypatch.setattr(ollama, "system_binary", lambda: None)
    local = LocalModels(tmp_path)
    catalog = settings.ModelCatalog(tmp_path / "missing.env", local=local)
    listed = catalog.models("ollama")
    # Without Ollama the recommendations still show, with what to install.
    assert listed["error"]["kind"] == "unavailable"
    assert "ollama.com/download" in listed["error"]["message"]
    assert listed["models"][0]["default"] is True
    assert not any(model["downloaded"] for model in listed["models"])
    entry = next(p for p in settings.planners(tmp_path / "missing.env", local))
    assert entry["id"] == "gemini"
    ollama_entry = next(
        p
        for p in settings.planners(tmp_path / "missing.env", local)
        if p["id"] == "ollama"
    )
    assert ollama_entry["available"] is False and ollama_entry["key"] is None


@needs_ffmpeg
def test_settings_api_never_returns_keys_and_reaches_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("# mine\nGEMINI_API_KEY=AIzaSECRET-abcd\n", encoding="utf-8")
    spawned: list[Path] = []

    class Done:
        stdout = io.StringIO('{"type": "error", "message": "stop"}\n')
        stderr = io.StringIO("")
        pid = 0

        def wait(self) -> int:
            return 1

        def poll(self) -> int:
            return 1

    def spawn(request: Path) -> Done:
        spawned.append(request)
        return Done()

    monkeypatch.setattr(server, "spawn_worker", spawn)
    _fake_get(monkeypatch, GEMINI_PAYLOAD)
    client = TestClient(
        server.create_app(projects_dir=tmp_path / "projects", env_file=env)
    )

    state = client.get("/api/settings")
    assert "SECRET" not in state.text
    gemini = next(item for item in state.json()["planners"] if item["id"] == "gemini")
    assert gemini["key"] == {"set": True, "masked": "••••abcd", "source": "env_file"}
    assert gemini["listens_to_audio"] is True
    assert not next(
        item for item in state.json()["planners"] if item["id"] == "openai"
    )["listens_to_audio"]
    assert (
        client.get("/api/planners/gemini/models").json()["models"][0]["id"]
        == DEFAULT_GEMINI_MODEL
    )

    # A rejected key is not saved; an accepted one replaces only its line.
    _fake_get(monkeypatch, urllib.error.HTTPError("u", 400, "bad", None, io.BytesIO()))
    assert (
        client.put("/api/settings/keys/openai", json={"key": "sk-typo"}).status_code
        == 400
    )
    assert "OPENAI_API_KEY" not in env.read_text(encoding="utf-8")
    _fake_get(monkeypatch, {"data": [{"id": "gpt-5-mini"}]})
    saved = client.put("/api/settings/keys/openai", json={"key": "sk-good-123456"})
    assert saved.status_code == 200 and "sk-good" not in saved.text
    assert (
        env.read_text(encoding="utf-8")
        == "# mine\nGEMINI_API_KEY=AIzaSECRET-abcd\nOPENAI_API_KEY=sk-good-123456\n"
    )

    # Project settings are remembered and become the defaults of new projects.
    first = client.post(
        "/api/projects/import",
        json={"path": str(write_wav(tmp_path / "a.wav", source_audio()))},
    ).json()
    chosen = {
        "planner_backend": "gemini",
        "planner_model": "gemini-3.5-flash",
        "language": "ru",
        "breath_cleanup": None,
    }
    assert (
        client.put(f"/api/projects/{first['id']}/settings", json=chosen).json()[
            "planner_model"
        ]
        == "gemini-3.5-flash"
    )
    second = client.post(
        "/api/projects/import",
        json={"path": str(write_wav(tmp_path / "b.wav", source_audio()[::-1].copy()))},
    ).json()
    assert (
        second["settings"]["planner_model"] == "gemini-3.5-flash"
        and second["settings"]["language"] == "ru"
    )
    assert (
        client.put(
            f"/api/projects/{first['id']}/settings", json={"planner_model": "two words"}
        ).status_code
        == 400
    )

    # The chosen model reaches the pipeline's command line.
    client.post(f"/api/projects/{first['id']}/voicecut", json={})
    argv = json.loads(spawned[-1].read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--planner-model") + 1] == "gemini-3.5-flash"
    assert argv[argv.index("--language") + 1] == "ru"

    # Without a key the run is refused up front.
    response = client.post(
        f"/api/projects/{first['id']}/voicecut", json={"planner_backend": "deepseek"}
    )
    assert (
        response.status_code == 400 and "DeepSeek API key" in response.json()["detail"]
    )


@needs_ffmpeg
def test_a_local_planner_runs_through_ollama(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .ollama_support import FakeOllama

    fake = FakeOllama()
    monkeypatch.setenv("OLLAMA_HOST", fake.url)
    spawned: list[tuple[Path, dict[str, str]]] = []

    class Done:
        stdout = io.StringIO('{"type": "error", "message": "stop"}\n')
        stderr = io.StringIO("")
        pid = 0

        def wait(self) -> int:
            return 1

        def poll(self) -> int:
            return 1

    def spawn(request: Path, environment: dict[str, str] | None = None) -> Done:
        spawned.append((request, environment or {}))
        return Done()

    monkeypatch.setattr(server, "spawn_worker", spawn)
    try:
        client = TestClient(
            server.create_app(
                projects_dir=tmp_path / "projects", env_file=tmp_path / ".env"
            )
        )
        project = client.post(
            "/api/projects/import",
            json={"path": str(write_wav(tmp_path / "a.wav", source_audio()))},
        ).json()
        # A project saved with the MLX-LM planner of earlier versions.
        legacy = client.put(
            f"/api/projects/{project['id']}/settings",
            json={"planner_backend": "qwen", "planner_model": "mlx-community/x"},
        ).json()
        assert legacy["planner_backend"] == "ollama"
        assert legacy["planner_model"] is None

        listed = client.get("/api/planners/ollama/models").json()
        assert any(
            m["id"] == "qwen3:0.6b" and m["downloaded"] for m in listed["models"]
        )
        refused = client.post(f"/api/projects/{project['id']}/voicecut", json={})
        assert refused.status_code == 400 and "first" in refused.json()["detail"]

        chosen = {"planner_backend": "ollama", "planner_model": "qwen3:0.6b"}
        client.post(f"/api/projects/{project['id']}/voicecut", json=chosen)
        request, environment = spawned[-1]
        argv = json.loads(request.read_text(encoding="utf-8"))["argv"]
        assert argv[argv.index("--planner-backend") + 1] == "ollama"
        assert argv[argv.index("--planner-think") + 1] == "on"
        assert environment == {"OLLAMA_HOST": fake.url}

        client.post(
            "/api/local-models/think", json={"model": "qwen3:0.6b", "think": False}
        )
        client.post(f"/api/projects/{project['id']}/voicecut", json=chosen)
        argv = json.loads(spawned[-1][0].read_text(encoding="utf-8"))["argv"]
        assert argv[argv.index("--planner-think") + 1] == "off"
        assert (
            client.post("/api/local-models/pull", json={"model": "a b"}).status_code
            == 400
        )
    finally:
        fake.close()


@needs_ffmpeg
def test_a_local_decision_model_lists_downloads_and_runs_without_ollama(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voicecut import decision_models, ollama
    from voicecut.editor import local as local_module

    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")  # no Ollama at all
    monkeypatch.delenv("VOICECUT_OLLAMA", raising=False)
    monkeypatch.setattr(ollama, "system_binary", lambda: None)
    monkeypatch.setattr(decision_models, "runs_here", lambda model: True)
    have: set[str] = set()
    monkeypatch.setattr(decision_models, "weights_ready", lambda model: model.id in have)
    downloads: list[str] = []
    deleted: list[str] = []

    def fake_run(self: local_module._HubPull) -> None:
        downloads.append(self.model.id)
        have.add(self.model.id)

    monkeypatch.setattr(local_module._HubPull, "run", fake_run)
    monkeypatch.setattr(
        local_module, "_delete_hub_weights", lambda model: (deleted.append(model.id), have.discard(model.id))
    )
    spawned: list[tuple[Path, dict[str, str]]] = []

    class Done:
        stdout = io.StringIO('{"type": "error", "message": "stop"}\n')
        stderr = io.StringIO("")
        pid = 0

        def wait(self) -> int:
            return 1

        def poll(self) -> int:
            return 1

    def spawn(request: Path, environment: dict[str, str] | None = None) -> Done:
        spawned.append((request, environment or {}))
        return Done()

    monkeypatch.setattr(server, "spawn_worker", spawn)
    client = TestClient(
        server.create_app(projects_dir=tmp_path / "projects", env_file=tmp_path / ".env")
    )
    listed = client.get("/api/planners/ollama/models").json()
    ids = [model["id"] for model in listed["models"]]
    # Below the recommended LLMs, still listed without Ollama.
    assert ids.index("jaredpalmer/kev-9b") == ids.index("jaredpalmer/kev-4b") - 1
    assert all(
        ids.index("jaredpalmer/kev-9b") > ids.index(model["id"])
        for model in listed["models"]
        if model["recommended"] and not model.get("decision")
    )
    kev = next(model for model in listed["models"] if model["id"] == "jaredpalmer/kev-9b")
    assert kev["decision"] is True and kev["recommended"] is True
    assert kev["downloaded"] is False and kev["think_options"] is None

    project = client.post(
        "/api/projects/import",
        json={"path": str(write_wav(tmp_path / "a.wav", source_audio()))},
    ).json()
    chosen = {"planner_backend": "ollama", "planner_model": "jaredpalmer/kev-9b"}
    refused = client.post(f"/api/projects/{project['id']}/voicecut", json=chosen)
    assert refused.status_code == 400 and "Download Kev 9B first" in refused.json()["detail"]

    client.post("/api/local-models/pull", json={"model": "jaredpalmer/kev-9b"})
    for _ in range(100):
        listed = client.get("/api/planners/ollama/models").json()
        kev = next(model for model in listed["models"] if model["id"] == "jaredpalmer/kev-9b")
        if kev["downloaded"] and kev["pull"] is None:
            break
        time.sleep(0.02)
    assert downloads == ["jaredpalmer/kev-9b"] and kev["downloaded"] is True

    client.post(f"/api/projects/{project['id']}/voicecut", json=chosen)
    request, environment = spawned[-1]
    argv = json.loads(request.read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--planner-model") + 1] == "jaredpalmer/kev-9b"
    assert "--planner-think" not in argv
    assert environment == {}  # no Ollama for a decision model

    client.post("/api/local-models/delete", json={"model": "jaredpalmer/kev-9b"})
    assert deleted == ["jaredpalmer/kev-9b"]


def test_pause_widths_are_one_setting_for_every_project(tmp_path: Path) -> None:
    from voicecut.editor import pauses

    client = TestClient(
        server.create_app(projects_dir=tmp_path / "projects", env_file=tmp_path / ".env")
    )
    state = client.get("/api/settings").json()
    assert state["pause_widths"] == state["pause_defaults"] == pauses.DEFAULTS
    changed = client.put("/api/settings/pauses", json={"widths": {"sentence": 0.8}}).json()
    assert changed["pause_widths"]["sentence"] == 0.8
    assert changed["pause_widths"]["comma"] == pauses.DEFAULTS["comma"]
    # A second editor over the same projects sees the same widths.
    again = TestClient(
        server.create_app(projects_dir=tmp_path / "projects", env_file=tmp_path / ".env")
    )
    assert again.get("/api/settings").json()["pause_widths"]["sentence"] == 0.8
    for bad in ({"sentence": -1}, {"sentence": 9}, {"breath": 1}, {"sentence": True}):
        assert client.put("/api/settings/pauses", json={"widths": bad}).status_code == 400
    reset = client.put("/api/settings/pauses", json={"reset": True}).json()
    assert reset["pause_widths"] == pauses.DEFAULTS
