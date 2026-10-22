"""Local planner models for the editor: Ollama, its models and their downloads.

The editor owns one :class:`~voicecut.ollama.OllamaServer`: a running Ollama
when there is one, else one it starts (and stops when it exits).  The model
list puts VoiceCut's recommendations first, then every other model Ollama
has; a model downloads in the background (``/api/pull``) while the list
reports its progress.  Whether a model reasons before answering is a setting
of that model, kept in ``local_models.json`` next to the editor's projects.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from pathlib import Path
from typing import Any

from .. import local_models, ollama

SETTINGS_FILE = "local_models.json"
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")


class LocalModelError(ValueError):
    """A local model request is invalid or cannot be served."""


def check_name(model: Any) -> str:
    if not isinstance(model, str) or not MODEL_NAME.fullmatch(model.strip()):
        raise LocalModelError("a model name such as qwen3.6:27b")
    return model.strip()


class _Pull:
    def __init__(self) -> None:
        self.status = "starting"
        self.layers: dict[str, tuple[int, int]] = {}
        self.error: str | None = None
        self.done = False
        self.cancelled = False

    def update(self, chunk: dict[str, Any]) -> None:
        self.status = str(chunk.get("status") or self.status)
        digest = chunk.get("digest")
        if digest and chunk.get("total"):
            self.layers[str(digest)] = (
                int(chunk.get("completed") or 0),
                int(chunk["total"]),
            )

    def view(self) -> dict[str, Any]:
        completed = sum(done for done, _ in self.layers.values())
        total = sum(size for _, size in self.layers.values())
        return {
            "status": self.status,
            "completed": completed,
            "total": total,
            "fraction": completed / total if total else None,
            "error": self.error,
            "done": self.done,
        }


class LocalModels:
    """Ollama and its models, as the editor's planner list shows them."""

    def __init__(self, root: Path, *, log_path: Path | None = None) -> None:
        self.settings_path = root / SETTINGS_FILE
        self.server = ollama.OllamaServer(log_path=log_path)
        self._lock = threading.Lock()
        self._pulls: dict[str, _Pull] = {}

    # -- per-model settings -----------------------------------------------------
    def _settings(self) -> dict[str, Any]:
        try:
            value = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def think(self, model: str) -> bool:
        """Whether ``model`` reasons before answering in a Voice Cut."""

        saved = (self._settings().get("think") or {}).get(ollama.canonical_name(model))
        if isinstance(saved, bool):
            return saved
        recommended = local_models.recommended_think(model)
        return True if recommended is None else recommended

    def set_think(self, model: Any, value: Any) -> dict[str, Any]:
        model = check_name(model)
        if not isinstance(value, bool):
            raise LocalModelError("think must be true or false")
        from .projects import write_json_atomic

        with self._lock:
            settings = self._settings()
            think = dict(settings.get("think") or {})
            think[ollama.canonical_name(model)] = value
            write_json_atomic(self.settings_path, {**settings, "think": think})
        return self.listing()

    # -- the list ---------------------------------------------------------------
    def available(self) -> bool:
        """Whether a Voice Cut could use Ollama (without starting it)."""

        return self.server.installed() is not None or self.server.running() is not None

    def _client(self) -> ollama.OllamaClient:
        try:
            return ollama.OllamaClient(self.server.ensure())
        except ollama.OllamaError as error:
            raise LocalModelError(str(error)) from None

    def listing(self) -> dict[str, Any]:
        default = local_models.default_model()
        status = self.server.status()
        installed: dict[str, dict[str, Any]] = {}
        error = None
        if status["available"]:
            try:
                for entry in ollama.OllamaClient(status["url"]).models():
                    name = ollama.canonical_name(
                        str(entry.get("model") or entry.get("name"))
                    )
                    installed[name] = entry
            except ollama.OllamaError as failure:
                error = {"kind": "network", "message": str(failure)}
        else:
            error = {"kind": "unavailable", "message": status["message"]}
        with self._lock:
            pulls = {name: pull.view() for name, pull in self._pulls.items()}
        models = []
        seen = set()
        for entry in local_models.recommended():
            name = ollama.canonical_name(str(entry["id"]))
            seen.add(name)
            have = installed.get(name)
            models.append(
                self._entry(
                    str(entry["id"]),
                    label=str(entry["label"]),
                    have=have,
                    size_gb=float(entry["size_gb"]),  # type: ignore[arg-type]
                    engine=str(entry["engine"]),
                    recommended=True,
                    pull=pulls.get(name),
                    default=str(entry["id"]) == default,
                )
            )
        for name, have in sorted(installed.items()):
            if name in seen:
                continue
            seen.add(name)
            details = have.get("details") or {}
            engine = {"safetensors": "MLX", "gguf": "GGUF"}.get(
                str(details.get("format") or ""), None
            )
            model_id = name.removesuffix(":latest")
            models.append(
                self._entry(
                    model_id,
                    label=model_id,
                    have=have,
                    size_gb=float(have.get("size") or 0) / 1e9 or None,
                    engine=engine,
                    recommended=False,
                    pull=pulls.get(name),
                    default=False,
                )
            )
        # Downloads of models not listed yet (a name typed in).
        for name, pull in pulls.items():
            if name not in seen:
                model_id = name.removesuffix(":latest")
                models.append(
                    self._entry(
                        model_id,
                        label=model_id,
                        have=None,
                        size_gb=None,
                        engine=None,
                        recommended=False,
                        pull=pull,
                        default=False,
                    )
                )
        return {
            "models": models,
            "default": default,
            "error": error,
            "ollama": status,
        }

    def _entry(
        self,
        model: str,
        *,
        label: str,
        have: dict[str, Any] | None,
        size_gb: float | None,
        engine: str | None,
        recommended: bool,
        pull: dict[str, Any] | None,
        default: bool,
    ) -> dict[str, Any]:
        # Known from Ollama once downloaded; before, only for recommendations.
        can_think: bool | None = None
        if have is not None:
            can_think = "thinking" in (have.get("capabilities") or [])
        elif local_models.recommended_think(model) is not None:
            can_think = True
        parts = [part for part in (engine,) if part]
        if size_gb:
            parts.append(f"{size_gb:.1f} GB")
        parts.append("downloaded" if have is not None else "not downloaded")
        return {
            "id": model,
            "label": label,
            "detail": " · ".join(parts),
            "alias": False,
            "default": default,
            "downloaded": have is not None,
            "size_gb": round(size_gb, 1) if size_gb else None,
            "engine": engine,
            "recommended": recommended,
            "can_think": can_think,
            "think": self.think(model) if can_think is not False else False,
            "pull": pull,
        }

    # -- downloads --------------------------------------------------------------
    def pull(self, model: Any) -> dict[str, Any]:
        model = check_name(model)
        name = ollama.canonical_name(model)
        client = self._client()
        with self._lock:
            current = self._pulls.get(name)
            if current is not None and not current.done:
                return self.listing()
            pull = _Pull()
            self._pulls[name] = pull

        def run() -> None:
            try:
                client.pull(
                    model, progress=pull.update, cancelled=lambda: pull.cancelled
                )
            except ollama.OllamaError as error:
                pull.error = (
                    "cancelled" if pull.cancelled else _pull_error(str(error), model)
                )
            except (OSError, ValueError) as error:
                pull.error = str(error) or type(error).__name__
            else:
                pull.status = "success"
            finally:
                pull.done = True
                if pull.error is None or pull.cancelled:
                    # The list shows the model itself from now on.
                    with self._lock:
                        if self._pulls.get(name) is pull:
                            del self._pulls[name]

        threading.Thread(target=run, daemon=True, name=f"pull {model}").start()
        return self.listing()

    def cancel(self, model: Any) -> dict[str, Any]:
        name = ollama.canonical_name(check_name(model))
        with self._lock:
            pull = self._pulls.get(name)
            if pull is not None:
                if pull.done:
                    del self._pulls[name]
                else:
                    pull.cancelled = True
        return self.listing()

    def delete(self, model: Any) -> dict[str, Any]:
        model = check_name(model)
        with self._lock:
            pull = self._pulls.get(ollama.canonical_name(model))
            if pull is not None and not pull.done:
                raise LocalModelError(f"{model} is still downloading")
        try:
            self._client().delete(model)
        except ollama.OllamaError as error:
            raise LocalModelError(str(error)) from None
        return self.listing()

    def downloading(self) -> bool:
        with self._lock:
            return any(not pull.done for pull in self._pulls.values())

    # -- Voice Cut ---------------------------------------------------------------
    def ready(self, model: str) -> str:
        """The URL a Voice Cut with ``model`` uses; raises with what to do."""

        client = self._client()
        try:
            names = {
                ollama.canonical_name(str(entry.get("model") or entry.get("name")))
                for entry in client.models()
            }
        except ollama.OllamaError as error:
            raise LocalModelError(str(error)) from None
        name = ollama.canonical_name(model)
        if name not in names:
            with self._lock:
                pull = self._pulls.get(name)
            if pull is not None and not pull.done:
                raise LocalModelError(
                    f"{model} is still downloading; Voice Cut can start once it is done"
                )
            raise LocalModelError(
                f"Download {model} first: open the planner's model list and "
                "choose Download"
            )
        return client.url

    def stop(self) -> None:
        with self._lock:
            for pull in self._pulls.values():
                pull.cancelled = True
        with contextlib.suppress(Exception):
            self.server.stop()


def _pull_error(message: str, model: str) -> str:
    if "file does not exist" in message or "not found" in message.casefold():
        return f"Ollama has no model called {model}"
    return message
