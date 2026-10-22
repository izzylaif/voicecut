"""Ollama: how VoiceCut runs planner models on this computer.

Every system gets the same interface: an Ollama server and its HTTP API.  The
server VoiceCut talks to is, in this order,

1. one that already answers at ``OLLAMA_HOST`` (``127.0.0.1:11434`` unless
   set): the user's own Ollama, or the one the editor started for its Voice
   Cut runs (the editor sets ``OLLAMA_HOST`` for them);
2. otherwise one the editor starts itself (:class:`OllamaServer`) from an
   Ollama installed on the system, else from VoiceCut's own copy (the desktop
   app's setup installs it and names it in ``VOICECUT_OLLAMA``, with its
   models in ``VOICECUT_OLLAMA_MODELS``).  It listens on a free loopback port
   and stops with the editor.

Requests stream, so a long answer is never cut off by a total timeout: only a
silence longer than :data:`REQUEST_IDLE_SECONDS` (loading a large model and
reading a long prompt on a CPU) ends one.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import processes

DEFAULT_HOST = "127.0.0.1:11434"
DEFAULT_PORT = 11434
# Large enough for a window prompt (about 7k tokens, more on a retry, which
# repeats the rejected answer) plus its answer and the model's reasoning.
CONTEXT_TOKENS = 32768
SERVER_START_SECONDS = 90.0
SERVER_STOP_SECONDS = 5.0
PROBE_SECONDS = 1.5
REQUEST_IDLE_SECONDS = 1800.0
INSTALL_PAGE = "https://ollama.com/download"
# Where Ollama's own installers put it.
SYSTEM_LOCATIONS = {
    "darwin": (
        "/Applications/Ollama.app/Contents/Resources/ollama",
        "~/Applications/Ollama.app/Contents/Resources/ollama",
        "/usr/local/bin/ollama",
        "/opt/homebrew/bin/ollama",
    ),
    "linux": ("/usr/local/bin/ollama", "/usr/bin/ollama"),
    "win32": ("~/AppData/Local/Programs/Ollama/ollama.exe",),
}


class OllamaError(RuntimeError):
    """Ollama refused a request or could not be reached."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OllamaUnavailable(OllamaError):
    """No Ollama runs here and none can be started."""


def host_url(value: str | None) -> str:
    """The base URL of an ``OLLAMA_HOST`` value, as Ollama's own client reads it."""

    raw = (value or "").strip() or DEFAULT_HOST
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    host = parts.hostname or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = parts.port or (DEFAULT_PORT if parts.scheme == "http" else 443)
    rendered = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{rendered}:{port}{parts.path.rstrip('/')}"


def configured_url() -> str:
    return host_url(os.environ.get("OLLAMA_HOST"))


@dataclass
class ChatResult:
    content: str
    thinking: str
    done_reason: str | None
    prompt_tokens: int | None
    output_tokens: int | None


