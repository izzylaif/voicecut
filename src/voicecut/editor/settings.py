"""Planner choice, API keys and live model lists for the editor's settings.

* API keys live where the pipeline already reads them: the dotenv file the
  editor passes to ``voicecut --env-file`` (the repository ``.env`` by
  default).  Saving a key rewrites only that key's line; every other line,
  comment and the file's permissions are kept.  A key is never sent back to
  the browser in full and never logged.
* Model lists come from each provider's models API, called with the saved
  key, and are filtered to text models that can serve as the take planner.
  Failing to list models doubles as a key check: the UI says whether a key is
  missing, rejected, or the provider is unreachable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..planner_backends import (
    API_KEY_ENV_BY_BACKEND,
    DEFAULT_GEMMA_MODEL,
    DEFAULT_LOCAL_PYTHON,
    DEFAULT_MODEL_BY_BACKEND,
    DEFAULT_QWEN_MODEL,
    resolve_planner_base_url,
)

GEMINI_MODELS_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
)
LIST_TIMEOUT_SECONDS = 12
MODEL_CACHE_SECONDS = 600
MAX_KEY_LENGTH = 512
KEY_PAGES = {
    "gemini": "https://aistudio.google.com/apikey",
    "openai": "https://platform.openai.com/api-keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
}
PLANNER_LABELS = {
    "gemini": "Gemini",
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "local": "Local (MLX)",
}
# Gemini models that generate text but are not general planners.
GEMINI_EXCLUDED = re.compile(
    r"tts|image|banana|lyria|transcribe|robotics|computer-use|antigravity|"
    r"deep-research|customtools|embedding|imagen|veo|aqa|native-audio|live|gemma",
)
OPENAI_INCLUDED = re.compile(r"^(gpt-|o\d|chatgpt-)")
OPENAI_EXCLUDED = re.compile(
    r"audio|realtime|tts|transcribe|image|search|embedding|moderation|instruct|"
    r"dall-e|whisper|babbage|davinci",
)
LOCAL_EXCLUDED = re.compile(r"whisper|parakeet|wav2vec|asr|crisper", re.IGNORECASE)


class SettingsError(ValueError):
    """A settings request is invalid."""


# ---------------------------------------------------------------------------
# API keys in the dotenv file

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _parse_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value.split(" #", 1)[0].strip()


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _LINE.match(line)
        if match:
            values[match.group(1)] = _parse_value(line[match.end() :])
    return values


def key_value(env_file: Path, name: str) -> tuple[str | None, str | None]:
    """The key the pipeline would use and where it comes from.

    Like the pipeline, a variable of the editor's own environment wins over
    the dotenv file.
    """

    environment = os.environ.get(name, "").strip()
    if environment:
        return environment, "environment"
    stored = read_env_file(env_file).get(name, "").strip()
    return (stored, "env_file") if stored else (None, None)


def mask(value: str) -> str:
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def _quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.\-:/+=]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_key(env_file: Path, name: str, value: str | None) -> None:
    """Set (or with ``None``/empty remove) one variable, keeping the rest."""

    if value is not None:
        value = value.strip()
        if len(value) > MAX_KEY_LENGTH or any(
            ord(c) < 32 or c.isspace() for c in value
        ):
            raise SettingsError("the key must be one line without spaces")
    lines = (
        env_file.read_text(encoding="utf-8").splitlines() if env_file.is_file() else []
    )
    kept: list[str] = []
    replaced = False
    for line in lines:
        match = _LINE.match(line)
        if match and match.group(1) == name:
            if value and not replaced:
                kept.append(f"{name}={_quote(value)}")
            replaced = True
            continue
        kept.append(line)
    if value and not replaced:
        kept.append(f"{name}={_quote(value)}")
    mode = stat.S_IMODE(env_file.stat().st_mode) if env_file.exists() else 0o600
    env_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=env_file.parent, prefix=".env.", delete=False
    ) as handle:
        handle.write("\n".join(kept) + ("\n" if kept else ""))
        temporary = Path(handle.name)
    os.chmod(temporary, mode)
    temporary.replace(env_file)


# ---------------------------------------------------------------------------
# Planners and their models


def audio_listening_backends() -> frozenset[str]:
    from ..full_pipeline import AUDIO_LISTENING_BACKENDS

    return AUDIO_LISTENING_BACKENDS


def planners(env_file: Path) -> list[dict[str, Any]]:
    """The planner providers the editor offers, with their key status."""

    listening = audio_listening_backends()
    entries = []
    for backend, label in PLANNER_LABELS.items():
        key_env = API_KEY_ENV_BY_BACKEND.get(backend)
        entry: dict[str, Any] = {
            "id": backend,
            "label": label,
            "listens_to_audio": backend in listening,
            "default_model": DEFAULT_MODEL_BY_BACKEND[backend],
            "key_env": key_env,
            "key_page": KEY_PAGES.get(backend),
        }
        if key_env:
            value, source = key_value(env_file, key_env)
            entry["key"] = {
                "set": value is not None,
                "masked": mask(value) if value else None,
                "source": source,
            }
            entry["available"] = value is not None
        else:
            entry["key"] = None
            entry["available"] = DEFAULT_LOCAL_PYTHON.is_file()
        entries.append(entry)
    return entries


def _get_json(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", **headers}
    )
    with urllib.request.urlopen(request, timeout=LIST_TIMEOUT_SECONDS) as response:
        return json.load(response)


def _version(model_id: str) -> tuple[float, ...]:
    numbers = re.findall(r"\d+(?:\.\d+)?", model_id)
    return tuple(float(number) for number in numbers[:2]) or (0.0,)


def _tier(model_id: str) -> int:
    """Pro before flash before lite/mini/nano within one version."""

    if "lite" in model_id or "nano" in model_id:
        return 2
    if "pro" in model_id:
        return 0
    if "flash" in model_id:
        return 1
    return 3 if "mini" in model_id else 4


def _gemini_models(key: str) -> list[dict[str, Any]]:
    payload = _get_json(GEMINI_MODELS_URL, {"x-goog-api-key": key})
    models = []
    for model in payload.get("models") or []:
        model_id = str(model.get("name", "")).removeprefix("models/")
        if (
            "generateContent" not in (model.get("supportedGenerationMethods") or [])
            or GEMINI_EXCLUDED.search(model_id)
            or int(model.get("inputTokenLimit") or 0) < 131_072
        ):
            continue
        models.append(
            {
                "id": model_id,
                "label": model.get("displayName") or model_id,
                "detail": f"{int(model.get('outputTokenLimit') or 0) // 1024}K output tokens",
                "alias": model_id.endswith("-latest"),
            }
        )
    return models


def _openai_compatible_models(
    backend: str, key: str, env_file: Path
) -> list[dict[str, Any]]:
    base = resolve_planner_base_url(provider=backend, env_file=env_file, base_url=None)
    payload = _get_json(f"{base}/models", {"Authorization": f"Bearer {key}"})
    models = []
    for model in payload.get("data") or []:
        model_id = str(model.get("id", ""))
        if backend == "openai" and (
            not OPENAI_INCLUDED.search(model_id) or OPENAI_EXCLUDED.search(model_id)
        ):
            continue
        models.append(
            {"id": model_id, "label": model_id, "detail": None, "alias": False}
        )
    return models


def _local_models() -> list[dict[str, Any]]:
    hub = Path(
        os.environ.get("HF_HUB_CACHE")
        or Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
        / "hub"
    )
    found: dict[str, dict[str, Any]] = {
        DEFAULT_QWEN_MODEL: {
            "id": DEFAULT_QWEN_MODEL,
            "label": DEFAULT_QWEN_MODEL,
            "detail": "Qwen",
            "alias": False,
        },
        DEFAULT_GEMMA_MODEL: {
            "id": DEFAULT_GEMMA_MODEL,
            "label": DEFAULT_GEMMA_MODEL,
            "detail": "Gemma",
            "alias": False,
        },
    }
    for entry in sorted(hub.glob("models--*")) if hub.is_dir() else []:
        name = entry.name.removeprefix("models--").replace("--", "/")
        if "mlx" in name.lower() and not LOCAL_EXCLUDED.search(name):
            found.setdefault(
                name, {"id": name, "label": name, "detail": None, "alias": False}
            )
    for model in found.values():
        cached = (hub / f"models--{model['id'].replace('/', '--')}").is_dir()
        model["detail"] = " · ".join(
            part
            for part in (
                model["detail"],
                "downloaded" if cached else "downloads on first use",
            )
            if part
        )
    return list(found.values())


def _sorted(models: list[dict[str, Any]], default: str) -> list[dict[str, Any]]:
    """Newest first; within a version pro before flash before lite."""

    unique = {model["id"]: model for model in models}
    ordered = sorted(
        unique.values(),
        key=lambda model: (
            model["id"] != default,
            model["alias"],
            tuple(-part for part in _version(model["id"])),
            _tier(model["id"]),
            model["id"],
        ),
    )
    for model in ordered:
        model["default"] = model["id"] == default
    return ordered


class ModelCatalog:
    """Live model lists per provider, cached for a few minutes per key."""

    def __init__(self, env_file: Path) -> None:
        self.env_file = env_file
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    def models(
        self, backend: str, *, refresh: bool = False, key: str | None = None
    ) -> dict[str, Any]:
        if backend not in PLANNER_LABELS:
            raise SettingsError(f"unknown planner {backend!r}")
        default = DEFAULT_MODEL_BY_BACKEND[backend]
        if backend == "local":
            if not DEFAULT_LOCAL_PYTHON.is_file():
                return self._failure(
                    default,
                    "unavailable",
                    "Local models need VoiceCut's MLX environment (.venv-mlx); "
                    "run scripts/install.sh",
                )
            return {
                "models": _sorted(_local_models(), default),
                "default": default,
                "error": None,
            }
        key_env = API_KEY_ENV_BY_BACKEND[backend]
        if key is None:
            key, _ = key_value(self.env_file, key_env)
        if not key:
            label = PLANNER_LABELS[backend]
            return self._failure(
                default,
                "missing_key",
                f"Add your {label} API key in Settings ({key_env})",
            )
        fingerprint = hashlib.sha256(key.encode()).hexdigest()[:16]
        with self._lock:
            cached = self._cache.get((backend, fingerprint))
        if (
            cached
            and not refresh
            and time.monotonic() - cached[0] < MODEL_CACHE_SECONDS
        ):
            return cached[1]
        try:
            listed = (
                _gemini_models(key)
                if backend == "gemini"
                else _openai_compatible_models(backend, key, self.env_file)
            )
        except urllib.error.HTTPError as error:
            label = PLANNER_LABELS[backend]
            if error.code in (400, 401, 403):
                return self._failure(
                    default, "invalid_key", f"{label} rejected the API key"
                )
            if error.code == 429:
                return self._failure(
                    default,
                    "network",
                    f"{label} is rate limiting requests; try again shortly",
                )
            return self._failure(
                default, "network", f"{label} answered HTTP {error.code}"
            )
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            reason = getattr(error, "reason", error)
            return self._failure(
                default, "network", f"Cannot reach {PLANNER_LABELS[backend]}: {reason}"
            )
        result = {"models": _sorted(listed, default), "default": default, "error": None}
        with self._lock:
            self._cache[(backend, fingerprint)] = (time.monotonic(), result)
        return result

    @staticmethod
    def _failure(default: str, kind: str, message: str) -> dict[str, Any]:
        return {
            "models": [],
            "default": default,
            "error": {"kind": kind, "message": message},
        }


# ---------------------------------------------------------------------------
# Remembered Voice Cut settings


def load_preferences(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_preferences(path: Path, preferences: dict[str, Any]) -> None:
    from .projects import write_json_atomic

    write_json_atomic(path, preferences)
