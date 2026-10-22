"""VoiceCut documents: an editor project saved as one ``.vc`` file.

The desktop app keeps every project where the user saved it, in a folder
named ``<name>.vc`` that macOS shows as a single file (a document package,
like ``.fcpbundle``)::

  <name>.vc/
    project.json        settings, Voice Cut run state, exports, and where the
                        document and its source recording were last seen
    media/              canonical WAV + media_input.json, waveform peaks,
                        video proxy and filmstrip sheets
    timeline.json       the user's edit (autosaved)
    voicecut/           the pipeline's output and its work directory
                        (``.voicecut-cache/<run>/``: canonical media,
                        transcripts, take plan, boundary plan, MFA output)
    exports/  jobs/

That is the layout of an editor project (:mod:`.projects`), so the editor
works on a document in place: the timeline autosaves into it, Voice Cut
writes its stages into it, and reopening it finds the finished run without
running anything again.

The source recording is referenced, not copied: it can be a multi-gigabyte
video, and the document already holds the audio the editor plays, edits and
exports.  Only a Voice Cut run and a video export read the source.  When it
has moved, the document looks for it beside itself (by its path relative to
the document, then by name) and accepts a file only if its SHA-256 matches;
otherwise the editor asks the user to locate it.

A document records the folder it was last opened from.  Opened from anywhere
else (moved or copied in the Finder, or saved with Save As), the absolute
paths the editor keeps in ``project.json`` and ``media/media_input.json`` are
rebased onto the new location.  The pipeline's own stage files are left
alone: they are sealed by hashes and matter only to a later run.

The format is the same on every system.  macOS shows the folder as one file;
Linux and Windows show a folder, so a document also holds ``<name>.vcproj``,
a small file the system associates with VoiceCut: double-clicking it opens
the document around it.  Nothing else is specific to a system except the APFS
clone that makes Save As instant on macOS.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

from ..common import read_json, sha256_file
from .projects import (
    IMPORTABLE_EXTENSIONS,
    Project,
    ProjectError,
    ProjectStore,
    safe_name,
    write_json_atomic,
)

DOCUMENT_EXTENSION = ".vc"
LAUNCHER_EXTENSION = ".vcproj"
DOCUMENT_SCHEMA_VERSION = 1
PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Files whose absolute paths are rebased when a document moves.
REBASED_FILES = (
    "project.json",
    "media/media_input.json",
    "voicecut/parts/final_boundary_plan.json",
)


def _clonefile(source: Path, destination: Path) -> bool:
    """Clone a file or a whole tree with APFS ``clonefile(2)``."""

    if sys.platform != "darwin":
        return False
    try:
        clonefile = ctypes.CDLL(None, use_errno=True).clonefile
    except (OSError, AttributeError):
        return False
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    clonefile.restype = ctypes.c_int
    return clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0


def copy_tree(source: Path, destination: Path) -> None:
    """Copy a directory tree: instant and sharing its blocks on APFS."""

    if destination.exists():
        raise FileExistsError(destination)
    if _clonefile(source, destination):
        return
    # A failed clone may leave a partial tree behind; it is ours to remove.
    shutil.rmtree(destination, ignore_errors=True)
    try:
        shutil.copytree(source, destination, symlinks=True)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def clone_file(source: Path, destination: Path) -> bool:
    """Replace ``destination`` with an APFS clone of ``source`` if possible."""

    temporary = destination.with_name(f".{destination.name}.clone")
    temporary.unlink(missing_ok=True)
    if not _clonefile(source, temporary):
        temporary.unlink(missing_ok=True)
        return False
    os.replace(temporary, destination)
    return True


def rebase(value: Any, old: str, new: str) -> Any:
    """Replace the path prefix ``old`` with ``new`` in every string of ``value``.

    A document made on another system keeps that system's separators after
    the prefix; they become this system's.
    """

    if isinstance(value, str):
        if value == old:
            return new
        if value.startswith(old) and value[len(old) : len(old) + 1] in ("/", "\\"):
            rest = value[len(old) :]
            return new + rest.replace(rest[0], os.sep)
        return value
    if isinstance(value, list):
        return [rebase(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: rebase(item, old, new) for key, item in value.items()}
    return value


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _previous_root(root: Path, record: dict[str, Any]) -> Path | None:
    """Where the document was last opened from.

    Projects of the browser editor do not say; their canonical WAV was
    ``<root>/media/source_audio.wav`` when they were made.
    """

    document = record.get("document")
    if isinstance(document, dict) and isinstance(document.get("root"), str):
        return Path(document["root"])
    with contextlib.suppress(OSError, ValueError, TypeError):
        manifest = read_json(root / "media" / "media_input.json")
        canonical = (
            manifest.get("canonical_audio") if isinstance(manifest, dict) else None
        )
        if isinstance(canonical, str):
            return Path(canonical).parents[1]
    return None


def relocate(root: Path, previous: Path) -> None:
    """Rebase the editor's absolute paths from ``previous`` onto ``root``."""

    for relative in REBASED_FILES:
        path = root / relative
        if path.is_file():
            write_json_atomic(path, rebase(read_json(path), str(previous), str(root)))


