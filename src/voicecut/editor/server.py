"""FastAPI application of the VoiceCut editor.

The server owns project storage, runs VoiceCut in a worker subprocess and
streams its stages over Server-Sent Events, prepares video previews, renders
preview audio for any stretch of the edited timeline, and exports.  It is a
single-user local tool, so long work runs in plain threads.

With ``desktop=True`` it is the engine of the VoiceCut desktop app: projects
are ``.vc`` documents opened from anywhere (:mod:`.documents`), API keys live
in the macOS Keychain, only the app's own pages may call it, and stopping it
stops every running Voice Cut.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .. import __version__, processes
from ..common import read_json
from ..language_profiles import SUPPORTED_LANGUAGE_CODES
from ..planner_backends import (
    API_KEY_ENV_BY_BACKEND,
    DEFAULT_LOCAL_PYTHON,
    DEFAULT_MODEL_BY_BACKEND,
    PLANNER_BACKENDS,
)
from . import ambience, documents, peaks, plan, proxy, settings
from .export import EXPORT_STAGES, ExportError, check_destination, export_timeline
from .jobs import Job, JobRegistry, sse_stream
from .projects import (
    DEFAULT_SETTINGS,
    PROJECT_ROOT,
    SETTINGS_KEYS,
    Project,
    ProjectError,
    ProjectNotFound,
    ProjectStore,
    utc_now,
    write_json_atomic,
)
from .timeline import (
    Proposal,
    TimelineError,
    full_source_items,
    load_proposal,
    timeline_revision,
    validate_timeline,
)
from .worker import STDIN_CONTROL_ENV

PREVIEW_CACHE_SIZE = 32
MAX_RENDER_FRAMES = 1 << 20
EXCLUSIVE_JOBS = frozenset({"voicecut", "export"})
PREPARE_STAGES = {
    "proxy": "Building the video preview",
    "filmstrip": "Making thumbnails",
}
SHUTDOWN_GRACE_SECONDS = 5.0
FRONTEND_MISSING = """<!doctype html><meta charset="utf-8"><title>VoiceCut editor</title>
<body style="font:16px system-ui;max-width:40rem;margin:4rem auto">
<h1>VoiceCut editor</h1><p>The API is running, but the frontend is not built.</p>
<pre>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</pre></body>"""


def spawn_worker(
    request_path: Path, environment: dict[str, str] | None = None
) -> subprocess.Popen[str]:
    """Start ``python -m voicecut.editor.worker`` as the root of its own tree.

    ``environment`` adds variables (a planner key from the Keychain) to the
    editor's own.  The worker stops when it reads "cancel" on its standard
    input, or when that pipe closes because the editor is gone.
    """

    environment = {**os.environ, **(environment or {}), STDIN_CONTROL_ENV: "1"}
    source = str(PROJECT_ROOT / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source + (os.pathsep + existing if existing else "")
    return subprocess.Popen(
        [sys.executable, "-m", "voicecut.editor.worker", str(request_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=PROJECT_ROOT,
        env=environment,
        **processes.own_group(),
    )


class EditorService:
    """Everything the HTTP routes do, independent of FastAPI."""

    def __init__(
        self,
        store: ProjectStore,
        *,
        env_file: Path,
        defaults: dict[str, Any],
        keys: settings.KeyStore | None = None,
    ):
        self.store = store
        self.jobs = JobRegistry()
        self.env_file = env_file
        self.keys = keys or settings.EnvFileKeys(env_file)
        self.models = settings.ModelCatalog(env_file, keys=self.keys)
        self.preferences_path = store.root / "preferences.json"
        # The last Voice Cut settings chosen in the editor beat the CLI's.
        remembered = settings.load_preferences(self.preferences_path)
        self.defaults = {
            **DEFAULT_SETTINGS,
            **defaults,
            **{key: remembered[key] for key in SETTINGS_KEYS if key in remembered},
        }
        store.defaults = dict(self.defaults)
        self._lock = threading.Lock()
        self._proposals: dict[str, tuple[Path, float, Proposal]] = {}
        self._previews: OrderedDict[tuple[str, str], plan.Derivation] = OrderedDict()

    # -- projects ----------------------------------------------------------------
    def project(self, project_id: str) -> Project:
        try:
            return self.store.get(project_id)
        except ProjectNotFound as error:
            raise HTTPException(404, "project not found") from error

    def proposal(self, project: Project) -> Proposal | None:
        """VoiceCut's proposal of the last run (cached per plan file)."""

        path = project.plan_path
        if path is None:
            return None
        mtime = path.stat().st_mtime
        with self._lock:
            cached = self._proposals.get(project.id)
            if cached and cached[0] == path and cached[1] == mtime:
                return cached[2]
        value = load_proposal(path)
        with self._lock:
            self._proposals[project.id] = (path, mtime, value)
        return value

    def saved_timeline(
        self, project: Project, proposal: Proposal | None
    ) -> dict | None:
        if not project.timeline_path.is_file():
            return None
        try:
            return validate_timeline(
                read_json(project.timeline_path),
                source_frames=project.audio_geometry()[2],
                proposal=proposal,
                allow_pauses=project.kind != "video",
                sample_rate=project.audio_geometry()[0],
            )
        except (TimelineError, ValueError, OSError):
            return None  # belongs to an earlier Voice Cut result

    def state(self, project: Project) -> dict[str, Any]:
        project.ensure_peaks()
        if (
            project.kind == "video"
            and project.preview.get("status", "pending") == "pending"
        ):
            self.start_prepare(project)
            project = self.project(project.id)
        proposal, proposal_error = None, None
        try:
            proposal = self.proposal(project)
        except (TimelineError, OSError, ValueError, KeyError) as error:
            proposal_error = str(error)
        frames = project.audio_geometry()[2]
        jobs = {
            kind: job.id
            for kind in ("voicecut", "export", "prepare")
            if (job := self.jobs.running(project.id, frozenset({kind}))) is not None
        }
        return {
            **project.summary(),
            "uploaded": project.uploaded,
            "source_missing": not documents.source_available(project),
            "document": (
                {"path": str(project.root), "name": project.root.stem}
                if project.document
                else None
            ),
            "media": project.media_state(),
            "settings": {key: project.settings.get(key) for key in SETTINGS_KEYS},
            "run": project.run,
            "proposal": proposal.to_client() if proposal else None,
            "proposal_error": proposal_error,
            "source_items": full_source_items(frames),
            "timeline": self.saved_timeline(project, proposal),
            "exports": project.exports,
            "default_export_path": str(project.default_export_path()),
            "jobs": jobs,
        }

    def import_media(self, *, upload: UploadFile | None, path: str | None) -> Project:
        if upload is not None:
            return self.store.import_upload(upload.filename or "", upload.file)
        if not path:
            raise HTTPException(400, "send a file or a path")
        try:
            return self.store.import_path(Path(path))
        except FileNotFoundError as error:
            raise HTTPException(404, f"no such file: {path}") from error

    def delete(self, project: Project) -> None:
        if self.jobs.running(project.id, EXCLUSIVE_JOBS | {"prepare"}):
            raise HTTPException(409, "the project has a job running")
        self.store.delete(project.id)

    # -- documents (desktop app) ------------------------------------------------------
    @property
    def documents(self) -> documents.DocumentStore:
        if not isinstance(self.store, documents.DocumentStore):
            raise HTTPException(404, "documents are a feature of the desktop app")
        return self.store

    def open_document(self, path: str | None) -> Project:
        if not path:
            raise HTTPException(400, "send the document's path")
        return self.documents.open_document(Path(path))

    def create_document(self, source: str | None, path: str | None) -> Project:
        if not source or not path:
            raise HTTPException(400, "send the recording and the document's path")
        try:
            return self.documents.create_document(Path(source), Path(path))
        except FileNotFoundError as error:
            raise HTTPException(404, f"no such file: {source}") from error

    def save_document_as(self, project: Project, path: str | None) -> Project:
        if not path:
            raise HTTPException(400, "send the new document's path")
        if self.jobs.running(project.id, EXCLUSIVE_JOBS | {"prepare"}):
            raise HTTPException(
                409, "wait until Voice Cut or the export finishes, then save"
            )
        return self.documents.save_document_as(project.id, Path(path))

    def close_document(self, project: Project, *, cancel: bool) -> None:
        running = self.jobs.running(project.id, EXCLUSIVE_JOBS | {"prepare"})
        if running is not None and not cancel:
            raise HTTPException(409, "the project has a job running")
        self.stop_jobs(project.id)
        self.documents.close_document(project.id)

    def locate_source(self, project: Project, path: str | None) -> Project:
        if not path:
            raise HTTPException(400, "send the recording's path")
        store = self.store
        if not isinstance(store, documents.DocumentStore):
            store = documents.DocumentStore(store.root, defaults=store.defaults)
        try:
            return store.locate_source(project.id, Path(path))
        except FileNotFoundError as error:
            raise HTTPException(404, f"no such file: {path}") from error

    def stop_jobs(self, project_id: str | None = None) -> None:
        """Cancel running Voice Cut runs and wait until their workers exit."""

        jobs = [
            job
            for job in self.jobs.all()
            if job.kind == "voicecut"
            and not job.finished
            and project_id in (None, job.project_id)
        ]
        for job in jobs:
            job.cancel_requested = True
            self._cancel(job)
        deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
        for job in jobs:
            process = job.process
            if process is None:
                continue
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(max(0.0, deadline - time.monotonic()))
            processes.terminate_tree(process, force=True)

    @staticmethod
    def _cancel(job: Job) -> None:
        """Ask a running Voice Cut to stop; its worker ends its stages."""

        process = job.process
        if process is None or process.poll() is not None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError, ValueError):
                process.stdin.write("cancel\n")
                process.stdin.flush()
                return
        processes.terminate_tree(process)

    # -- video preview -------------------------------------------------------------
    def start_prepare(self, project: Project) -> Job | None:
        if project.kind != "video":
            return None
        with self._lock:
            if (
                job := self.jobs.running(project.id, frozenset({"prepare"}))
            ) is not None:
                return job
            job = self.jobs.create("prepare", project.id)

        def mark(current: Project) -> None:
            current.preview.update(status="preparing", error=None, progress=0.0)

        self.store.update(project.id, mark)
        threading.Thread(
            target=self._prepare, args=(job, project), daemon=True, name=job.id
        ).start()
        return job

    def _prepare(self, job: Job, project: Project) -> None:
        last = [0.0, 0.0]

        def progress(fraction: float) -> None:
            now = time.monotonic()
            if fraction - last[0] >= 0.01 and now - last[1] >= 0.2 or fraction >= 1:
                last[:] = [fraction, now]
                job.append({"type": "progress", "stage": "proxy", "fraction": fraction})

        try:
            duration = project.audio_geometry()[2] / project.audio_geometry()[0]
            job.stage("proxy", "started", PREPARE_STAGES["proxy"])
            info = proxy.build_proxy(
                project.source_path,
                project.proxy_path,
                duration=duration,
                on_progress=progress,
            )
            job.stage("proxy", "completed", PREPARE_STAGES["proxy"])
            job.stage("filmstrip", "started", PREPARE_STAGES["filmstrip"])
            strip = proxy.build_filmstrip(
                project.proxy_path,
                project.filmstrip_dir,
                width=info["width"],
                height=info["height"],
                duration=info["duration"] or duration,
            )
            job.stage("filmstrip", "completed", PREPARE_STAGES["filmstrip"])
        except Exception as error:  # noqa: BLE001 - reported to the browser
            message = str(error) or type(error).__name__

            def failed(current: Project) -> None:
                current.preview.update(status="failed", error=message, progress=None)

            self.store.update(project.id, failed)
            job.append({"type": "error", "message": message})
            return

        def ready(current: Project) -> None:
            current.preview = {
                "status": "ready",
                "proxy": info,
                "filmstrip": strip,
                "version": int(current.preview.get("version", 0)) + 1,
            }

        self.store.update(project.id, ready)
        job.append({"type": "complete", "result": {"proxy": info, "filmstrip": strip}})

    # -- Voice Cut -------------------------------------------------------------------
    def settings_from(self, project: Project, body: dict[str, Any]) -> dict[str, Any]:
        chosen = {key: project.settings.get(key) for key in SETTINGS_KEYS}
        for key in SETTINGS_KEYS:
            if key in body:
                value = body[key]
                chosen[key] = value.strip() if isinstance(value, str) else value
                if chosen[key] in ("", None):
                    chosen[key] = None
        if chosen["language"] not in SUPPORTED_LANGUAGE_CODES:
            raise HTTPException(400, f"unsupported language {chosen['language']!r}")
        if chosen["planner_backend"] not in PLANNER_BACKENDS:
            raise HTTPException(400, "unsupported planner backend")
        if chosen["breath_cleanup"] not in (None, "off", "replace"):
            raise HTTPException(400, "breath_cleanup must be off or replace")
        model = chosen["planner_model"]
        if model is not None and (
            not isinstance(model, str)
            or len(model) > 200
            or any(ord(c) < 32 or c.isspace() for c in model)
        ):
            raise HTTPException(400, "the model name must be one word")
        return chosen

    def save_settings(self, project: Project, body: dict[str, Any]) -> dict[str, Any]:
        """Remember a project's Voice Cut settings (and use them for new ones)."""

        chosen = self.settings_from(project, body)

        def apply(current: Project) -> None:
            current.settings = chosen

        self.store.update(project.id, apply)
        self.defaults.update(chosen)
        self.store.defaults = dict(self.defaults)
        settings.save_preferences(self.preferences_path, chosen)
        return chosen

    def check_planner(self, chosen: dict[str, Any]) -> None:
        """Refuse to start a run that would fail for want of a key or runtime."""

        backend = chosen["planner_backend"]
        key_env = API_KEY_ENV_BY_BACKEND.get(backend)
        if key_env:
            value, _ = settings.key_value(self.keys, key_env)
            if not value:
                label = settings.PLANNER_LABELS.get(backend, backend)
                raise HTTPException(
                    400, f"Add your {label} API key in Settings ({key_env}) first"
                )
        elif not DEFAULT_LOCAL_PYTHON.is_file():
            raise HTTPException(
                400, "Local models need VoiceCut's MLX environment (.venv-mlx)"
            )

    # -- settings dialog --------------------------------------------------------------
    def settings_state(self) -> dict[str, Any]:
        return {
            "env_file": str(self.env_file),
            "key_store": {"kind": self.keys.kind, "location": self.keys.location},
            "planners": settings.planners(self.keys),
            "default_models": dict(DEFAULT_MODEL_BY_BACKEND),
            "languages": list(SUPPORTED_LANGUAGE_CODES),
            "defaults": {key: self.defaults.get(key) for key in SETTINGS_KEYS},
        }

    def save_key(self, backend: str, body: Any) -> dict[str, Any]:
        key_env = API_KEY_ENV_BY_BACKEND.get(backend)
        if key_env is None:
            raise HTTPException(404, f"{backend} does not use an API key")
        value = body.get("key") if isinstance(body, dict) else None
        if value is not None and not isinstance(value, str):
            raise HTTPException(400, "key must be a string")
        value = (value or "").strip()
        if value:
            # A typo is caught here instead of at the start of a long run.
            checked = self.models.models(backend, refresh=True, key=value)
            error = checked["error"]
            if error and error["kind"] == "invalid_key":
                raise HTTPException(400, error["message"])
        try:
            self.keys.set(key_env, value or None)
        except settings.SettingsError as error:
            raise HTTPException(400, str(error)) from error
        except OSError as error:
            raise HTTPException(
                500, f"cannot save the key in {self.keys.location}: {error}"
            ) from error
        return self.settings_state()

    def list_models(self, backend: str, refresh: bool) -> dict[str, Any]:
        try:
            return self.models.models(backend, refresh=refresh)
        except settings.SettingsError as error:
            raise HTTPException(404, str(error)) from error

    def pipeline_argv(self, project: Project, chosen: dict[str, Any]) -> list[str]:
        argv = [
            str(project.source_path),
            "-o",
            str(project.pipeline_output),
            "--overwrite",
            "--env-file",
            str(self.env_file),
            "--language",
            str(chosen["language"]),
            "--planner-backend",
            str(chosen["planner_backend"]),
        ]
        if chosen["planner_model"]:
            argv += ["--planner-model", str(chosen["planner_model"])]
        if chosen["breath_cleanup"]:
            argv += ["--breath-cleanup", str(chosen["breath_cleanup"])]
        return argv

    def worker_environment(self, chosen: dict[str, Any]) -> dict[str, str]:
        """The planner key for the worker, unless the pipeline reads it itself.

        The pipeline reads the editor's environment and the env file; a key
        kept anywhere else travels in the worker's environment.
        """

        key_env = API_KEY_ENV_BY_BACKEND.get(chosen["planner_backend"])
        if not key_env or self.keys.kind == "env_file":
            return {}
        value, source = settings.key_value(self.keys, key_env)
        return {key_env: value} if value and source == self.keys.kind else {}

    def start_voicecut(self, project: Project, body: dict[str, Any]) -> Job:
        chosen = self.settings_from(project, body)
        self.check_planner(chosen)
        if not documents.source_available(project):
            raise HTTPException(
                400,
                f"The original recording {project.source_path.name} was moved or "
                "deleted; locate it first",
            )
        with self._lock:
            if self.jobs.running(project.id, EXCLUSIVE_JOBS):
                raise HTTPException(409, "Voice Cut or an export is already running")
            job = self.jobs.create("voicecut", project.id)
        request_path = project.jobs_dir / f"{job.id}.json"
        write_json_atomic(request_path, {"argv": self.pipeline_argv(project, chosen)})

        def running(current: Project) -> None:
            current.settings = chosen
            current.run = {
                **current.run,
                "status": "running",
                "job_id": job.id,
                "error": None,
                "started_at": utc_now(),
                "finished_at": None,
            }

        self.store.update(project.id, running)
        environment = self.worker_environment(chosen)
        try:
            job.process = (
                spawn_worker(request_path, environment)
                if environment
                else spawn_worker(request_path)
            )
        except OSError as error:
            job.append({"type": "error", "message": f"cannot start VoiceCut: {error}"})
            self._finish_voicecut(job)
            raise HTTPException(500, f"cannot start VoiceCut: {error}") from error
        threading.Thread(
            target=self._supervise, args=(job,), daemon=True, name=job.id
        ).start()
        return job

    def _supervise(self, job: Job) -> None:
        process = job.process
        assert process is not None and process.stdout and process.stderr
        terminal: list[dict[str, Any]] = []
        stderr_tail: list[str] = []

        def read_events() -> None:
            for line in process.stdout:
                with contextlib.suppress(json.JSONDecodeError):
                    event = json.loads(line)
                    if isinstance(event, dict) and isinstance(event.get("type"), str):
                        if event["type"] in {"complete", "error", "cancelled"}:
                            terminal.append(event)
                        else:
                            job.append(event)
                        continue
                if line.strip():
                    job.append(
                        {"type": "log", "stream": "stdout", "line": line.rstrip()}
                    )

        def read_stderr() -> None:
            for line in process.stderr:
                text = line.rstrip("\r\n")
                stderr_tail[:] = [*stderr_tail[-19:], text]
                job.append({"type": "log", "stream": "stderr", "line": text})

        readers = [
            threading.Thread(target=read_events),
            threading.Thread(target=read_stderr),
        ]
        for reader in readers:
            reader.start()
        code = process.wait()
        for reader in readers:
            reader.join()
        # The terminal event goes last so no log line arrives after it.
        if terminal:
            job.append(terminal[0])
        elif job.cancel_requested or code in (130, -signal.SIGTERM):
            job.append({"type": "cancelled"})
        else:
            detail = next((line for line in reversed(stderr_tail) if line.strip()), "")
            job.append(
                {
                    "type": "error",
                    "message": f"VoiceCut stopped with exit status {code}"
                    + (f": {detail}" if detail else ""),
                }
            )
        self._finish_voicecut(job)

    def _finish_voicecut(self, job: Job) -> None:
        result = job.result or {}

        def apply(project: Project) -> None:
            if project.run.get("job_id") != job.id:
                return
            run = {**project.run, "job_id": None, "finished_at": utc_now()}
            if job.status == "complete":
                run.update(
                    status="complete",
                    work_dir=result.get("work_dir"),
                    output=result.get("output"),
                    warnings=list(result.get("pipeline_warnings") or []),
                    error=None,
                )
            elif job.status == "cancelled":
                run.update(status="cancelled", error=None)
            else:
                run.update(status="failed", error=job.error)
            project.run = run

        with contextlib.suppress(ProjectNotFound):
            project = self.store.update(job.project_id, apply)
            if job.status == "complete":
                self._adopt_canonical_audio(project)

    def _adopt_canonical_audio(self, project: Project) -> None:
        """Make sure the editor's WAV is the one VoiceCut's plan indexes."""

        try:
            proposal = self.proposal(project)
        except (TimelineError, OSError, ValueError, KeyError):
            return
        if proposal is None:
            return
        manifest = project.media_manifest()
        if manifest.get("canonical_audio_sha256") == proposal.source_audio_sha256:
            if project.document:
                # The same audio twice in one document: share its blocks.
                with contextlib.suppress(OSError):
                    documents.clone_file(proposal.source_audio, project.canonical_wav)
            return
        pipeline_manifest = proposal.source_audio.parent / "media_input.json"
        if not pipeline_manifest.is_file():
            return
        shutil.copy2(proposal.source_audio, project.canonical_wav)
        adopted = read_json(pipeline_manifest)
        adopted["canonical_audio"] = str(project.canonical_wav.resolve())
        write_json_atomic(project.media_manifest_path, adopted)
        project.peaks_path.unlink(missing_ok=True)
        project.ensure_peaks()

    def cancel(self, project: Project) -> None:
        job = self.jobs.running(project.id, frozenset({"voicecut"}))
        if job is None:
            raise HTTPException(409, "Voice Cut is not running")
        job.cancel_requested = True
        self._cancel(job)

    # -- timeline + preview audio ------------------------------------------------------
    def room_tone(
        self, project: Project, proposal: Proposal | None
    ) -> plan.RoomToneSource:
        """Clean room tone for inserted or resized pauses, found on first use."""

        source: list[ambience.AmbienceRoomTone] = []

        def take(length: int, near: int, seed: str) -> list[tuple[int, int]]:
            if not source:
                runs = ambience.ambience_runs(
                    project.canonical_wav, proposal, project.media_dir / "ambience.json"
                )
                source.append(
                    ambience.AmbienceRoomTone(runs, project.audio_geometry()[0])
                )
            return source[0](length, near, seed)

        return take

    def save_timeline(self, project: Project, body: Any) -> dict[str, Any]:
        proposal = self.proposal(project)
        sample_rate, _, frames = project.audio_geometry()
        try:
            timeline = validate_timeline(
                body,
                source_frames=frames,
                proposal=proposal,
                allow_pauses=project.kind != "video",
                sample_rate=sample_rate,
            )
            derivation = plan.derive_pieces(
                timeline["items"],
                proposal,
                sample_rate=sample_rate,
                declick=plan.declick_samples(sample_rate),
                room_tone=self.room_tone(project, proposal),
            )
        except (TimelineError, plan.PlanError) as error:
            raise HTTPException(400, str(error)) from error
        revision = timeline_revision(timeline)
        with self._lock:
            self._previews[(project.id, revision)] = derivation
            self._previews.move_to_end((project.id, revision))
            while len(self._previews) > PREVIEW_CACHE_SIZE:
                self._previews.popitem(last=False)
        write_json_atomic(project.timeline_path, {**timeline, "saved_at": utc_now()})
        return {"revision": revision, "output_frames": derivation.output_frames}

    def render(
        self, project: Project, revision: str, start: int, frames: int
    ) -> Response:
        with self._lock:
            derivation = self._previews.get((project.id, revision))
        if derivation is None:
            raise HTTPException(
                409, "unknown timeline revision; save the timeline again"
            )
        if start < 0 or not 0 < frames <= MAX_RENDER_FRAMES:
            raise HTTPException(400, "invalid render range")
        with plan.SourceAudio(project.canonical_wav) as source:
            audio = plan.render_range(source, derivation, start, start + frames)
        return Response(
            content=audio.T.astype("<f4").tobytes(),
            media_type="application/octet-stream",
            headers={
                "X-Frames": str(len(audio)),
                "X-Channels": str(audio.shape[1]),
                "X-Output-Frames": str(derivation.output_frames),
                "Cache-Control": "no-store",
            },
        )

    # -- export ----------------------------------------------------------------------
    def start_export(self, project: Project, body: dict[str, Any]) -> dict[str, str]:
        proposal = self.proposal(project)
        try:
            timeline = validate_timeline(
                body.get("timeline"),
                source_frames=project.audio_geometry()[2],
                proposal=proposal,
                allow_pauses=project.kind != "video",
                sample_rate=project.audio_geometry()[0],
            )
        except TimelineError as error:
            raise HTTPException(400, str(error)) from error
        destination = body.get("destination")
        if not isinstance(destination, str) or not destination.strip():
            raise HTTPException(400, "choose where to save the export")
        overwrite = bool(body.get("overwrite"))
        try:
            check_destination(
                project, Path(destination.strip()).expanduser(), overwrite=overwrite
            )
        except FileExistsError as error:
            raise HTTPException(409, f"{error} already exists") from error
        except ExportError as error:
            raise HTTPException(400, str(error)) from error
        with self._lock:
            if self.jobs.running(project.id, EXCLUSIVE_JOBS):
                raise HTTPException(409, "Voice Cut or an export is already running")
            job = self.jobs.create("export", project.id)
        export_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"

        def run() -> None:
            try:
                record = export_timeline(
                    project=project,
                    proposal=proposal,
                    timeline=timeline,
                    room_tone=self.room_tone(project, proposal),
                    destination=Path(destination.strip()).expanduser(),
                    overwrite=overwrite,
                    export_id=export_id,
                    on_stage=lambda stage, status: job.stage(
                        stage, status, EXPORT_STAGES[stage]
                    ),
                )
            except Exception as error:  # noqa: BLE001 - reported to the browser
                job.append({"type": "error", "message": str(error) or repr(error)})
                return

            def remember(current: Project) -> None:
                current.exports = [record, *current.exports][:20]

            with contextlib.suppress(ProjectNotFound):
                self.store.update(project.id, remember)
            job.append({"type": "complete", "result": record})

        threading.Thread(target=run, daemon=True, name=job.id).start()
        return {"job_id": job.id, "export_id": export_id}

    @staticmethod
    def find_export(project: Project, export_id: str) -> dict[str, Any]:
        for record in project.exports:
            if record.get("export_id") == export_id:
                return record
        raise HTTPException(404, "export not found")


