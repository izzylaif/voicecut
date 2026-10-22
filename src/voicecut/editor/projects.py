"""Editor projects: one imported recording and everything derived from it.

```
<projects>/<id>/
  project.json            settings, Voice Cut run state, preview state, exports
  source/<file>           an uploaded file (files opened by path stay in place)
  media/                  canonical WAV + media_input.json, waveform peaks,
                          video proxy and filmstrip sheets
  voicecut/               the pipeline's own output and work directory
  timeline.json           the user's edit (autosaved)
  exports/<export_id>/    editor plans and renders of finished exports
```

``project.json`` is rewritten atomically; the store serialises updates.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import soundfile as sf

from .. import media
from ..common import read_json, sha256_file
from . import peaks

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROJECTS_DIR = PROJECT_ROOT / ".voicecut-cache" / "editor"
PROJECT_SCHEMA_VERSION = 2
SETTINGS_KEYS = ("language", "planner_backend", "planner_model", "breath_cleanup")
DEFAULT_SETTINGS: dict[str, Any] = {
    "language": "en",
    "planner_backend": "gemini",
    "planner_model": None,
    "breath_cleanup": None,
}
IMPORTABLE_EXTENSIONS = media.AUDIO_INPUT_EXTENSIONS | media.VIDEO_INPUT_EXTENSIONS


class ProjectError(RuntimeError):
    """A project cannot be created, read or updated."""


class ProjectNotFound(ProjectError):
    """No project exists with the requested id."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def safe_name(value: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return compact[:48] or "media"


def edited_name(source: Path) -> str:
    """``<stem>_edited<ext>``, VoiceCut's naming for its own output."""

    extension = source.suffix.lower()
    if extension not in media.AUDIO_OUTPUT_EXTENSIONS | media.VIDEO_OUTPUT_EXTENSIONS:
        extension = ".mp4" if extension in media.VIDEO_INPUT_EXTENSIONS else ".wav"
    return f"{source.stem}_edited{extension}"


def idle_run() -> dict[str, Any]:
    return {
        "status": "idle",
        "job_id": None,
        "work_dir": None,
        "output": None,
        "error": None,
        "warnings": [],
        "started_at": None,
        "finished_at": None,
    }