class OllamaClient:
    """The parts of Ollama's API VoiceCut uses."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def _request(
        self, method: str, path: str, body: Any = None, *, timeout: float
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            raise OllamaError(_error_text(error), status=error.code) from None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise OllamaError(f"cannot reach Ollama at {self.url}: {reason}") from None

    def _json(
        self, method: str, path: str, body: Any = None, *, timeout: float = 30.0
    ) -> Any:
        with self._request(method, path, body, timeout=timeout) as response:
            text = response.read().decode("utf-8")
        return json.loads(text) if text.strip() else None

    def _stream(
        self, path: str, body: Any, *, timeout: float
    ) -> Iterator[dict[str, Any]]:
        with self._request("POST", path, body, timeout=timeout) as response:
            for line in response:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if isinstance(chunk, dict) and chunk.get("error"):
                    raise OllamaError(str(chunk["error"]))
                yield chunk

    def version(self, *, timeout: float = PROBE_SECONDS) -> str | None:
        """The server's version, or None when nothing answers."""

        try:
            value = self._json("GET", "/api/version", timeout=timeout)
        except (OllamaError, ValueError):
            return None
        return str(value.get("version")) if isinstance(value, dict) else None

    def models(self) -> list[dict[str, Any]]:
        value = self._json("GET", "/api/tags")
        return list(value.get("models") or []) if isinstance(value, dict) else []

    def show(self, model: str) -> dict[str, Any]:
        value = self._json("POST", "/api/show", {"model": model})
        return value if isinstance(value, dict) else {}

    def delete(self, model: str) -> None:
        self._json("DELETE", "/api/delete", {"model": model})

    def pull(
        self,
        model: str,
        *,
        progress: Callable[[dict[str, Any]], None],
        cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        """Download ``model``, reporting Ollama's progress records."""

        for chunk in self._stream(
            "/api/pull", {"model": model}, timeout=REQUEST_IDLE_SECONDS
        ):
            if cancelled():
                raise OllamaError("the download was cancelled")
            progress(chunk)

    def chat(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, str]],
        format: dict[str, Any] | None,
        think: bool | None,
        options: dict[str, Any],
        on_progress: Callable[[str, int], None] | None = None,
    ) -> ChatResult:
        """One chat answer, streamed; ``on_progress(phase, tokens)`` follows it."""

        body: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": True,
            "options": options,
        }
        if format is not None:
            body["format"] = format
        if think is not None:
            body["think"] = think
        content: list[str] = []
        thinking: list[str] = []
        last: dict[str, Any] = {}
        tokens = 0
        for chunk in self._stream("/api/chat", body, timeout=REQUEST_IDLE_SECONDS):
            message = chunk.get("message") or {}
            if message.get("thinking"):
                thinking.append(str(message["thinking"]))
                tokens += 1
                if on_progress:
                    on_progress("thinking", tokens)
            if message.get("content"):
                content.append(str(message["content"]))
                tokens += 1
                if on_progress:
                    on_progress("answering", tokens)
            if chunk.get("done"):
                last = chunk
        return ChatResult(
            content="".join(content),
            thinking="".join(thinking),
            done_reason=last.get("done_reason"),
            prompt_tokens=last.get("prompt_eval_count"),
            output_tokens=last.get("eval_count"),
        )


def _error_text(error: urllib.error.HTTPError) -> str:
    try:
        value = json.loads(error.read().decode("utf-8"))
    except (ValueError, OSError):
        value = None
    if isinstance(value, dict) and value.get("error"):
        return str(value["error"])
    return f"Ollama answered HTTP {error.code}"


# ---------------------------------------------------------------------------
# Which Ollama, and starting it


def system_binary() -> Path | None:
    """An ``ollama`` the user installed (on PATH or where its installer puts it)."""

    found = shutil.which("ollama")
    if found:
        return Path(found)
    for location in SYSTEM_LOCATIONS.get(sys.platform, ()):
        candidate = Path(location).expanduser()
        if candidate.is_file():
            return candidate
    return None


def voicecut_binary() -> Path | None:
    """VoiceCut's own Ollama, which the desktop app's setup installed."""

    value = os.environ.get("VOICECUT_OLLAMA", "").strip()
    return Path(value) if value and Path(value).is_file() else None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class _Started:
    process: subprocess.Popen[bytes]
    url: str
    source: str
    log: Any = field(repr=False)


