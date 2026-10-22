"""Export an editor timeline to a finished audio or video file.

An untouched VoiceCut result is copied from the pipeline's own publication
when the container matches.  Anything else is derived into an editor plan
(:mod:`.plan`), rendered once from the canonical WAV with the pipeline's
overlap-add renderer and published with VoiceCut's own publishers:
``media.publish_audio`` for audio and ``video_render.render_edited_video``
(direct cuts from the original video) for video.

The render (``final_cut.wav``, as long as the edit in 32-bit float) is only
the publishers' input: it is deleted once the export is written, or fails, so
a project does not grow by a recording's worth with every export.  The
manifests beside it keep its SHA-256.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import media, video_render
from ..common import sha256_file, write_json
from .plan import RoomToneSource, build_editor_plan, render_plan_to_wav
from .projects import Project, utc_now
from .timeline import Proposal, matches_proposal

EDITOR_RENDERER_ID = "voicecut_editor_render_v2"
EXPORT_STAGES = {
    "plan": "Deriving the edit",
    "render": "Rendering audio",
    "publish": "Writing the file",
}

StageCallback = Callable[[str, str], None]


class ExportError(RuntimeError):
    """An export cannot be produced safely."""


def output_kind(destination: Path, project_kind: str) -> str:
    extension = destination.suffix.lower()
    if extension in media.AUDIO_OUTPUT_EXTENSIONS:
        return "audio"
    if project_kind == "video" and extension in media.VIDEO_OUTPUT_EXTENSIONS:
        return "video"
    allowed = sorted(
        media.AUDIO_OUTPUT_EXTENSIONS
        | (media.VIDEO_OUTPUT_EXTENSIONS if project_kind == "video" else frozenset())
    )
    raise ExportError(
        f"unsupported file type {extension or '(none)'}; use one of {', '.join(allowed)}"
    )


def check_destination(project: Project, destination: Path, *, overwrite: bool) -> Path:
    """Validate an export destination and return it as an absolute path."""

    if not destination.is_absolute():
        raise ExportError("the destination must be an absolute path")
    if destination.is_symlink():
        raise ExportError("the destination must not be a symbolic link")
    resolved = destination.resolve()
    output_kind(resolved, project.kind)
    if resolved == project.source_path.resolve():
        raise ExportError("the export cannot replace the source media")
    for protected in (project.media_dir, project.voicecut_dir):
        if resolved.is_relative_to(protected.resolve()):
            raise ExportError(
                "the export cannot be written into the project's own media"
            )
    if resolved.exists() and not overwrite:
        raise FileExistsError(resolved)
    if not resolved.parent.is_dir():
        raise ExportError(f"the folder {resolved.parent} does not exist")
    return resolved


def _copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.voicecut-export.tmp")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def export_timeline(
    *,
    project: Project,
    proposal: Proposal | None,
    timeline: dict[str, Any],
    destination: Path,
    overwrite: bool,
    export_id: str,
    on_stage: StageCallback | None = None,
    room_tone: RoomToneSource | None = None,
    keep_render: bool = False,
) -> dict[str, Any]:
    """Export a validated timeline; return the export record.

    ``keep_render`` keeps the intermediate render (for tests).
    """

    def stage(name: str, status: str) -> None:
        if on_stage is not None:
            on_stage(name, status)

    destination = check_destination(project, destination, overwrite=overwrite)
    kind = output_kind(destination, project.kind)
    export_dir = project.exports_dir / export_id
    export_dir.mkdir(parents=True, exist_ok=True)
    sample_rate, channels, frames = project.audio_geometry()
    result: dict[str, Any] = {
        "export_id": export_id,
        "status": "complete",
        "output_path": str(destination),
        "output_kind": kind,
        "created_at": utc_now(),
        "reused_voicecut_output": False,
        "export_dir": str(export_dir),
    }

    original = project.pipeline_output
    if (
        matches_proposal(timeline, proposal)
        and original.is_file()
        and original.suffix.lower() == destination.suffix.lower()
    ):
        assert proposal is not None
        stage("publish", "started")
        _copy(original, destination)
        stage("publish", "completed")
        result.update(
            reused_voicecut_output=True,
            duration_seconds=proposal.output_frames / proposal.sample_rate,
            manual_boundaries=0,
            kept_pauses=sum(
                1 for item in proposal.timeline_items() if item["kind"] == "pause"
            ),
            dropped_pauses=0,
            inserted_pauses=0,
        )
        write_json(export_dir / "export_result.json", result)
        return result

    stage("plan", "started")
    manifest = project.media_manifest()
    canonical = Path(str(manifest.get("canonical_audio"))).resolve()
    canonical_sha = str(manifest.get("canonical_audio_sha256"))
    if canonical != project.canonical_wav.resolve():
        raise ExportError("the project's media manifest points at another WAV")
    if proposal is not None and proposal.source_audio_sha256 != canonical_sha:
        raise ExportError("Voice Cut's plan was made from different audio")
    plan = build_editor_plan(
        timeline=timeline,
        proposal=proposal,
        media_kind=project.kind,
        source_audio=canonical,
        source_audio_sha256=canonical_sha,
        sample_rate=sample_rate,
        channels=channels,
        source_frames=frames,
        room_tone=room_tone,
    )
    plan_path = export_dir / "editor_boundary_plan.json"
    write_json(plan_path, plan)
    stage("plan", "completed")

    final_cut = export_dir / "final_cut.wav"
    try:
        rendered = _render_and_publish(
            project=project,
            kind=kind,
            plan=plan,
            plan_path=plan_path,
            canonical=canonical,
            canonical_sha=canonical_sha,
            final_cut=final_cut,
            destination=destination,
            stage=stage,
        )
    finally:
        if not keep_render:
            final_cut.unlink(missing_ok=True)
    result.update(
        duration_seconds=rendered["duration_seconds"],
        manual_boundaries=plan["manual_boundary_count"],
        kept_pauses=plan["carried_over"]["pauses"],
        dropped_pauses=len(plan["dropped"]),
        inserted_pauses=plan["carried_over"]["inserted_pauses"],
    )
    write_json(export_dir / "export_result.json", result)
    return result


def _render_and_publish(
    *,
    project: Project,
    kind: str,
    plan: dict[str, Any],
    plan_path: Path,
    canonical: Path,
    canonical_sha: str,
    final_cut: Path,
    destination: Path,
    stage: StageCallback,
) -> dict[str, Any]:
    """Render the editor plan to ``final_cut`` and publish it to ``destination``."""

    export_dir = final_cut.parent
    stage("render", "started")
    rendered = render_plan_to_wav(
        audio_path=canonical, plan=plan, output_path=final_cut
    )
    render_manifest = {
        "schema_version": 1,
        "renderer": EDITOR_RENDERER_ID,
        "status": "complete",
        "source_audio": str(canonical),
        "source_audio_sha256": canonical_sha,
        "final_boundary_plan": str(plan_path.resolve()),
        "final_boundary_plan_sha256": sha256_file(plan_path),
        "final_cut_wav": str(final_cut.resolve()),
        "final_cut_wav_sha256": sha256_file(final_cut),
        **rendered,
    }
    render_manifest_path = export_dir / "editor_render_manifest.json"
    write_json(render_manifest_path, render_manifest)
    stage("render", "completed")

    stage("publish", "started")
    if kind == "video":
        video_render.render_edited_video(
            source_video=project.source_path.resolve(),
            media_input_manifest_path=project.media_manifest_path,
            final_render_manifest_path=render_manifest_path,
            output_path=destination,
            manifest_path=export_dir / "video_render_manifest.json",
            overwrite=True,
        )
    else:
        media.publish_audio(
            final_cut,
            destination,
            manifest_path=export_dir / "audio_publish_manifest.json",
            overwrite=True,
        )
    stage("publish", "completed")
    return rendered
