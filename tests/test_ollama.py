from __future__ import annotations

import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from voicecut import local_models, ollama
from voicecut.editor.local import LocalModelError, LocalModels

from .ollama_support import FakeOllama


@pytest.fixture
def fake() -> Iterator[FakeOllama]:
    server = FakeOllama()
    yield server
    server.close()


def test_host_url_reads_ollama_host_like_ollama() -> None:
    assert ollama.host_url(None) == "http://127.0.0.1:11434"
    assert ollama.host_url("0.0.0.0") == "http://127.0.0.1:11434"
    assert ollama.host_url("127.0.0.1:47630") == "http://127.0.0.1:47630"
    assert ollama.host_url("http://example.test:8080/") == "http://example.test:8080"


def test_canonical_name_adds_the_latest_tag() -> None:
    assert ollama.canonical_name("gpt-oss") == "gpt-oss:latest"
    assert ollama.canonical_name("qwen3:8b") == "qwen3:8b"
    assert ollama.canonical_name("hf.co/user/repo") == "hf.co/user/repo:latest"


def test_chat_streams_reasoning_apart_from_the_answer(fake: FakeOllama) -> None:
    progress: list[str] = []
    result = ollama.OllamaClient(fake.url).chat(
        model="qwen3:0.6b",
        messages=[{"role": "user", "content": "hi"}],
        format={"type": "object"},
        think=True,
        options={"num_ctx": 8192, "num_predict": 4096},
        on_progress=lambda phase, tokens: progress.append(phase),
    )
    assert result.content == '{"ok":true}'
    assert result.thinking == "Compare the takes."
    assert result.done_reason == "stop"
    assert progress == ["thinking", "answering", "answering"]
    path, body = fake.requests[-1]
    assert path == "/api/chat"
    assert body["think"] is True
    assert body["format"] == {"type": "object"}
    assert body["stream"] is True


def test_errors_carry_ollamas_message_and_status(fake: FakeOllama) -> None:
    with pytest.raises(ollama.OllamaError, match="not found") as raised:
        ollama.OllamaClient(fake.url).show("missing:1b")
    assert raised.value.status == 404
    assert ollama.OllamaClient("http://127.0.0.1:9").version() is None


def test_a_running_ollama_is_used_as_it_is(
    fake: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", fake.url)
    server = ollama.OllamaServer()
    assert server.ensure() == fake.url
    assert server.status()["source"] == "system"


def test_an_installed_ollama_comes_before_voicecuts_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    own = tmp_path / "ollama"
    own.write_text("")
    monkeypatch.setenv("VOICECUT_OLLAMA", str(own))
    monkeypatch.setattr(ollama, "system_binary", lambda: None)
    assert ollama.OllamaServer.installed() == (own, "voicecut")
    system = tmp_path / "system-ollama"
    monkeypatch.setattr(ollama, "system_binary", lambda: system)
    assert ollama.OllamaServer.installed() == (system, "system")


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX script stands in")
def test_the_editor_starts_voicecuts_own_ollama_and_stops_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "bin" / "ollama"
    binary.parent.mkdir()
    binary.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """
            import json, os, sys
            from http.server import BaseHTTPRequestHandler, HTTPServer
            assert sys.argv[1:] == ["serve"]
            host, port = os.environ["OLLAMA_HOST"].split(":")
            seen = {k: os.environ.get(k) for k in
                    ("OLLAMA_MODELS", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_NO_CLOUD", "HOME")}
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    body = json.dumps({"version": "0.40.8", "env": seen}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            HTTPServer((host, int(port)), Handler).serve_forever()
            """
        )
    )
    binary.chmod(0o755)
    models = tmp_path / "ollama" / "models"
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")
    monkeypatch.setenv("VOICECUT_OLLAMA", str(binary))
    monkeypatch.setenv("VOICECUT_OLLAMA_MODELS", str(models))
    monkeypatch.setattr(ollama, "system_binary", lambda: None)
    server = ollama.OllamaServer(log_path=tmp_path / "ollama.log")
    url = server.ensure()
    try:
        assert url != "http://127.0.0.1:9"
        assert server.status()["source"] == "voicecut"
        assert server.ensure() == url  # started once
        env = ollama.OllamaClient(url)._json("GET", "/api/version")["env"]
        assert env["OLLAMA_MODELS"] == str(models)
        assert env["OLLAMA_CONTEXT_LENGTH"] == str(ollama.CONTEXT_TOKENS)
        assert env["OLLAMA_NO_CLOUD"] == "1"
        assert env["HOME"] == str(models.parent / "home")
    finally:
        server.stop()
    assert ollama.OllamaClient(url).version() is None


