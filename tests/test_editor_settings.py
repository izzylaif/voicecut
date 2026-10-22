"""API keys, planner choice and live model lists (voicecut.editor.settings)."""

from __future__ import annotations

import io
import json
import os
import stat
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
    assert ids == [
        DEFAULT_GEMINI_MODEL,
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
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
    catalog = settings.ModelCatalog(tmp_path / "missing.env")
    assert catalog.models("deepseek")["error"]["kind"] == "missing_key"
    hub = tmp_path / "hub"
    for name in (
        "models--mlx-community--Llama-4-8B-4bit",
        "models--mlx-community--whisper-large-v3-turbo",
    ):
        (hub / name).mkdir(parents=True)
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "DEFAULT_LOCAL_PYTHON", python)
    local = catalog.models("local")
    ids = [model["id"] for model in local["models"]]
    assert "mlx-community/Llama-4-8B-4bit" in ids and not any(
        "whisper" in model for model in ids
    )
    assert local["models"][0]["default"] is True
    monkeypatch.setattr(settings, "DEFAULT_LOCAL_PYTHON", tmp_path / "missing-python")
    assert catalog.models("local")["error"]["kind"] == "unavailable"


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