@dataclass
class Project:
    id: str
    root: Path
    name: str
    kind: str
    source_path: Path
    source_sha256: str
    uploaded: bool
    created_at: str
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    run: dict[str, Any] = field(default_factory=idle_run)
    preview: dict[str, Any] = field(default_factory=dict)
    exports: list[dict[str, Any]] = field(default_factory=list)

    # -- paths -------------------------------------------------------------
    @property
    def project_json(self) -> Path:
        return self.root / "project.json"

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def canonical_wav(self) -> Path:
        return self.media_dir / "source_audio.wav"

    @property
    def media_manifest_path(self) -> Path:
        return self.media_dir / "media_input.json"

    @property
    def peaks_path(self) -> Path:
        return self.media_dir / f"peaks_{peaks.BASE_SPP}.bin"

    @property
    def proxy_path(self) -> Path:
        return self.media_dir / "proxy.mp4"

    @property
    def filmstrip_dir(self) -> Path:
        return self.media_dir / "filmstrip"

    @property
    def voicecut_dir(self) -> Path:
        return self.root / "voicecut"

    @property
    def pipeline_output(self) -> Path:
        return self.voicecut_dir / edited_name(self.source_path)

    @property
    def timeline_path(self) -> Path:
        return self.root / "timeline.json"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def plan_path(self) -> Path | None:
        """VoiceCut's sealed plan of the last successful run, if any.

        ``work_dir`` changes only when a run completes, so a later run that
        fails or is cancelled keeps the previous result editable.
        """

        work_dir = self.run.get("work_dir")
        if not work_dir:
            return None
        path = Path(work_dir) / "05_final" / "final_boundary_plan.json"
        return path if path.is_file() else None

    def default_export_path(self) -> Path:
        name = edited_name(self.source_path)
        if not self.uploaded:
            return self.source_path.with_name(name)
        downloads = Path.home() / "Downloads"
        return (downloads if downloads.is_dir() else self.exports_dir) / name

    # -- media ---------------------------------------------------------------
    def media_manifest(self) -> dict[str, Any]:
        value = read_json(self.media_manifest_path)
        if not isinstance(value, dict):
            raise ProjectError(f"invalid media manifest: {self.media_manifest_path}")
        return value

    def audio_geometry(self) -> tuple[int, int, int]:
        """``(sample_rate, channels, frames)`` of the canonical WAV."""

        info = sf.info(str(self.canonical_wav))
        return int(info.samplerate), int(info.channels), int(info.frames)

    def video_offset(self) -> float:
        """Proxy time of audio time 0 (the proxy starts at the first frame)."""

        info = self.media_manifest().get("source_media_info") or {}
        audio = info.get("audio_stream") or {}
        video = info.get("video_stream") or {}
        return float(audio.get("start_seconds") or 0.0) - float(
            video.get("start_seconds") or 0.0
        )

    def ensure_peaks(self) -> None:
        if not self.peaks_path.is_file():
            peaks.compute_base_peaks(self.canonical_wav, self.peaks_path)

    def preview_ready(self) -> bool:
        return self.preview.get("status") == "ready" and self.proxy_path.is_file()

    def media_state(self) -> dict[str, Any]:
        sample_rate, channels, frames = self.audio_geometry()
        state: dict[str, Any] = {
            "kind": self.kind,
            "sample_rate": sample_rate,
            "channels": channels,
            "frames": frames,
            "duration": frames / sample_rate,
            "peaks": {
                "url": f"/api/projects/{self.id}/peaks",
                "spp": peaks.BASE_SPP,
                "bins": -(-frames // peaks.BASE_SPP),
            },
            "video": None,
        }
        if self.kind != "video":
            return state
        stream = (self.media_manifest().get("source_media_info") or {}).get(
            "video_stream"
        ) or {}
        ready = self.preview_ready()
        proxy = self.preview.get("proxy") if ready else None
        filmstrip = self.preview.get("filmstrip") if ready else None
        version = self.preview.get("version", 0)
        state["video"] = {
            "status": "ready" if ready else self.preview.get("status", "pending"),
            "progress": self.preview.get("progress"),
            "error": self.preview.get("error"),
            "width": proxy["width"] if proxy else stream.get("width"),
            "height": proxy["height"] if proxy else stream.get("height"),
            "fps": stream.get("average_frame_rate"),
            "offset": self.video_offset(),
            "proxy_url": (
                f"/api/projects/{self.id}/proxy.mp4?v={version}" if proxy else None
            ),
            "filmstrip": (
                {
                    **filmstrip,
                    "url": f"/api/projects/{self.id}/filmstrip/{{sheet}}?v={version}",
                }
                if filmstrip
                else None
            ),
        }
        return state

    # -- persistence -----------------------------------------------------------
    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "source_path": str(self.source_path),
            "source_sha256": self.source_sha256,
            "uploaded": self.uploaded,
            "created_at": self.created_at,
            "settings": dict(self.settings),
            "run": dict(self.run),
            "preview": dict(self.preview),
            "exports": list(self.exports),
        }

    @classmethod
    def from_record(cls, root: Path, record: dict[str, Any]) -> Project:
        settings = dict(DEFAULT_SETTINGS)
        stored = record.get("settings")
        if isinstance(stored, dict):
            settings.update(
                {key: stored[key] for key in SETTINGS_KEYS if key in stored}
            )
        run = idle_run()
        if isinstance(record.get("run"), dict):
            run.update(record["run"])
        preview = record.get("preview")
        exports = record.get("exports")
        return cls(
            id=str(record["id"]),
            root=root,
            name=str(record["name"]),
            kind=str(record["kind"]),
            source_path=Path(str(record["source_path"])),
            source_sha256=str(record["source_sha256"]),
            uploaded=bool(record.get("uploaded", False)),
            created_at=str(record.get("created_at") or utc_now()),
            settings=settings,
            run=run,
            # Records of the previous editor kept viewer preferences here.
            preview=dict(preview)
            if isinstance(preview, dict) and "status" in preview
            else {},
            exports=list(exports) if isinstance(exports, list) else [],
        )

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "created_at": self.created_at,
            "run_status": self.run.get("status", "idle"),
            "source_path": str(self.source_path),
        }


