from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from voicecut import ollama
from voicecut.planner_backends import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OPENAI_BASE_URL,
    DEFAULT_OPENROUTER_MODEL,
    OPENROUTER_BASE_URL,
    PLANNER_BACKENDS,
    OllamaPlannerBackend,
    OpenAICompatiblePlannerBackend,
    PermanentPlannerError,
    default_model_for_backend,
    preflight_planner_backend,
    resolve_planner_base_url,
    sanitize_planner_base_url,
    think_setting,
)
from voicecut.take_planner import build_parser as build_take_parser


class RecordingCompletions:
    def __init__(self, content: str = '{"ok":true}') -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class RecordingResponses:
    def __init__(self, content: str = '{"ok":true}') -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.content)


class RecordingClient:
    def __init__(self, content: str = '{"ok":true}') -> None:
        self.completions = RecordingCompletions(content)
        self.chat = SimpleNamespace(completions=self.completions)
        self.responses = RecordingResponses(content)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_openai_uses_strict_json_schema() -> None:
    client = RecordingClient()
    backend = OpenAICompatiblePlannerBackend(
        provider="openai",
        model="test-openai",
        api_key="secret",
        strict_json_schema=True,
        client=client,
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }

    assert (
        backend.generate(
            "classify this",
            response_schema=schema,
            request_id="ignored",
        )
        == '{"ok":true}'
    )
    call = client.responses.calls[0]
    assert call["model"] == "test-openai"
    assert call["text"] == {
        "format": {
            "type": "json_schema",
            "name": "voicecut_planner_response",
            "strict": True,
            "schema": schema,
        },
    }
    assert call["input"][1]["content"] == "classify this"  # type: ignore[index]
    assert "temperature" not in call
    assert "max_tokens" not in call
    assert call["max_output_tokens"] == 8192

    backend.close()
    assert client.closed is True


def test_deepseek_uses_json_object_and_includes_schema_in_prompt() -> None:
    client = RecordingClient()
    backend = OpenAICompatiblePlannerBackend(
        provider="deepseek",
        model=DEFAULT_DEEPSEEK_MODEL,
        api_key="secret",
        base_url=DEFAULT_DEEPSEEK_BASE_URL,
        strict_json_schema=False,
        client=client,
    )
    schema = {
        "type": "object",
        "required": ["transitions"],
        "properties": {"transitions": {"type": "array"}},
    }

    backend.generate(
        "classify transitions",
        response_schema=schema,
        request_id="pause-1",
    )
    call = client.completions.calls[0]
    assert call["model"] == "deepseek-v4-flash"
    assert call["response_format"] == {"type": "json_object"}
    user_prompt = call["messages"][1]["content"]  # type: ignore[index]
    assert "classify transitions" in user_prompt
    assert '"transitions"' in user_prompt


class FakeOllama:
    """An OllamaClient that answers from a script."""

    def __init__(
        self,
        answers: list[ollama.ChatResult],
        *,
        capabilities: tuple[str, ...] = ("completion", "thinking"),
        context: int | None = 40960,
        think_values: list[object] | None = None,
    ) -> None:
        self.answers = answers
        self.capabilities = capabilities
        self.context = context
        self.think_values = think_values
        self.calls: list[dict[str, object]] = []

    def show(self, model: str) -> dict[str, object]:
        if model == "missing:1b":
            raise ollama.OllamaError("model 'missing:1b' not found", status=404)
        info = {"qwen3.context_length": self.context} if self.context else {}
        shown: dict[str, object] = {
            "capabilities": list(self.capabilities),
            "model_info": info,
        }
        if self.think_values is not None:
            shown["thinking"] = {"values": self.think_values}
        return shown

    def chat(self, **kwargs: object) -> ollama.ChatResult:
        self.calls.append(kwargs)
        return self.answers.pop(0)


def _answer(content: str, reason: str = "stop") -> ollama.ChatResult:
    return ollama.ChatResult(
        content=content,
        thinking="",
        done_reason=reason,
        prompt_tokens=10,
        output_tokens=10,
    )