async def _json(request: Request) -> Any:
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(400, "the request body must be JSON") from error


def _local_host(value: str) -> bool:
    """Whether a ``Host`` header names this computer's loopback interface."""

    if value.startswith("["):  # [::1]:port
        host = value[1:].split("]", 1)[0]
    else:
        host = value.rsplit(":", 1)[0] if value.count(":") == 1 else value
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_app(
    *,
    projects_dir: Path,
    env_file: Path,
    defaults: dict[str, Any] | None = None,
    static_dir: Path | None = None,
    keys: settings.KeyStore | None = None,
    desktop: bool = False,
) -> FastAPI:
    store = (documents.DocumentStore if desktop else ProjectStore)(projects_dir)
    store.recover_interrupted()
    service = EditorService(
        store, env_file=env_file, defaults=defaults or {}, keys=keys
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        # Quitting the editor stops Voice Cut instead of orphaning its worker.
        await run_in_threadpool(service.stop_jobs)

    app = FastAPI(title="VoiceCut editor", version=__version__, lifespan=lifespan)
    app.state.service = service

    if desktop:

        @app.middleware("http")
        async def only_the_app(request: Request, call_next):
            """Refuse requests from web pages and DNS-rebound host names.

            The desktop engine listens on the loopback interface; anything but
            the app's own pages and the app itself is turned away.
            """

            host = request.headers.get("host", "")
            origin = request.headers.get("origin")
            if not _local_host(host) or (
                origin is not None and origin != f"http://{host}"
            ):
                return JSONResponse({"detail": "forbidden"}, status_code=403)
            return await call_next(request)

    @app.exception_handler(ProjectNotFound)
    async def not_found(_: Request, error: ProjectNotFound) -> JSONResponse:
        return JSONResponse({"detail": "project not found"}, status_code=404)

    @app.exception_handler(ProjectError)
    async def project_error(_: Request, error: ProjectError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {
            "version": __version__,
            "desktop": desktop,
            "languages": list(SUPPORTED_LANGUAGE_CODES),
            "planner_backends": list(PLANNER_BACKENDS),
            "defaults": {key: service.defaults.get(key) for key in SETTINGS_KEYS},
        }

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return service.settings_state()

    @app.put("/api/settings/keys/{backend}")
    async def put_key(backend: str, request: Request) -> dict[str, Any]:
        body = await _json(request)
        return await run_in_threadpool(lambda: service.save_key(backend, body))

    @app.get("/api/planners/{backend}/models")
    def planner_models(backend: str, refresh: bool = False) -> dict[str, Any]:
        return service.list_models(backend, refresh)

    @app.get("/api/projects")
    def list_projects() -> list[dict[str, Any]]:
        return [project.summary() for project in store.list()]

    @app.post("/api/projects/import")
    async def import_project(request: Request) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise HTTPException(400, "the form has no file")
            project = await run_in_threadpool(
                lambda: service.import_media(upload=upload, path=None)
            )
        else:
            body = await _json(request)
            path = body.get("path") if isinstance(body, dict) else None
            project = await run_in_threadpool(
                lambda: service.import_media(
                    upload=None, path=path if isinstance(path, str) else None
                )
            )
        return await run_in_threadpool(lambda: service.state(project))

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        return service.state(service.project(project_id))

    @app.delete("/api/projects/{project_id}", status_code=204)
    def delete_project(project_id: str) -> Response:
        service.delete(service.project(project_id))
        return Response(status_code=204)

    @app.get("/api/projects/{project_id}/peaks")
    def base_peaks(project_id: str) -> FileResponse:
        project = service.project(project_id)
        project.ensure_peaks()
        return FileResponse(
            project.peaks_path,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/projects/{project_id}/peaks/fine")
    def fine_peaks(project_id: str, spp: int, start: int, count: int) -> Response:
        project = service.project(project_id)
        try:
            data = peaks.fine_peaks(
                project.canonical_wav, spp=spp, first_bin=start, count=count
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        return Response(content=data, media_type="application/octet-stream")

    @app.get("/api/projects/{project_id}/proxy.mp4")
    def proxy_video(project_id: str) -> FileResponse:
        project = service.project(project_id)
        if not project.preview_ready():
            raise HTTPException(404, "the video preview is not ready")
        return FileResponse(project.proxy_path, media_type="video/mp4")

    @app.get("/api/projects/{project_id}/filmstrip/{sheet}")
    def filmstrip(project_id: str, sheet: int) -> FileResponse:
        project = service.project(project_id)
        path = project.filmstrip_dir / f"sheet_{sheet:03d}.jpg"
        if not path.is_file():
            raise HTTPException(404, "no such filmstrip sheet")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/projects/{project_id}/prepare")
    def prepare(project_id: str) -> dict[str, Any]:
        job = service.start_prepare(service.project(project_id))
        return {"job_id": job.id if job else None}

    @app.post("/api/projects/{project_id}/voicecut")
    async def voicecut(project_id: str, request: Request) -> dict[str, str]:
        body = await _json(request)
        project = service.project(project_id)
        job = service.start_voicecut(project, body if isinstance(body, dict) else {})
        return {"job_id": job.id}

    @app.put("/api/projects/{project_id}/settings")
    async def put_settings(project_id: str, request: Request) -> dict[str, Any]:
        body = await _json(request)
        project = service.project(project_id)
        if not isinstance(body, dict):
            raise HTTPException(400, "the request body must be an object")
        return service.save_settings(project, body)

    @app.post("/api/projects/{project_id}/cancel")
    def cancel(project_id: str) -> dict[str, str]:
        service.cancel(service.project(project_id))
        return {"status": "cancelling"}

    @app.put("/api/projects/{project_id}/timeline")
    async def put_timeline(project_id: str, request: Request) -> dict[str, Any]:
        body = await _json(request)
        project = service.project(project_id)
        return await run_in_threadpool(lambda: service.save_timeline(project, body))

    @app.get("/api/projects/{project_id}/render")
    def render(project_id: str, rev: str, start: int, frames: int) -> Response:
        return service.render(service.project(project_id), rev, start, frames)

    @app.post("/api/projects/{project_id}/export")
    async def export(project_id: str, request: Request) -> dict[str, str]:
        body = await _json(request)
        project = service.project(project_id)
        if not isinstance(body, dict):
            raise HTTPException(400, "the request body must be an object")
        return await run_in_threadpool(lambda: service.start_export(project, body))

    @app.get("/api/projects/{project_id}/exports/{export_id}/download")
    def download(project_id: str, export_id: str) -> FileResponse:
        record = service.find_export(service.project(project_id), export_id)
        path = Path(record["output_path"])
        if not path.is_file():
            raise HTTPException(404, "the exported file no longer exists")
        return FileResponse(path, filename=path.name)

    @app.post("/api/projects/{project_id}/exports/{export_id}/reveal")
    def reveal(project_id: str, export_id: str) -> dict[str, bool]:
        record = service.find_export(service.project(project_id), export_id)
        path = Path(record["output_path"])
        if not path.exists():
            raise HTTPException(404, "the exported file no longer exists")
        if sys.platform == "darwin":
            command = ["open", "-R", str(path)]
        elif sys.platform == "win32":
            command = ["explorer", f"/select,{path}"]
        else:
            command = ["xdg-open", str(path.parent)]
        with contextlib.suppress(OSError):
            subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return {"ok": True}
        return {"ok": False}

    @app.post("/api/projects/{project_id}/locate-source")
    async def locate_source(project_id: str, request: Request) -> dict[str, Any]:
        body = await _json(request)
        path = body.get("path") if isinstance(body, dict) else None
        project = service.project(project_id)
        located = await run_in_threadpool(
            lambda: service.locate_source(
                project, path if isinstance(path, str) else None
            )
        )
        return await run_in_threadpool(lambda: service.state(located))

    if desktop:

        @app.post("/api/documents/open")
        async def open_document(request: Request) -> dict[str, Any]:
            body = await _json(request)
            path = body.get("path") if isinstance(body, dict) else None
            project = await run_in_threadpool(
                lambda: service.open_document(path if isinstance(path, str) else None)
            )
            return await run_in_threadpool(lambda: service.state(project))

        @app.post("/api/documents/create")
        async def create_document(request: Request) -> dict[str, Any]:
            body = await _json(request) or {}
            if not isinstance(body, dict):
                raise HTTPException(400, "the request body must be an object")
            source, path = body.get("source"), body.get("path")
            project = await run_in_threadpool(
                lambda: service.create_document(
                    source if isinstance(source, str) else None,
                    path if isinstance(path, str) else None,
                )
            )
            return await run_in_threadpool(lambda: service.state(project))

        @app.post("/api/projects/{project_id}/save-as")
        async def save_as(project_id: str, request: Request) -> dict[str, Any]:
            body = await _json(request)
            path = body.get("path") if isinstance(body, dict) else None
            project = service.project(project_id)
            saved = await run_in_threadpool(
                lambda: service.save_document_as(
                    project, path if isinstance(path, str) else None
                )
            )
            return await run_in_threadpool(lambda: service.state(saved))

        @app.post("/api/projects/{project_id}/close", status_code=204)
        def close_document(project_id: str, cancel: bool = False) -> Response:
            service.close_document(service.project(project_id), cancel=cancel)
            return Response(status_code=204)

        @app.post("/api/desktop/stop-jobs")
        def stop_all_jobs() -> dict[str, bool]:
            service.stop_jobs()
            return {"ok": True}

    @app.get("/api/jobs/{job_id}")
    def job_state(job_id: str) -> dict[str, Any]:
        job = service.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job.to_dict()

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str, since: int = 0) -> StreamingResponse:
        job = service.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return StreamingResponse(
            sse_stream(job, since),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if static_dir is not None:
        index = static_dir / "index.html"

        @app.get("/{path:path}", include_in_schema=False)
        def frontend(path: str) -> Response:
            if path.startswith("api/"):
                raise HTTPException(404, "not found")
            candidate = (static_dir / path).resolve()
            if (
                path
                and candidate.is_file()
                and candidate.is_relative_to(static_dir.resolve())
            ):
                return FileResponse(candidate)
            if index.is_file():
                return FileResponse(index, headers={"Cache-Control": "no-cache"})
            return HTMLResponse(FRONTEND_MISSING)

    return app