def test_without_any_ollama_the_editor_says_what_to_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")
    monkeypatch.delenv("VOICECUT_OLLAMA", raising=False)
    monkeypatch.setattr(ollama, "system_binary", lambda: None)
    with pytest.raises(ollama.OllamaUnavailable, match="ollama.com/download"):
        ollama.OllamaServer().ensure()
    status = ollama.OllamaServer().status()
    assert status["available"] is False


def test_recommendations_pick_mlx_tags_only_on_big_apple_silicon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_models, "uses_mlx", lambda: False)
    assert local_models.default_model() == local_models.RECOMMENDED[0].gguf
    monkeypatch.setattr(local_models, "uses_mlx", lambda: True)
    entries = local_models.recommended()
    assert entries[0]["id"] == local_models.RECOMMENDED[0].mlx
    assert entries[0]["default"] is True
    # A model without an MLX version keeps its GGUF one.
    plain = [item for item in local_models.RECOMMENDED if item.mlx is None]
    if plain:
        assert any(entry["id"] == plain[0].gguf for entry in entries)


def _wait(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_the_model_list_downloads_and_deletes_models(
    fake: FakeOllama, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", fake.url)
    local = LocalModels(tmp_path)
    listing = local.listing()
    assert listing["error"] is None
    assert listing["ollama"]["version"] == "0.40.8"
    ids = [model["id"] for model in listing["models"]]
    recommended = [entry["id"] for entry in local_models.recommended()]
    assert ids[: len(recommended)] == recommended  # recommendations first
    tiny = next(model for model in listing["models"] if model["id"] == "qwen3:0.6b")
    assert tiny["downloaded"] is True
    assert tiny["can_think"] is True and tiny["think"] is True

    fake.pull_gate.clear()
    listing = local.pull("gemma3:1b")
    pulling = next(model for model in listing["models"] if model["id"] == "gemma3:1b")
    assert pulling["downloaded"] is False and pulling["pull"] is not None
    assert local.downloading()
    with pytest.raises(LocalModelError, match="still downloading"):
        local.ready("gemma3:1b")
    fake.pull_gate.set()
    _wait(lambda: not local.downloading())
    done = next(
        model for model in local.listing()["models"] if model["id"] == "gemma3:1b"
    )
    assert done["downloaded"] is True and done["pull"] is None
    assert local.ready("gemma3:1b") == fake.url

    local.delete("gemma3:1b")
    with pytest.raises(LocalModelError, match="Download gemma3:1b first"):
        local.ready("gemma3:1b")


def test_a_failed_download_says_why(
    fake: FakeOllama, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", fake.url)
    local = LocalModels(tmp_path)
    local.pull("nothing:1b")
    _wait(lambda: not local.downloading())
    entry = next(
        model for model in local.listing()["models"] if model["id"] == "nothing:1b"
    )
    assert entry["pull"]["error"] == "Ollama has no model called nothing:1b"
    local.cancel("nothing:1b")  # dismisses it
    assert all(model["id"] != "nothing:1b" for model in local.listing()["models"])
    with pytest.raises(LocalModelError):
        local.pull("not a name")


def test_reasoning_is_a_setting_of_each_model(
    fake: FakeOllama, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", fake.url)
    local = LocalModels(tmp_path)
    assert local.think("qwen3:0.6b") is True  # not recommended: it reasons
    local.set_think("qwen3:0.6b", False)
    assert LocalModels(tmp_path).think("qwen3:0.6b") is False
    assert LocalModels(tmp_path).think_argument("qwen3:0.6b") == "off"
    local.set_think("qwen3:0.6b", "low")
    tiny = next(m for m in local.listing()["models"] if m["id"] == "qwen3:0.6b")
    # It takes only on and off: a level means on.
    assert tiny["think_options"] == [False, True] and tiny["think"] is True
    assert local.think_argument("qwen3:0.6b") == "low"
    with pytest.raises(LocalModelError):
        local.set_think("qwen3:0.6b", "yes")
    # Recommendations start from the setting that planned best.
    for item in local_models.RECOMMENDED:
        assert local.think(item.gguf) == item.think
    listing = local.listing()
    assert listing["memory"]["needed_gb"] == local_models.MEMORY_NEEDED_GB
    levels = [m for m in listing["models"] if isinstance(m["think"], str)]
    assert all(m["think_options"][0] is False for m in levels)


def test_fit_think_maps_a_setting_onto_what_a_model_takes() -> None:
    assert local_models.fit_think("low", [False, True]) is True
    assert local_models.fit_think(True, ["low", "medium", "high"]) == "medium"
    assert local_models.fit_think(False, ["low", "medium", "high"]) is None
    assert local_models.fit_think("high", None) == "high"
