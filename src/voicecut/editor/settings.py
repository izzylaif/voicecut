"""Planner choice, API keys and live model lists for the editor's settings.

* API keys live in a key store.  The browser editor keeps them where the
  pipeline already reads them: the dotenv file it passes to ``voicecut
  --env-file`` (the repository ``.env`` by default); saving a key rewrites
  only that key's line, keeping every other line, comment and the file's
  permissions.  The desktop app keeps them in the system's credential store
  (the macOS login keychain, the Windows Credential Manager, the Secret
  Service keyring on Linux) and hands the planner's key to a Voice Cut run in
  its environment.  Either way a key is never sent back to the browser in
  full and never logged.
* Model lists come from each provider's models API, called with the saved
  key, and are filtered to text models that can serve as the take planner.
  Failing to list models doubles as a key check: the UI says whether a key is
  missing, rejected, or the provider is unreachable.  Local models are
  Ollama's, with VoiceCut's recommendations first (:mod:`.local`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from ..planner_backends import (
    API_KEY_ENV_BY_BACKEND,
    DEFAULT_MODEL_BY_BACKEND,
    resolve_planner_base_url,
)
from .local import LocalModels

GEMINI_MODELS_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
)
LIST_TIMEOUT_SECONDS = 12
KEYCHAIN_SERVICE = "VoiceCut"
SECURITY_TOOL = "/usr/bin/security"
# ``security`` exits with this status when the keychain has no such item.
SECURITY_ITEM_NOT_FOUND = 44
MODEL_CACHE_SECONDS = 600
MAX_KEY_LENGTH = 512
KEY_PAGES = {
    "gemini": "https://aistudio.google.com/apikey",
    "openai": "https://platform.openai.com/api-keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "openrouter": "https://openrouter.ai/settings/keys",
}
PLANNER_LABELS = {
    "gemini": "Gemini",
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "openrouter": "OpenRouter",
    "ollama": "Local (Ollama)",
}
# Gemini models that generate text but are not general planners.
GEMINI_EXCLUDED = re.compile(
    r"tts|image|banana|lyria|transcribe|robotics|computer-use|antigravity|"
    r"deep-research|customtools|embedding|imagen|veo|aqa|native-audio|live|gemma",
)
# OpenRouter variants that are not ordinary chat endpoints ("-:batch") or
# share a small free quota (":free"); either can still be typed in.
OPENROUTER_EXCLUDED = re.compile(r":(batch|free)$")
OPENROUTER_MIN_CONTEXT = 131_072
OPENAI_INCLUDED = re.compile(r"^(gpt-|o\d|chatgpt-)")
OPENAI_EXCLUDED = re.compile(
    r"audio|realtime|tts|transcribe|image|search|embedding|moderation|instruct|"
    r"dall-e|whisper|babbage|davinci",
)


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


def key_value(keys: KeyStore | Path, name: str) -> tuple[str | None, str | None]:
    """The key the pipeline would use and where it comes from.

    Like the pipeline, a variable of the editor's own environment wins over
    the key store (a dotenv file when ``keys`` is a path).
    """

    environment = os.environ.get(name, "").strip()
    if environment:
        return environment, "environment"
    store = EnvFileKeys(keys) if isinstance(keys, Path) else keys
    stored = store.get(name)
    return (stored, store.kind) if stored else (None, None)


def mask(value: str) -> str:
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def _quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.\-:/+=]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_key(env_file: Path, name: str, value: str | None) -> None:
    """Set (or with ``None``/empty remove) one variable, keeping the rest."""

    if value is not None:
        value = _check_key(value)
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


def _check_key(value: str) -> str:
    value = value.strip()
    if len(value) > MAX_KEY_LENGTH or any(ord(c) < 32 or c.isspace() for c in value):
        raise SettingsError("the key must be one line without spaces")
    return value


class KeyStore(Protocol):
    """Where the editor keeps the planners' API keys."""

    kind: str

    @property
    def location(self) -> str: ...

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str | None) -> None: ...


class EnvFileKeys:
    """Keys in a dotenv file, the one ``voicecut --env-file`` reads."""

    kind = "env_file"

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def location(self) -> str:
        return str(self.path)

    def get(self, name: str) -> str | None:
        return read_env_file(self.path).get(name, "").strip() or None

    def set(self, name: str, value: str | None) -> None:
        save_key(self.path, name, value)


