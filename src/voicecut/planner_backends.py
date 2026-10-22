#!/usr/bin/env python3
"""Provider-neutral structured-JSON backends for VoiceCut planners.

The take planner talks to every provider through this small transport
layer.  Validation, grounding and the retry policy remain in the planner; a
backend has only one responsibility: return the model's raw textual JSON
response.  Cloud planners are Gemini, OpenAI and DeepSeek; planners on this
computer run through Ollama (:mod:`voicecut.ollama`) on every system.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from . import local_models, ollama

DEFAULT_GEMINI_MODEL = "gemini-3.1-pro-preview"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_OLLAMA_MODEL = local_models.default_model()
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

PLANNER_BACKENDS = ("gemini", "openai", "deepseek", "ollama")
# Earlier local planners (MLX-LM on Apple Silicon); settings that name one
# now mean Ollama.
LEGACY_LOCAL_BACKENDS = frozenset({"local", "qwen", "gemma"})
API_KEY_ENV_BY_BACKEND = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
DEFAULT_MODEL_BY_BACKEND = {
    "gemini": DEFAULT_GEMINI_MODEL,
    "openai": DEFAULT_OPENAI_MODEL,
    "deepseek": DEFAULT_DEEPSEEK_MODEL,
    "ollama": DEFAULT_OLLAMA_MODEL,
}
THINK_CHOICES = ("auto", "on", "off", *local_models.THINK_LEVELS)
# A prompt's length in tokens, estimated from its characters on the safe
# side: word IDs ("[123]word") make the window prompts dense.
CHARACTERS_PER_TOKEN = 2.5
# The least room a local answer gets, reasoning included.
MIN_OUTPUT_TOKENS = 8192
PROGRESS_SECONDS = 30.0

NARRATION_SYSTEM_INSTRUCTION = (
    "You are a precise source-grounded narration editor. "
    "Return only the structured JSON response requested by the schema."
)


@dataclass(frozen=True)
class PlannerRuntimeConfiguration:
    """Validated, non-secret planner configuration used by the pipeline."""

    provider: str
    base_url: str | None
    api_key_env: str | None


class PermanentPlannerError(RuntimeError):
    """A planner failure that retrying the same request cannot fix."""


@dataclass(frozen=True)
class MediaPart:
    """Inline media attached to a planner request (audio for listening)."""

    data: bytes
    mime_type: str


class PlannerBackend(Protocol):
    """Minimal transport contract shared by all semantic planners."""

    backend_name: str
    model: str

    def generate(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any],
        request_id: str,
    ) -> str:
        """Return the raw model response without interpreting it."""

    def close(self) -> None:
        """Release network or local-model resources."""


def _dotenv_value(env_file: Path, key: str) -> str | None:
    """Read one secret with environment variables taking precedence."""

    environment_value = os.environ.get(key)
    if isinstance(environment_value, str) and environment_value.strip():
        return environment_value.strip()
    if not env_file.is_file():
        return None
    try:
        from dotenv import dotenv_values
    except ImportError as error:
        raise RuntimeError(
            "Reading API keys from .env requires `python-dotenv`; install "
            "`voicecut[cloud]` or export the key in the environment"
        ) from error
    value = dotenv_values(env_file).get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _require_api_key(
    *,
    provider: str,
    env_file: Path,
    api_key_env: str | None,
) -> str:
    key_name = api_key_env or API_KEY_ENV_BY_BACKEND[provider]
    api_key = _dotenv_value(env_file, key_name)
    if api_key is None:
        raise RuntimeError(f"{key_name} is missing from the environment and {env_file}")
    return api_key


def sanitize_planner_base_url(value: str) -> str:
    """Validate and canonicalize a credential-free HTTP(S) API base URL."""

    raw = value.strip()
    if not raw:
        raise ValueError("planner base URL cannot be empty")
    if any(character.isspace() or ord(character) < 32 for character in raw):
        raise ValueError("planner base URL cannot contain whitespace or control bytes")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"invalid planner base URL: {error}") from None
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("planner base URL must use http or https")
    if not parsed.hostname:
        raise ValueError("planner base URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "planner base URL must not contain credentials; use "
            "--planner-api-key-env instead"
        )
    if parsed.query:
        raise ValueError(
            "planner base URL must not contain query parameters because they "
            "may expose credentials"
        )
    if parsed.fragment:
        raise ValueError("planner base URL must not contain a URL fragment")

    hostname = parsed.hostname.casefold()
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = (parsed.scheme.casefold() == "https" and port == 443) or (
        parsed.scheme.casefold() == "http" and port == 80
    )
    if port is not None and not default_port:
        rendered_host += f":{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            rendered_host,
            path,
            "",
            "",
        )
    )


def resolve_planner_base_url(
    *,
    provider: str,
    env_file: Path,
    base_url: str | None,
) -> str | None:
    """Resolve the effective endpoint without returning any credential."""

    if provider not in PLANNER_BACKENDS:
        raise ValueError(f"unsupported planner backend: {provider}")
    if provider not in {"openai", "deepseek"}:
        if base_url is not None:
            raise ValueError(
                "--planner-base-url is supported only for openai and deepseek"
            )
        return None

    selected = base_url
    if selected is None:
        selected = _dotenv_value(env_file, f"{provider.upper()}_BASE_URL")
    if selected is None:
        selected = (
            DEFAULT_OPENAI_BASE_URL
            if provider == "openai"
            else DEFAULT_DEEPSEEK_BASE_URL
        )
    return sanitize_planner_base_url(selected)


def preflight_planner_backend(
    *,
    provider: str,
    env_file: Path,
    base_url: str | None,
    api_key_env: str | None,
    model: str | None = None,
) -> PlannerRuntimeConfiguration:
    """Fail before media work when planner credentials or SDKs are unavailable,
    or a local model is not downloaded."""

    effective_base_url = resolve_planner_base_url(
        provider=provider,
        env_file=env_file,
        base_url=base_url,
    )
    if provider in API_KEY_ENV_BY_BACKEND:
        selected_key_env = api_key_env or API_KEY_ENV_BY_BACKEND[provider]
        _require_api_key(
            provider=provider,
            env_file=env_file,
            api_key_env=api_key_env,
        )
        if provider == "gemini":
            try:
                from google import genai  # noqa: F401
                from google.genai import types  # noqa: F401
            except ImportError as error:
                raise RuntimeError(
                    "Gemini planner support is unavailable; reinstall with "
                    "`pip install -e '.[cloud]'`"
                ) from error
        else:
            try:
                import openai  # noqa: F401
            except ImportError as error:
                raise RuntimeError(
                    f"{provider} planner support is unavailable; reinstall with "
                    "`pip install -e '.[cloud]'`"
                ) from error
        return PlannerRuntimeConfiguration(
            provider=provider,
            base_url=effective_base_url,
            api_key_env=selected_key_env,
        )

    check_ollama_model(model or default_model_for_backend(provider))
    return PlannerRuntimeConfiguration(
        provider=provider,
        base_url=None,
        api_key_env=None,
    )


def check_ollama_model(model: str) -> None:
    """Raise with what to do when Ollama or ``model`` is missing.

    A stopped Ollama passes when one is installed: the planner starts it.
    """

    client = ollama.OllamaClient(ollama.configured_url())
    if client.version() is None:
        if ollama.OllamaServer.installed() is None:
            raise RuntimeError(
                "local models need Ollama: install it from "
                f"{ollama.INSTALL_PAGE}, or choose a cloud planner"
            )
        return
    try:
        names = {
            ollama.canonical_name(str(entry.get("model") or entry.get("name")))
            for entry in client.models()
        }
    except ollama.OllamaError as error:
        raise RuntimeError(str(error)) from None
    if ollama.canonical_name(model) not in names:
        raise RuntimeError(
            f"the local model {model} is not downloaded: download it from the "
            f"planner's model list in the editor, or run `ollama pull {model}`"
        )


def _schema_prompt(prompt: str, response_schema: dict[str, Any]) -> str:
    return (
        prompt
        + "\n\nReturn JSON only, matching this JSON schema exactly:\n"
        + json.dumps(response_schema, ensure_ascii=False, separators=(",", ":"))
    )


def _response_text(response: Any, *, provider: str) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as error:
        raise RuntimeError(
            f"{provider} returned an invalid chat-completion response"
        ) from error
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for block in content:
            text = (
                block.get("text")
                if isinstance(block, dict)
                else getattr(block, "text", None)
            )
            if isinstance(text, str):
                pieces.append(text)
        combined = "".join(pieces)
        if combined.strip():
            return combined
    raise RuntimeError(f"{provider} returned no textual JSON response")


class GeminiPlannerBackend:
    """Google Gen AI structured-output transport."""

    backend_name = "gemini"
    supports_audio = True

    def __init__(
        self,
        *,
        model: str = DEFAULT_GEMINI_MODEL,
        api_key: str,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        system_instruction: str = NARRATION_SYSTEM_INSTRUCTION,
        temperature: float | None = 0.0,
        client: Any | None = None,
        types_module: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Gemini API key is empty")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if client is None or types_module is None:
            try:
                from google import genai
                from google.genai import types
            except ImportError as error:
                raise RuntimeError(
                    "Gemini requires `pip install -e '.[cloud]'`"
                ) from error
            client = genai.Client(api_key=api_key)
            types_module = types
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.system_instruction = system_instruction
        self.temperature = temperature
        self._types = types_module
        self._client = client

    @classmethod
    def from_env(
        cls,
        *,
        env_file: Path,
        model: str = DEFAULT_GEMINI_MODEL,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        system_instruction: str = NARRATION_SYSTEM_INSTRUCTION,
        api_key_env: str | None = None,
        temperature: float | None = 0.0,
    ) -> "GeminiPlannerBackend":
        return cls(
            model=model,
            api_key=_require_api_key(
                provider="gemini",
                env_file=env_file,
                api_key_env=api_key_env,
            ),
            max_output_tokens=max_output_tokens,
            system_instruction=system_instruction,
            temperature=temperature,
        )

    def generate(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any],
        request_id: str,
        media: Sequence[MediaPart] = (),
    ) -> str:
        del request_id
        config_values: dict[str, Any] = {
            "system_instruction": self.system_instruction,
            "max_output_tokens": self.max_output_tokens,
        }
        # Gemini 3 models are tuned for their default temperature; zero can
        # make long reasoning loop.  ``None`` keeps the provider default.
        if self.temperature is not None:
            config_values["temperature"] = self.temperature
        config_fields = getattr(
            self._types.GenerateContentConfig,
            "model_fields",
            {},
        )
        if "response_format" in config_fields:
            config_values["response_format"] = {
                "text": {
                    "mime_type": "application/json",
                    "schema": response_schema,
                }
            }
        else:
            config_values["response_mime_type"] = "application/json"
            config_values["response_json_schema"] = response_schema
        contents: Any = prompt
        if media:
            contents = [
                prompt,
                *(
                    self._types.Part.from_bytes(
                        data=part.data, mime_type=part.mime_type
                    )
                    for part in media
                ),
            ]
        response = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=self._types.GenerateContentConfig(**config_values),
        )
        raw = response.text
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError("Gemini returned no textual JSON response")
        return raw

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


class OpenAICompatiblePlannerBackend:
    """OpenAI Responses transport plus DeepSeek Chat Completions."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        system_instruction: str = NARRATION_SYSTEM_INSTRUCTION,
        base_url: str | None = None,
        strict_json_schema: bool,
        client: Any | None = None,
    ) -> None:
        if provider not in {"openai", "deepseek"}:
            raise ValueError(f"unsupported OpenAI-compatible provider: {provider}")
        if not api_key.strip():
            raise ValueError(f"{provider} API key is empty")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as error:
                raise RuntimeError(
                    f"{provider} requires `pip install -e '.[cloud]'`"
                ) from error
            client_arguments: dict[str, Any] = {"api_key": api_key}
            if base_url:
                client_arguments["base_url"] = base_url
            client = OpenAI(**client_arguments)
        self.backend_name = provider
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.system_instruction = system_instruction
        self.base_url = base_url
        self.strict_json_schema = strict_json_schema
        self._client = client

    def generate(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any],
        request_id: str,
    ) -> str:
        del request_id
        if self.strict_json_schema:
            response = self._client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": self.system_instruction},
                    {"role": "user", "content": prompt},
                ],
                max_output_tokens=self.max_output_tokens,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "voicecut_planner_response",
                        "strict": True,
                        "schema": response_schema,
                    }
                },
            )
            raw = getattr(response, "output_text", None)
            if not isinstance(raw, str) or not raw.strip():
                raise RuntimeError("openai returned no textual JSON response")
            return raw

        response_format = {"type": "json_object"}
        request_prompt = _schema_prompt(prompt, response_schema)
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_instruction},
                {"role": "user", "content": request_prompt},
            ],
            temperature=0.0,
            max_tokens=self.max_output_tokens,
            response_format=response_format,
        )
        return _response_text(response, provider=self.backend_name)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


