"""Local planner models for the editor: Ollama, its models and their downloads.

The editor owns one :class:`~voicecut.ollama.OllamaServer`: a running Ollama
when there is one, else one it starts (and stops when it exits).  The model
list puts VoiceCut's recommendations first, then every other model Ollama
has; a model downloads in the background (``/api/pull``) while the list
reports its progress.  How a model reasons before answering (off, on, or a
level for models that take one) is a setting of that model, kept in
``local_models.json`` next to the editor's projects; a recommended model
starts from the setting that planned best.

Below the recommended LLMs come the local decision models
(:mod:`voicecut.decision_models`, Kev on Apple Silicon).  They are not Ollama
models: their weights download from Hugging Face into its cache, and a Voice
Cut with one does not need Ollama.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import re
import threading
from pathlib import Path
from typing import Any

from .. import decision_models, local_models, ollama

SETTINGS_FILE = "local_models.json"
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")


class LocalModelError(ValueError):
    """A local model request is invalid or cannot be served."""


def check_name(model: Any) -> str:
    if not isinstance(model, str) or not MODEL_NAME.fullmatch(model.strip()):
        raise LocalModelError("a model name such as gemma4:31b")
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


class _HubPull:
    """A local decision model's weights downloading from Hugging Face, file by
    file; progress counts finished files and the partial one in the cache."""

    def __init__(self, model: decision_models.DecisionModel) -> None:
        self.model = model
        self.status = "starting"
        self.error: str | None = None
        self.done = False
        self.cancelled = False
        self.total = 0
        self.finished = 0
        self.blob_dirs: list[Path] = []

    def partial(self) -> int:
        size = 0
        for directory in self.blob_dirs:
            with contextlib.suppress(OSError):
                for path in directory.glob("*.incomplete"):
                    size += path.stat().st_size
        return size

    def view(self) -> dict[str, Any]:
        completed = min(self.total, self.finished + self.partial()) if self.total else 0
        return {
            "status": self.status,
            "completed": completed,
            "total": self.total,
            "fraction": completed / self.total if self.total else None,
            "error": self.error,
            "done": self.done,
        }

    def run(self) -> None:
        from huggingface_hub import HfApi, hf_hub_download
        from huggingface_hub.constants import HF_HUB_CACHE

        api = HfApi()
        files: list[tuple[str, str | None, str, int]] = []
        for repo, revision in decision_models.weight_repos(self.model):
            info = api.model_info(repo, revision=revision, files_metadata=True)
            self.blob_dirs.append(
                Path(HF_HUB_CACHE) / f"models--{repo.replace('/', '--')}" / "blobs"
            )
            for sibling in info.siblings or []:
                name = sibling.rfilename
                if any(
                    fnmatch.fnmatch(name, pattern)
                    for pattern in decision_models.WEIGHT_PATTERNS
                ):
                    files.append((repo, revision, name, int(sibling.size or 0)))
        self.total = sum(size for *_, size in files)
        self.status = "downloading"
        for repo, revision, name, size in files:
            if self.cancelled:
                raise InterruptedError
            hf_hub_download(repo, name, revision=revision)
            self.finished += size


class LocalModels:
    """Ollama and its models, as the editor's planner list shows them."""

    def __init__(self, root: Path, *, log_path: Path | None = None) -> None:
        self.settings_path = root / SETTINGS_FILE
        self.server = ollama.OllamaServer(log_path=log_path)
        self._lock = threading.Lock()
        self._pulls: dict[str, _Pull] = {}
        # The think values each downloaded model takes, by its digest.
        self._choices: dict[str, list[local_models.Think]] = {}

    # -- per-model settings -----------------------------------------------------
    def _settings(self) -> dict[str, Any]:
        try:
            value = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def think(self, model: str) -> local_models.Think:
        """How ``model`` reasons before answering in a Voice Cut.

        Unless set, a recommended model's best setting; any other model
        reasons, since a model that plans badly without reasoning is far
        worse off than one that is slower with it.
        """

        if decision_models.find("ollama", model) is not None:
            return False
        saved = (self._settings().get("think") or {}).get(ollama.canonical_name(model))
        if local_models.is_think(saved):
            return saved
        recommended = local_models.recommended_think(model)
        return True if recommended is None else recommended

    def think_argument(self, model: str) -> str:
        """The setting as the pipeline's ``--planner-think``."""

        value = self.think(model)
        return {True: "on", False: "off"}.get(value, value)  # type: ignore[arg-type]

    def set_think(self, model: Any, value: Any) -> dict[str, Any]:
        model = check_name(model)
        if not local_models.is_think(value):
            raise LocalModelError("think must be true, false, low, medium or high")
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
                    url=status["url"],
                )
            )
        for decision in decision_models.local_models_here():
            seen.update({decision.id, ollama.canonical_name(decision.id)})
            models.append(
                self._decision_entry(
                    decision, pulls.get(decision.id), default=decision.id == default
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
                    url=status["url"],
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
                        url=None,
                    )
                )
        return {
            "models": models,
            "default": default,
            "error": error,
            "ollama": status,
            "memory": {
                "gb": local_models.memory_gb(),
                "needed_gb": local_models.MEMORY_NEEDED_GB,
            },
            "speed_note": local_models.SPEED_NOTE,
        }

    def _decision_entry(
        self,
        model: decision_models.DecisionModel,
        pull: dict[str, Any] | None,
        *,
        default: bool,
    ) -> dict[str, Any]:
        downloaded = decision_models.weights_ready(model)
        parts = ["MLX", "decision model"]
        if model.size_gb:
            parts.append(f"{model.size_gb:.1f} GB")
        parts.append("downloaded" if downloaded else "not downloaded")
        return {
            "id": model.id,
            "label": model.label,
            "detail": " · ".join(parts),
            "alias": False,
            "default": default,
            "downloaded": downloaded,
            "size_gb": model.size_gb,
            "engine": "MLX",
            "recommended": True,
            "decision": True,
            "can_think": False,
            "think": False,
            "think_options": None,
            "pull": pull,
        }

    def _think_choices(
        self, model: str, have: dict[str, Any] | None, url: str | None
    ) -> list[local_models.Think] | None:
        """The think values ``model`` takes (None: it cannot reason)."""

        if have is None:
            recommended = local_models.recommended_think(model)
            if recommended is None:
                return None
            if isinstance(recommended, str):
                return [False, *local_models.THINK_LEVELS]
            return [False, True]
        if "thinking" not in (have.get("capabilities") or []):
            return None
        digest = str(have.get("digest") or model)
        with self._lock:
            cached = self._choices.get(digest)
        if cached is None and url:
            try:
                shown = ollama.OllamaClient(url).show(model)
            except ollama.OllamaError:
                shown = {}
            cached = ollama.think_choices(shown) or [False, True]
            with self._lock:
                self._choices[digest] = cached
        return cached or [False, True]

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
        url: str | None,
    ) -> dict[str, Any]:
        # Known from Ollama once downloaded; before, only for recommendations.
        choices = self._think_choices(model, have, url)
        can_think: bool | None = choices is not None
        if have is None and choices is None:
            can_think = None
        think: local_models.Think = False
        if choices is not None:
            wanted = self.think(model)
            fitted = local_models.fit_think(wanted, choices)
            think = wanted if fitted is None else fitted
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
            "think": think,
            "think_options": choices,
            "pull": pull,
        }

    # -- downloads --------------------------------------------------------------
    def _pull_decision(self, model: decision_models.DecisionModel) -> dict[str, Any]:
        if not decision_models.runs_here(model):
            raise LocalModelError(
                f"{model.label} runs on Apple Silicon with at least "
                f"{model.memory_gb:.0f} GB of memory"
            )
        with self._lock:
            current = self._pulls.get(model.id)
            if current is not None and not current.done:
                return self.listing()
            pull = _HubPull(model)
            self._pulls[model.id] = pull  # type: ignore[assignment]

        def run() -> None:
            try:
                pull.run()
            except InterruptedError:
                pull.error = "cancelled"
            except Exception as error:  # noqa: BLE001 - shown in the list
                pull.error = str(error) or type(error).__name__
            else:
                pull.status = "success"
            finally:
                pull.done = True
                if pull.error is None or pull.cancelled:
                    with self._lock:
                        if self._pulls.get(model.id) is pull:
                            del self._pulls[model.id]

        threading.Thread(target=run, daemon=True, name=f"pull {model.id}").start()
        return self.listing()

    def pull(self, model: Any) -> dict[str, Any]:
        model = check_name(model)
        decision = decision_models.find("ollama", model)
        if decision is not None:
            return self._pull_decision(decision)
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
        model = check_name(model)
        name = model if decision_models.find("ollama", model) else ollama.canonical_name(model)
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
        decision = decision_models.find("ollama", model)
        with self._lock:
            pull = self._pulls.get(model if decision else ollama.canonical_name(model))
            if pull is not None and not pull.done:
                raise LocalModelError(f"{model} is still downloading")
        if decision is not None:
            _delete_hub_weights(decision)
            return self.listing()
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
        """The URL a Voice Cut with ``model`` uses; raises with what to do.

        A local decision model needs its weights and no Ollama ("" then)."""

        decision = decision_models.find("ollama", model)
        if decision is not None:
            if not decision_models.runs_here(decision):
                raise LocalModelError(
                    f"{decision.label} runs on Apple Silicon with at least "
                    f"{decision.memory_gb:.0f} GB of memory; choose another model"
                )
            if not decision_models.weights_ready(decision):
                with self._lock:
                    pull = self._pulls.get(decision.id)
                if pull is not None and not pull.done:
                    raise LocalModelError(
                        f"{decision.label} is still downloading; Voice Cut can start once it is done"
                    )
                raise LocalModelError(
                    f"Download {decision.label} first: open the planner's model list and "
                    "choose Download"
                )
            return ""
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


def _delete_hub_weights(model: decision_models.DecisionModel) -> None:
    """Remove a local decision model's repositories from the Hugging Face cache."""

    from huggingface_hub import scan_cache_dir

    wanted = {repo for repo, _ in decision_models.weight_repos(model)}
    try:
        cache = scan_cache_dir()
    except Exception as error:  # noqa: BLE001
        raise LocalModelError(f"cannot read the Hugging Face cache: {error}") from None
    hashes = [
        revision.commit_hash
        for repo in cache.repos
        if repo.repo_id in wanted and repo.repo_type == "model"
        for revision in repo.revisions
    ]
    if hashes:
        cache.delete_revisions(*hashes).execute()


def _pull_error(message: str, model: str) -> str:
    if "file does not exist" in message or "not found" in message.casefold():
        return f"Ollama has no model called {model}"
    return message