class KeychainKeys:
    """Keys as generic passwords of the macOS login keychain.

    ``security`` reads and writes them; a new key reaches it on standard
    input (``security -i``), never on a command line other processes can see.
    """

    kind = "keychain"

    def __init__(
        self, service: str = KEYCHAIN_SERVICE, *, tool: str = SECURITY_TOOL
    ) -> None:
        self.service = service
        self.tool = tool

    @property
    def location(self) -> str:
        return f"the macOS Keychain (“{self.service}” items)"

    def _run(
        self, arguments: list[str], *, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.tool, *arguments],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def get(self, name: str) -> str | None:
        try:
            result = self._run(
                ["find-generic-password", "-s", self.service, "-a", name, "-w"]
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def set(self, name: str, value: str | None) -> None:
        if not value:
            result = self._run(
                ["delete-generic-password", "-s", self.service, "-a", name]
            )
            if result.returncode not in (0, SECURITY_ITEM_NOT_FOUND):
                raise SettingsError(f"the Keychain refused: {result.stderr.strip()}")
            return
        value = _check_key(value)
        if '"' in value or "\\" in value:
            raise SettingsError("the key must not contain quotes or backslashes")
        command = (
            f'add-generic-password -U -s "{self.service}" -a "{name}" '
            f'-l "{self.service}: {name}" -w "{value}"\n'
        )
        result = self._run(["-i"], stdin=command)
        if result.returncode != 0:
            raise SettingsError(f"the Keychain refused: {result.stderr.strip()}")


class CredentialStoreKeys:
    """Keys in the Windows Credential Manager or a Secret Service keyring.

    ``keyring`` talks to whichever the system has (on Linux: GNOME Keyring,
    KWallet or KeePassXC through the Secret Service API).
    """

    kind = "credential_store"

    def __init__(self, service: str = KEYCHAIN_SERVICE) -> None:
        import keyring

        self.service = service
        self.backend = keyring.get_keyring()

    @property
    def location(self) -> str:
        if sys.platform == "win32":
            return "the Windows Credential Manager"
        return "your keyring (Secret Service)"

    def get(self, name: str) -> str | None:
        try:
            value = self.backend.get_password(self.service, name)
        except Exception:  # noqa: BLE001 - a locked or broken keyring
            return None
        return value.strip() or None if value else None

    def set(self, name: str, value: str | None) -> None:
        import keyring.errors

        try:
            if value:
                self.backend.set_password(self.service, name, _check_key(value))
            else:
                with contextlib.suppress(keyring.errors.PasswordDeleteError):
                    self.backend.delete_password(self.service, name)
        except keyring.errors.KeyringError as error:
            raise SettingsError(f"the keyring refused: {error}") from error


def system_keys(fallback: Path) -> KeyStore:
    """The credential store of this system, or the dotenv file ``fallback``.

    Linux without a Secret Service (a bare window manager, a container) falls
    back to a file only its owner can read.
    """

    if sys.platform == "darwin":
        return KeychainKeys()
    with contextlib.suppress(Exception):
        import keyring
        from keyring.backends import fail

        if not isinstance(keyring.get_keyring(), fail.Keyring):
            return CredentialStoreKeys()
    return EnvFileKeys(fallback)


# ---------------------------------------------------------------------------
# Planners and their models


def audio_listening_backends() -> frozenset[str]:
    from ..full_pipeline import AUDIO_LISTENING_BACKENDS

    return AUDIO_LISTENING_BACKENDS


def planners(
    keys: KeyStore | Path, local: LocalModels | None = None
) -> list[dict[str, Any]]:
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
            value, source = key_value(keys, key_env)
            entry["key"] = {
                "set": value is not None,
                "masked": mask(value) if value else None,
                "source": source,
            }
            entry["available"] = value is not None
        else:
            entry["key"] = None
            entry["available"] = local is not None and local.available()
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


def _openrouter_models(key: str, env_file: Path) -> list[dict[str, Any]]:
    """Text models with room for a planning window, newest first.  The model
    list needs no key, so the key is checked on its own endpoint first."""

    base = resolve_planner_base_url(
        provider="openrouter", env_file=env_file, base_url=None
    )
    headers = {"Authorization": f"Bearer {key}"}
    _get_json(f"{base}/key", headers)
    payload = _get_json(f"{base}/models", headers)
    models = []
    for model in payload.get("data") or []:
        model_id = str(model.get("id", ""))
        architecture = model.get("architecture") or {}
        context = int(model.get("context_length") or 0)
        if (
            OPENROUTER_EXCLUDED.search(model_id)
            or context < OPENROUTER_MIN_CONTEXT
            or "text" not in (architecture.get("input_modalities") or ["text"])
            or "text" not in (architecture.get("output_modalities") or ["text"])
        ):
            continue
        models.append(
            {
                "id": model_id,
                "label": str(model.get("name") or model_id),
                "detail": f"{context // 1000}K context",
                "alias": False,
                "created": int(model.get("created") or 0),
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


def _sorted(models: list[dict[str, Any]], default: str) -> list[dict[str, Any]]:
    """Newest first (by release date where the provider gives one, else by
    version); within a version pro before flash before lite."""

    unique = {model["id"]: model for model in models}
    ordered = sorted(
        unique.values(),
        key=lambda model: (
            model["id"] != default,
            model["alias"],
            -model.get("created", 0),
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

    def __init__(
        self,
        env_file: Path,
        keys: KeyStore | None = None,
        local: LocalModels | None = None,
    ) -> None:
        self.env_file = env_file
        self.keys = keys or EnvFileKeys(env_file)
        self.local = local
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    def models(
        self, backend: str, *, refresh: bool = False, key: str | None = None
    ) -> dict[str, Any]:
        if backend not in PLANNER_LABELS:
            raise SettingsError(f"unknown planner {backend!r}")
        default = DEFAULT_MODEL_BY_BACKEND[backend]
        if backend == "ollama":
            if self.local is None:
                return self._failure(
                    default, "unavailable", "Local models are not available here"
                )
            return self.local.listing()
        key_env = API_KEY_ENV_BY_BACKEND[backend]
        if key is None:
            key, _ = key_value(self.keys, key_env)
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
            if backend == "gemini":
                listed = _gemini_models(key)
            elif backend == "openrouter":
                listed = _openrouter_models(key, self.env_file)
            else:
                listed = _openai_compatible_models(backend, key, self.env_file)
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