class OllamaPlannerBackend:
    """A model that Ollama serves on this computer.

    The answer is constrained to the response schema (Ollama's structured
    outputs), and a reasoning model may reason first: its reasoning arrives
    apart from the answer.  Should the reasoning fill the whole output budget,
    the request is asked again without it, so an answer still comes.  Each
    request gets the context it needs (at least :data:`ollama.CONTEXT_TOKENS`)
    and an explicit output budget, because Ollama otherwise keeps generating
    past a full context.
    """

    backend_name = "ollama"

    def __init__(
        self,
        *,
        model: str = DEFAULT_OLLAMA_MODEL,
        think: bool | str | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        system_instruction: str = NARRATION_SYSTEM_INSTRUCTION,
        context_tokens: int = ollama.CONTEXT_TOKENS,
        client: ollama.OllamaClient | None = None,
    ) -> None:
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self.model = model
        self.think = think
        self.max_output_tokens = max_output_tokens
        self.system_instruction = system_instruction
        self.context_tokens = context_tokens
        self._client = client
        self._server: ollama.OllamaServer | None = None
        self._model_info: (
            tuple[set[str], int | None, list[bool | str] | None] | None
        ) = None

    def _connect(self) -> ollama.OllamaClient:
        if self._client is None:
            url = ollama.configured_url()
            if ollama.OllamaClient(url).version() is None:
                # A command-line run without a running Ollama starts one.
                self._server = ollama.OllamaServer()
                url = self._server.ensure()
            self._client = ollama.OllamaClient(url)
        return self._client

    def _info(self) -> tuple[set[str], int | None, list[bool | str] | None]:
        """The model's capabilities, longest context and ``think`` values."""

        if self._model_info is None:
            try:
                shown = self._connect().show(self.model)
            except ollama.OllamaError as error:
                raise _planner_error(error, self.model) from None
            capabilities = {str(value) for value in shown.get("capabilities") or []}
            context = next(
                (
                    int(value)
                    for key, value in (shown.get("model_info") or {}).items()
                    if key.endswith(".context_length") and isinstance(value, int)
                ),
                None,
            )
            self._model_info = (capabilities, context, ollama.think_choices(shown))
        return self._model_info

    def options(self, prompt: str) -> dict[str, int]:
        """The context and output budget of one request."""

        _, model_context, _ = self._info()
        estimate = math.ceil(
            len(self.system_instruction + prompt) / CHARACTERS_PER_TOKEN
        )
        context = self.context_tokens
        if estimate + MIN_OUTPUT_TOKENS > context:
            context = math.ceil((estimate + MIN_OUTPUT_TOKENS) / 8192) * 8192
        if model_context:
            context = min(context, model_context)
        output = min(self.max_output_tokens, max(context - estimate, 1024))
        return {"num_ctx": context, "num_predict": output}

    def generate(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any],
        request_id: str,
    ) -> str:
        capabilities, _, choices = self._info()
        # A model without reasoning refuses the parameter; one with levels
        # may refuse a plain "on", one that always reasons "off".
        think = None
        if "thinking" in capabilities:
            wanted = True if self.think is None else self.think
            think = local_models.fit_think(wanted, choices)
        options = self.options(prompt)
        result = self._chat(prompt, response_schema, think, options, request_id)
        least = local_models.fit_think(False, choices)
        if least is None and choices and "low" in choices:
            least = "low"
        if (
            think not in (None, False, least)
            and result.done_reason == "length"
            and not result.content.strip()
        ):
            print(
                f"{request_id}: the reasoning filled the output budget; asking "
                "again with as little reasoning as the model allows",
                file=sys.stderr,
                flush=True,
            )
            result = self._chat(prompt, response_schema, least, options, request_id)
        if result.done_reason == "length":
            print(
                f"{request_id}: the answer reached the output budget "
                f"({options['num_predict']} tokens) and may be cut off",
                file=sys.stderr,
                flush=True,
            )
        return result.content

    def _chat(
        self,
        prompt: str,
        response_schema: dict[str, Any],
        think: bool | str | None,
        options: dict[str, int],
        request_id: str,
    ) -> ollama.ChatResult:
        started = time.monotonic()
        reported = [started]

        def progress(phase: str, tokens: int) -> None:
            now = time.monotonic()
            if now - reported[0] >= PROGRESS_SECONDS:
                reported[0] = now
                print(
                    f"{request_id}: {self.model} {phase}, {tokens} tokens in "
                    f"{now - started:.0f}s",
                    file=sys.stderr,
                    flush=True,
                )

        try:
            return self._connect().chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_instruction},
                    {"role": "user", "content": prompt},
                ],
                format=response_schema,
                think=think,
                options=options,
                on_progress=progress,
            )
        except ollama.OllamaError as error:
            if error.status is None:
                # Ollama is gone: the next try finds or starts one again.
                self._client = None
            raise _planner_error(error, self.model) from None

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.stop()