class OllamaServer:
    """The Ollama server of this editor: found running, or started on demand.

    A server it starts is its child in its process group (tree on Windows), so
    stopping the editor's process tree stops it too; :meth:`stop` ends it when
    the editor exits normally.
    """

    def __init__(self, *, log_path: Path | None = None) -> None:
        self.log_path = log_path
        self._lock = threading.Lock()
        self._started: _Started | None = None

    def _own_url(self) -> str | None:
        started = self._started
        if started is not None and started.process.poll() is None:
            return started.url
        return None

    def running(self) -> tuple[str, str] | None:
        """``(url, source)`` of a server that answers, without starting one."""

        own = self._own_url()
        if own and OllamaClient(own).version():
            assert self._started is not None
            return own, self._started.source
        url = configured_url()
        if OllamaClient(url).version():
            return url, "system"
        return None

    @staticmethod
    def installed() -> tuple[Path, str] | None:
        """The Ollama this editor would start, and whose it is."""

        system = system_binary()
        if system is not None:
            return system, "system"
        own = voicecut_binary()
        return (own, "voicecut") if own is not None else None

    def ensure(self) -> str:
        """The URL of a running server, starting one if there is none."""

        with self._lock:
            found = self.running()
            if found is not None:
                return found[0]
            binary = self.installed()
            if binary is None:
                raise OllamaUnavailable(
                    "Local models need Ollama, which is not installed: install "
                    f"it from {INSTALL_PAGE} (the VoiceCut app installs it "
                    "during setup)"
                )
            return self._start(*binary)

    def status(self) -> dict[str, Any]:
        """Where Ollama runs, starting it if it is installed but stopped."""

        try:
            url = self.ensure()
        except OllamaError as error:
            return {
                "available": False,
                "url": None,
                "version": None,
                "source": None,
                "message": str(error),
            }
        source = (
            self._started.source
            if self._started is not None and self._own_url() == url
            else "system"
        )
        return {
            "available": True,
            "url": url,
            "version": OllamaClient(url).version(),
            "source": source,
            "message": None,
        }

    def _start(self, binary: Path, source: str) -> str:
        self._stop_locked()
        port = _free_port()
        environment = {
            **os.environ,
            "OLLAMA_HOST": f"127.0.0.1:{port}",
            "OLLAMA_CONTEXT_LENGTH": str(CONTEXT_TOKENS),
            "OLLAMA_NO_CLOUD": "1",
        }
        if source == "voicecut":
            models = os.environ.get("VOICECUT_OLLAMA_MODELS", "").strip()
            if models:
                Path(models).mkdir(parents=True, exist_ok=True)
                environment["OLLAMA_MODELS"] = models
                if not processes.WINDOWS:
                    # Ollama keeps its key in ~/.ollama: keep it with the models.
                    home = Path(models).parent / "home"
                    home.mkdir(parents=True, exist_ok=True)
                    environment["HOME"] = str(home)
        log: Any = subprocess.DEVNULL
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log = open(self.log_path, "ab")  # noqa: SIM115 - the server's lifetime
        try:
            process = subprocess.Popen(
                [str(binary), "serve"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT if log is not subprocess.DEVNULL else log,
                env=environment,
            )
        except OSError as error:
            if log is not subprocess.DEVNULL:
                log.close()
            raise OllamaUnavailable(
                f"cannot start Ollama ({binary}): {error}"
            ) from None
        url = f"http://127.0.0.1:{port}"
        self._started = _Started(process=process, url=url, source=source, log=log)
        client = OllamaClient(url)
        deadline = time.monotonic() + SERVER_START_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._stop_locked()
                raise OllamaUnavailable(
                    f"Ollama ({binary}) stopped while starting (exit status "
                    f"{process.returncode})"
                    + (f"; see {self.log_path}" if self.log_path else "")
                )
            if client.version(timeout=1.0):
                return url
            time.sleep(0.25)
        self._stop_locked()
        raise OllamaUnavailable(f"Ollama ({binary}) did not start in time")

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        started, self._started = self._started, None
        if started is None:
            return
        process = started.process
        if process.poll() is None:
            if processes.WINDOWS:
                # Its model runners are its children.
                processes.terminate_tree(process, force=True)
            else:
                process.terminate()
            try:
                process.wait(SERVER_STOP_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(SERVER_STOP_SECONDS)
        if started.log is not subprocess.DEVNULL:
            with contextlib.suppress(OSError):
                started.log.close()


def canonical_name(model: str) -> str:
    """``model`` as Ollama lists it: a name without a tag means ``:latest``."""

    model = model.strip()
    return model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"