def test_ollama_backend_constrains_the_answer_and_lets_the_model_reason() -> None:
    client = FakeOllama([_answer('{"ok":true}')])
    backend = OllamaPlannerBackend(
        model="qwen3:0.6b",
        max_output_tokens=65536,
        client=client,  # type: ignore[arg-type]
    )
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    raw = backend.generate("classify this", response_schema=schema, request_id="w1")
    assert raw == '{"ok":true}'
    call = client.calls[0]
    assert call["format"] == schema
    assert call["think"] is True
    assert call["model"] == "qwen3:0.6b"
    options = call["options"]
    assert options["num_ctx"] == ollama.CONTEXT_TOKENS  # type: ignore[index]
    # An explicit budget: Ollama would otherwise generate past a full context.
    assert 0 < options["num_predict"] < options["num_ctx"]  # type: ignore[index]


def test_ollama_backend_answers_without_reasoning_when_reasoning_fills_the_budget() -> (
    None
):
    client = FakeOllama([_answer("", "length"), _answer('{"ok":true}')])
    backend = OllamaPlannerBackend(model="qwen3:0.6b", client=client)  # type: ignore[arg-type]
    assert backend.generate("p", response_schema={}, request_id="w1") == '{"ok":true}'
    assert [call["think"] for call in client.calls] == [True, False]


def test_ollama_backend_follows_the_model_and_the_setting_on_reasoning() -> None:
    plain = FakeOllama([_answer("{}")], capabilities=("completion",))
    OllamaPlannerBackend(model="gemma3:4b", think=True, client=plain).generate(  # type: ignore[arg-type]
        "p", response_schema={}, request_id="w"
    )
    # A model that cannot reason refuses the parameter.
    assert plain.calls[0]["think"] is None
    reasoning = FakeOllama([_answer("{}")])
    OllamaPlannerBackend(model="qwen3:8b", think=False, client=reasoning).generate(  # type: ignore[arg-type]
        "p", response_schema={}, request_id="w"
    )
    assert reasoning.calls[0]["think"] is False


def test_ollama_reasoning_levels_fit_what_the_model_takes() -> None:
    levels = FakeOllama(
        [_answer("", "length"), _answer("{}")], think_values=["low", "medium", "high"]
    )
    OllamaPlannerBackend(model="gpt-oss:20b", think="high", client=levels).generate(  # type: ignore[arg-type]
        "p", response_schema={}, request_id="w"
    )
    # A model that always reasons falls back to its lowest level, not "off".
    assert [call["think"] for call in levels.calls] == ["high", "low"]
    plain = FakeOllama([_answer("{}")], think_values=[False, True])
    OllamaPlannerBackend(model="qwen3:8b", think="low", client=plain).generate(  # type: ignore[arg-type]
        "p", response_schema={}, request_id="w"
    )
    assert plain.calls[0]["think"] is True
    assert think_setting("low") == "low"
    assert think_setting("off") is False and think_setting("auto") is None


def test_ollama_context_grows_for_a_long_prompt_within_the_model() -> None:
    backend = OllamaPlannerBackend(
        model="qwen3:8b",
        max_output_tokens=65536,
        client=FakeOllama([], context=131072),  # type: ignore[arg-type]
    )
    options = backend.options("x" * 100_000)
    assert options["num_ctx"] > ollama.CONTEXT_TOKENS
    assert options["num_ctx"] % 8192 == 0
    assert options["num_predict"] >= 8192
    small = OllamaPlannerBackend(
        model="qwen3:0.6b",
        client=FakeOllama([], context=16384),  # type: ignore[arg-type]
    )
    assert small.options("x" * 100_000)["num_ctx"] == 16384


def test_ollama_missing_model_is_not_retried() -> None:
    backend = OllamaPlannerBackend(model="missing:1b", client=FakeOllama([]))  # type: ignore[arg-type]
    with pytest.raises(PermanentPlannerError, match="ollama pull missing:1b"):
        backend.generate("p", response_schema={}, request_id="w")


def test_model_defaults_are_provider_specific() -> None:
    assert default_model_for_backend("ollama") == DEFAULT_OLLAMA_MODEL
    assert default_model_for_backend("deepseek") == "deepseek-v4-flash"
    for legacy in ("local", "qwen", "gemma"):
        assert legacy not in PLANNER_BACKENDS
    with pytest.raises(ValueError, match="unsupported planner backend"):
        default_model_for_backend("unknown")