def _planner_error(error: ollama.OllamaError, model: str) -> Exception:
    """An Ollama error, as permanent when asking again cannot help."""

    message = str(error)
    if isinstance(error, ollama.OllamaUnavailable):
        return PermanentPlannerError(message)
    if error.status == 404 or "not found" in message.casefold():
        return PermanentPlannerError(
            f"the local model {model} is not downloaded: download it from the "
            f"planner's model list in the editor, or run `ollama pull {model}`"
        )
    if (error.status is not None and 400 <= error.status < 500) or (
        "memory" in message.casefold()
    ):
        return PermanentPlannerError(f"Ollama cannot run {model}: {message}")
    return error


def default_model_for_backend(provider: str) -> str:
    try:
        return DEFAULT_MODEL_BY_BACKEND[provider]
    except KeyError as error:
        raise ValueError(f"unsupported planner backend: {provider}") from error


def create_planner_backend(
    *,
    provider: str,
    model: str | None,
    env_file: Path = Path(".env"),
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    system_instruction: str = NARRATION_SYSTEM_INSTRUCTION,
    base_url: str | None = None,
    api_key_env: str | None = None,
    think: bool | str | None = None,
    gemini_temperature: float | None = 0.0,
) -> PlannerBackend:
    """Create one planner transport from provider-neutral configuration."""

    selected_model = model or default_model_for_backend(provider)
    selected_base_url = resolve_planner_base_url(
        provider=provider,
        env_file=env_file,
        base_url=base_url,
    )
    if provider == "gemini":
        return GeminiPlannerBackend.from_env(
            env_file=env_file,
            model=selected_model,
            max_output_tokens=max_output_tokens,
            system_instruction=system_instruction,
            api_key_env=api_key_env,
            temperature=gemini_temperature,
        )
    if provider in {"openai", "deepseek"}:
        return OpenAICompatiblePlannerBackend(
            provider=provider,
            model=selected_model,
            api_key=_require_api_key(
                provider=provider,
                env_file=env_file,
                api_key_env=api_key_env,
            ),
            max_output_tokens=max_output_tokens,
            system_instruction=system_instruction,
            base_url=selected_base_url,
            strict_json_schema=provider == "openai",
        )
    if provider == "ollama":
        return OllamaPlannerBackend(
            model=selected_model,
            think=think,
            max_output_tokens=max_output_tokens,
            system_instruction=system_instruction,
        )
    raise ValueError(f"unsupported planner backend: {provider}")