def write_launcher(root: Path) -> Path:
    """``<name>.vcproj``: what Linux and Windows users double-click."""

    launcher = root / f"{root.stem}{LAUNCHER_EXTENSION}"
    for stale in root.glob(f"*{LAUNCHER_EXTENSION}"):
        if stale != launcher:
            stale.unlink(missing_ok=True)
    if not launcher.is_file():
        launcher.write_text(
            json.dumps(
                {
                    "voicecut_document": DOCUMENT_SCHEMA_VERSION,
                    "about": "Open this file with VoiceCut. The project is the "
                    "folder it is in.",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return launcher


def document_root(path: Path) -> Path:
    """The document a path names: the folder itself, or the one around a launcher."""

    path = Path(path)
    if path.suffix.lower() == LAUNCHER_EXTENSION and path.is_file():
        return path.parent
    return path


def _remember_source(project: Project) -> None:
    """Record where the source is relative to the document, to find it later."""

    source = project.source_path
    if not source.is_file():
        return
    with contextlib.suppress(ValueError):  # another drive on Windows
        project.document["source_relative"] = os.path.relpath(
            source, project.root.parent
        )
    project.document["source_size"] = source.stat().st_size


def source_available(project: Project) -> bool:
    """Whether the source recording is where the project says (cheap check)."""

    source = project.source_path
    if not source.is_file():
        return False
    size = project.document.get("source_size")
    return not isinstance(size, int) or source.stat().st_size == size


def _is_source(project: Project, candidate: Path) -> bool:
    size = project.document.get("source_size")
    if isinstance(size, int) and candidate.stat().st_size != size:
        return False
    return sha256_file(candidate) == project.source_sha256


def _drop_export_renders(project: Project) -> None:
    """Delete renders older exports left behind (see :mod:`.export`)."""

    for render in project.exports_dir.glob("*/final_cut.wav"):
        render.unlink(missing_ok=True)


def _recover(project: Project) -> None:
    """A run or preview preparation of an earlier session cannot resume."""

    if project.run.get("status") == "running":
        project.run.update(
            status="failed",
            job_id=None,
            error="VoiceCut was closed while Voice Cut was running",
        )
    if project.preview.get("status") == "preparing":
        project.preview["status"] = "pending"


class DocumentStore(ProjectStore):
    """Editor projects that live in ``.vc`` documents anywhere on disk.

    Open documents are registered by project id, so every editor route works
    on them unchanged.  ``root`` (the editor's own directory) keeps the
    remembered settings and nothing else.
    """

    def __init__(self, root: Path, *, defaults: dict[str, Any] | None = None) -> None:
        super().__init__(root, defaults=defaults)
        self._documents: dict[str, Path] = {}

    def _dir(self, project_id: str) -> Path:
        with self._lock:
            root = self._documents.get(project_id)
        return root if root is not None else super()._dir(project_id)

    def is_document(self, project_id: str) -> bool:
        with self._lock:
            return project_id in self._documents

    def documents(self) -> dict[str, Path]:
        with self._lock:
            return dict(self._documents)

    def delete(self, project_id: str) -> None:
        if self.is_document(project_id):
            raise ProjectError("a document is deleted in the Finder")
        super().delete(project_id)

    def _fresh_id(self, base: str) -> str:
        """An id no open document or stored project uses (store lock held)."""

        base = base if PROJECT_ID.fullmatch(base) else safe_name(base)
        candidate = base
        while candidate in self._documents or (self.root / candidate).exists():
            candidate = f"{base}-{secrets.token_hex(2)}"
        return candidate

    @staticmethod
    def _root(path: Path) -> Path:
        """``path`` made absolute, with its folder (not the package) resolved."""

        path = Path(path).expanduser().absolute()
        if path.suffix.lower() != DOCUMENT_EXTENSION:
            raise ProjectError(f"a VoiceCut document name ends in {DOCUMENT_EXTENSION}")
        if not path.parent.is_dir():
            raise ProjectError(f"the folder {path.parent} does not exist")
        return path.parent.resolve() / path.name

    # -- open, create, save as, close -------------------------------------------
    def open_document(self, path: Path) -> Project:
        """Open (or return the already open) document at ``path``."""

        root = document_root(Path(path).expanduser()).resolve()
        if not (root / "project.json").is_file():
            raise ProjectError(f"{root.name} is not a VoiceCut project")
        with self._lock:
            for project_id, open_root in self._documents.items():
                if open_root == root:
                    return self.get(project_id)
            try:
                record = read_json(root / "project.json")
                previous = _previous_root(root, record)
                if previous is not None and previous != root:
                    relocate(root, previous)
                    record = read_json(root / "project.json")
                project = Project.from_record(root, record)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                raise ProjectError(f"{root.name} cannot be read: {error}") from error
            if (
                not PROJECT_ID.fullmatch(project.id)
                or project.id in self._documents
                or (self.root / project.id).exists()
            ):
                project.id = self._fresh_id(project.id)
            _recover(project)
            self._consolidate(project)
            _drop_export_renders(project)
            project.document.update(
                schema_version=DOCUMENT_SCHEMA_VERSION, root=str(root)
            )
            _remember_source(project)
            try:
                self.save(project)
                write_launcher(root)
            except OSError as error:
                raise ProjectError(f"cannot write to {root}: {error}") from error
            self._documents[project.id] = root
        self.find_moved_source(project.id)
        return self.get(project.id)

    def create_document(self, source: Path, document: Path) -> Project:
        """Make a new document at ``document`` from the recording ``source``."""

        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() not in IMPORTABLE_EXTENSIONS:
            raise ProjectError(f"unsupported media type: {source.name}")
        root = self._root(document)
        if root.exists():
            raise ProjectError(f"{root.name} already exists")
        sha = sha256_file(source)
        with self._lock:
            project_id = self._fresh_id(f"{safe_name(source.stem)}-{sha[:8]}")
            self._documents[project_id] = root
        info: dict[str, Any] = {
            "schema_version": DOCUMENT_SCHEMA_VERSION,
            "root": str(root),
        }
        with contextlib.suppress(ValueError):
            info["source_relative"] = os.path.relpath(source, root.parent)
        info["source_size"] = source.stat().st_size
        try:
            project = self._create(
                project_id, root, source.name, source, sha, False, document=info
            )
            write_launcher(root)
            return project
        except BaseException:
            with self._lock:
                self._documents.pop(project_id, None)
            raise

    def save_document_as(self, project_id: str, destination: Path) -> Project:
        """Copy a document to ``destination`` and keep editing the copy."""

        if not self.is_document(project_id):
            raise ProjectError("only a document can be saved under another name")
        target = self._root(destination)
        if target.exists():
            raise ProjectError(f"{target.name} already exists")
        with self._lock:
            project = self.get(project_id)
            if _inside(target, project.root):
                raise ProjectError("a document cannot be saved inside itself")
            copy_tree(project.root, target)
            try:
                relocate(target, project.root)
                copy = Project.from_record(target, read_json(target / "project.json"))
                copy.document.update(
                    schema_version=DOCUMENT_SCHEMA_VERSION, root=str(target)
                )
                _remember_source(copy)
                write_json_atomic(target / "project.json", copy.to_record())
                write_launcher(target)
            except BaseException:
                shutil.rmtree(target, ignore_errors=True)
                raise
            self._documents[project_id] = target
        return self.get(project_id)

    def close_document(self, project_id: str) -> None:
        with self._lock:
            self._documents.pop(project_id, None)

    # -- the source recording ----------------------------------------------------
    def find_moved_source(self, project_id: str) -> bool:
        """Relink a moved source found beside the document; whether it is there."""

        project = self.get(project_id)
        if source_available(project):
            return True
        folder = project.root.parent
        candidates = [folder / project.source_path.name]
        relative = project.document.get("source_relative")
        if isinstance(relative, str):
            candidates.insert(0, Path(os.path.normpath(folder / relative)))
        for candidate in candidates:
            if candidate.is_file() and _is_source(project, candidate):
                self._set_source(project_id, candidate)
                return True
        return False

    def locate_source(self, project_id: str, path: Path) -> Project:
        """Point the project at its source recording in a new place."""

        candidate = Path(path).expanduser().resolve()
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        project = self.get(project_id)
        if sha256_file(candidate) != project.source_sha256:
            raise ProjectError(
                f"{candidate.name} is not the original recording of this project"
            )
        return self._set_source(project_id, candidate)

    def _set_source(self, project_id: str, source: Path) -> Project:
        def apply(project: Project) -> None:
            project.source_path = source
            if self.is_document(project_id):
                _remember_source(project)

        return self.update(project_id, apply)

    # -- helpers -------------------------------------------------------------------
    @staticmethod
    def _consolidate(project: Project) -> None:
        """Bring a run's work directory kept outside the document inside it.

        Projects of the browser editor may point at a pipeline work directory
        anywhere; a document carries its own.
        """

        value = project.run.get("work_dir")
        if not value:
            return
        work = Path(value)
        if _inside(work, project.root) or not work.is_dir():
            return
        cache = project.voicecut_dir / ".voicecut-cache"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / work.name
        while target.exists():
            target = cache / f"{work.name}-{secrets.token_hex(2)}"
        copy_tree(work, target)
        project.run["work_dir"] = str(target)