def test_planner_base_url_is_resolved_and_canonicalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "HTTPS://API.Example.TEST:443/v1/")
    assert (
        resolve_planner_base_url(
            provider="openai",
            env_file=tmp_path / "missing.env",
            base_url=None,
        )
        == "https://api.example.test/v1"
    )
    monkeypatch.delenv("OPENAI_BASE_URL")
    assert (
        resolve_planner_base_url(
            provider="openai",
            env_file=tmp_path / "missing.env",
            base_url=None,
        )
        == DEFAULT_OPENAI_BASE_URL
    )
    assert (
        resolve_planner_base_url(
            provider="deepseek",
            env_file=tmp_path / "missing.env",
            base_url=None,
        )
        == DEFAULT_DEEPSEEK_BASE_URL
    )
    assert (
        resolve_planner_base_url(
            provider="openrouter",
            env_file=tmp_path / "missing.env",
            base_url=None,
        )
        == OPENROUTER_BASE_URL
    )


def test_openrouter_plans_through_chat_completions_with_gemma(tmp_path: Path) -> None:
    from voicecut.planner_backends import create_planner_backend

    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=sk-or-test-1234\n", encoding="utf-8")
    backend = create_planner_backend(provider="openrouter", model=None, env_file=env)
    assert isinstance(backend, OpenAICompatiblePlannerBackend)
    assert backend.model == DEFAULT_OPENROUTER_MODEL == "google/gemma-4-31b-it"
    assert backend.base_url == OPENROUTER_BASE_URL
    # Chat Completions with a JSON object, the form every OpenRouter model takes.
    assert backend.strict_json_schema is False
    assert "openrouter" in PLANNER_BACKENDS


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            "https://token@example.test/v1",
            "must not contain credentials",
        ),
        (
            "https://example.test/v1?api_key=secret",
            "must not contain query parameters",
        ),
        (
            "https://example.test/v1#secret",
            "must not contain a URL fragment",
        ),
    ],
)
def test_planner_base_url_rejects_secret_bearing_forms(
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        sanitize_planner_base_url(value)


def test_cloud_preflight_reports_missing_key_before_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is missing"):
        preflight_planner_backend(
            provider="gemini",
            env_file=tmp_path / "missing.env",
            base_url=None,
            api_key_env=None,
        )


def test_ollama_preflight_says_what_to_install_or_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ollama.OllamaClient, "version", lambda self, **_: None)
    monkeypatch.setattr(ollama.OllamaServer, "installed", staticmethod(lambda: None))
    with pytest.raises(RuntimeError, match="install it from"):
        preflight_planner_backend(
            provider="ollama",
            env_file=tmp_path / "missing.env",
            base_url=None,
            api_key_env=None,
            model="qwen3:8b",
        )
    monkeypatch.setattr(ollama.OllamaClient, "version", lambda self, **_: "0.40.8")
    monkeypatch.setattr(
        ollama.OllamaClient, "models", lambda self: [{"model": "qwen3:0.6b"}]
    )
    with pytest.raises(RuntimeError, match="ollama pull qwen3:8b"):
        preflight_planner_backend(
            provider="ollama",
            env_file=tmp_path / "missing.env",
            base_url=None,
            api_key_env=None,
            model="qwen3:8b",
        )
    configuration = preflight_planner_backend(
        provider="ollama",
        env_file=tmp_path / "missing.env",
        base_url=None,
        api_key_env=None,
        model="qwen3:0.6b",
    )
    assert configuration.api_key_env is None


def test_take_planner_cli_exposes_provider_neutral_options(tmp_path: Path) -> None:
    args = build_take_parser().parse_args(
        [
            "--transcript",
            "words.json",
            "--output-dir",
            str(tmp_path / "out"),
            "--planner-backend",
            "deepseek",
            "--planner-model",
            "deepseek-v4-flash",
            "--planner-base-url",
            "https://example.test/v1",
            "--planner-api-key-env",
            "CUSTOM_KEY",
            "--planner-think",
            "off",
        ]
    )
    assert args.planner_backend == "deepseek"
    assert args.planner_model == "deepseek-v4-flash"
    assert args.planner_base_url == "https://example.test/v1"
    assert args.planner_api_key_env == "CUSTOM_KEY"
    assert args.planner_think == "off"