def add_planner_backend_arguments(
    parser: argparse.ArgumentParser,
    *,
    default_backend: str = "gemini",
) -> None:
    """Add the same model-selection surface to a VoiceCut subcommand."""

    parser.add_argument(
        "--planner-backend",
        choices=PLANNER_BACKENDS,
        default=default_backend,
        help=(
            "LLM transport: a cloud provider, or ollama for a model that "
            "runs on this computer."
        ),
    )
    parser.add_argument(
        "--planner-model",
        help="Provider model name, or an Ollama model (e.g. gemma4:31b).",
    )
    parser.add_argument(
        "--planner-base-url",
        help=(
            "Optional OpenAI-compatible API base URL. DeepSeek defaults to "
            "https://api.deepseek.com."
        ),
    )
    parser.add_argument(
        "--planner-api-key-env",
        help=(
            "Environment/.env variable containing the API key. Defaults to "
            "the standard variable for the selected provider."
        ),
    )
    parser.add_argument(
        "--planner-think",
        choices=THINK_CHOICES,
        default="auto",
        help=(
            "How an Ollama model reasons before it answers: on, off, or a "
            "level for models that take one (auto: the model's default)."
        ),
    )


def think_setting(value: str) -> bool | str | None:
    """``--planner-think`` as the backend's ``think`` argument."""

    if value in local_models.THINK_LEVELS:
        return value
    return {"on": True, "off": False}.get(value)