class ProjectStore:
    """Create, read, update and delete projects under one directory."""

    def __init__(self, root: Path, *, defaults: dict[str, Any] | None = None) -> None:
        self.root = root.expanduser().resolve()
        self._lock = threading.RLock()
        # Voice Cut settings a new project starts with.
        self.defaults = {**DEFAULT_SETTINGS, **(defaults or {})}

    def _dir(self, project_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project_id or ""):
            raise ProjectNotFound(project_id)
        return self.root / project_id

    def get(self, project_id: str) -> Project:
        root = self._dir(project_id)
        path = root / "project.json"
        if not path.is_file():
            raise ProjectNotFound(project_id)
        record = read_json(path)
        if not isinstance(record, dict):
            raise ProjectError(f"invalid project record: {path}")
        return Project.from_record(root, record)

    def list(self) -> list[Project]:
        if not self.root.is_dir():
            return []
        projects = []
        for entry in self.root.iterdir():
            if (entry / "project.json").is_file():
                with contextlib.suppress(ProjectError, OSError, ValueError, KeyError):
                    projects.append(self.get(entry.name))
        return sorted(projects, key=lambda project: project.created_at, reverse=True)

    def save(self, project: Project) -> None:
        with self._lock:
            write_json_atomic(project.project_json, project.to_record())

    def update(self, project_id: str, mutate: Callable[[Project], None]) -> Project:
        """Re-read, mutate and save a project under the store lock."""

        with self._lock:
            project = self.get(project_id)
            mutate(project)
            write_json_atomic(project.project_json, project.to_record())
            return project

    def delete(self, project_id: str) -> None:
        root = self._dir(project_id)
        if not (root / "project.json").is_file():
            raise ProjectNotFound(project_id)
        shutil.rmtree(root)

    def recover_interrupted(self) -> None:
        """A previous server's runs and preparations cannot be resumed."""

        def fix(project: Project) -> None:
            if project.run.get("status") == "running":
                project.run.update(
                    status="failed",
                    job_id=None,
                    error="the editor server stopped while Voice Cut was running",
                )
            if project.preview.get("status") == "preparing":
                project.preview["status"] = "pending"

        for project in self.list():
            self.update(project.id, fix)

    # -- import ----------------------------------------------------------------
    def import_upload(self, name: str, stream: IO[bytes]) -> Project:
        filename = Path(name).name
        if not filename or filename.startswith("."):
            raise ProjectError("the upload has no usable file name")
        if Path(filename).suffix.lower() not in IMPORTABLE_EXTENSIONS:
            raise ProjectError(f"unsupported media type: {filename}")
        incoming = self.root / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=incoming, delete=False) as handle:
            temporary = Path(handle.name)
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
                handle.write(chunk)
        try:
            sha = digest.hexdigest()
            project_id = f"{safe_name(Path(filename).stem)}-{sha[:8]}"
            with self._lock:
                with contextlib.suppress(ProjectNotFound):
                    return self.get(project_id)
                root = self._dir(project_id)
                source = root / "source" / filename
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(temporary), source)
                return self._create(project_id, root, filename, source, sha, True)
        finally:
            temporary.unlink(missing_ok=True)

    def import_path(self, path: Path) -> Project:
        source = path.expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() not in IMPORTABLE_EXTENSIONS:
            raise ProjectError(f"unsupported media type: {source.name}")
        sha = sha256_file(source)
        project_id = f"{safe_name(source.stem)}-{sha[:8]}"
        with self._lock:
            with contextlib.suppress(ProjectNotFound):
                return self.get(project_id)
            root = self._dir(project_id)
            return self._create(project_id, root, source.name, source, sha, False)

    def _create(
        self,
        project_id: str,
        root: Path,
        name: str,
        source: Path,
        sha: str,
        uploaded: bool,
    ) -> Project:
        try:
            root.mkdir(parents=True, exist_ok=True)
            project = Project(
                id=project_id,
                root=root,
                name=name,
                kind="audio",
                source_path=source,
                source_sha256=sha,
                uploaded=uploaded,
                created_at=utc_now(),
                settings={key: self.defaults.get(key) for key in SETTINGS_KEYS},
            )
            try:
                manifest = media.extract_audio_to_wav(
                    source, project.canonical_wav, overwrite=True
                )
            except media.MediaError as error:
                raise ProjectError(f"cannot read the media: {error}") from error
            write_json_atomic(project.media_manifest_path, manifest)
            project.kind = str(manifest.get("source_kind") or "audio")
            if project.kind == "video":
                project.preview = {"status": "pending"}
            project.ensure_peaks()
            self.save(project)
            return project
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
