"""Projects, video previews and exports against real FFmpeg (voicecut.editor)."""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from voicecut.common import read_json
from voicecut.editor import ambience, proxy
from voicecut.editor.export import ExportError, check_destination, export_timeline
from voicecut.editor.plan import (
    SourceAudio,
    declick_samples,
    derive_pieces,
    render_range,
)
from voicecut.editor.projects import Project, ProjectStore, edited_name
from voicecut.editor.timeline import load_proposal, validate_timeline

from .editor_support import (
    FRAMES,
    SAMPLE_RATE,
    frame_at,
    make_video,
    needs_ffmpeg,
    render_voicecut,
    source_audio,
    write_wav,
)

pytestmark = needs_ffmpeg


def _complete_run(
    project: Project, store: ProjectStore, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    """Stand in for a finished Voice Cut run on the project's own WAV."""

    work = project.voicecut_dir / ".voicecut-cache" / "run"
    wav = work / "00_media" / "source_audio.wav"
    wav.parent.mkdir(parents=True)
    shutil.copy2(project.canonical_wav, wav)
    shutil.copy2(project.media_manifest_path, wav.parent / "media_input.json")
    render_voicecut(work, wav, monkeypatch=monkeypatch, policy=policy)

    def complete(current: Project) -> None:
        current.run.update(status="complete", work_dir=str(work))

    store.update(project.id, complete)


def _edited_timeline(project: Project) -> tuple[Any, dict[str, Any]]:
    """Extend the first clip; in audio projects also add a pause after it."""

    proposal = load_proposal(project.plan_path)
    items = proposal.timeline_items()
    first = next(index for index, item in enumerate(items) if item["kind"] == "clip")
    items[first] = {
        **items[first],
        "source_end_sample": items[first]["source_end_sample"] + 1600,
        "end_origin": "manual",
    }
    if project.kind == "audio":
        pause = {
            "kind": "pause",
            "id": "m-pause",
            "length": SAMPLE_RATE // 2,
            "origin": "manual",
            "join": None,
        }
        items.insert(first + 1, pause)
    timeline = validate_timeline(
        {"proposal_sha256": proposal.plan_sha256, "items": items},
        source_frames=FRAMES,
        proposal=proposal,
        allow_pauses=project.kind == "audio",
    )
    return proposal, timeline


def _room_tone(project: Project, proposal: Any) -> ambience.AmbienceRoomTone:
    runs = ambience.ambience_runs(
        project.canonical_wav, proposal, project.media_dir / "ambience.json"
    )
    return ambience.AmbienceRoomTone(runs, SAMPLE_RATE)


def test_import_by_path_and_upload_share_one_content_addressed_project(
    tmp_path: Path,
) -> None:
    source = write_wav(tmp_path / "My take.wav", source_audio())
    store = ProjectStore(tmp_path / "projects")
    project = store.import_path(source)
    assert project.id.startswith("My-take-")
    assert project.kind == "audio" and not project.uploaded
    assert project.audio_geometry() == (SAMPLE_RATE, 2, FRAMES)
    assert project.peaks_path.stat().st_size == 4 * -(-FRAMES // 256)
    assert store.import_path(source).created_at == project.created_at
    uploaded = store.import_upload("My take.wav", io.BytesIO(source.read_bytes()))
    assert uploaded.id == project.id  # same content: reopened, not duplicated
    assert [item.id for item in store.list()] == [project.id]
    assert project.default_export_path() == tmp_path / "My take_edited.wav"
    assert edited_name(Path("clip.MOV")) == "clip_edited.mov"
    assert edited_name(Path("voice.aiff")) == "voice_edited.aiff"


def test_interrupted_runs_are_marked_failed(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "projects")
    project = store.import_path(write_wav(tmp_path / "a.wav", source_audio()))
    store.update(
        project.id, lambda current: current.run.update(status="running", job_id="x")
    )
    store.recover_interrupted()
    run = store.get(project.id).run
    assert run["status"] == "failed" and "stopped" in run["error"]


@pytest.mark.parametrize(
    ("rotation", "hflip"), [(90, False), (-90, True), (180, False)]
)
def test_proxy_shows_the_picture_the_way_ffmpeg_exports_it(
    tmp_path: Path, rotation: int, hflip: bool
) -> None:
    audio = write_wav(tmp_path / "a.wav", source_audio())
    video = make_video(
        tmp_path / "phone.mov", audio, seconds=2, rotation=rotation, hflip=hflip
    )
    output = tmp_path / "proxy.mp4"
    info = proxy.build_proxy(video, output, duration=2.0)
    expected = frame_at(video, 0.5)  # what players and FFmpeg show
    shown = frame_at(output, 0.5)
    assert shown.shape == expected.shape
    assert (info["width"], info["height"]) == (shown.shape[1], shown.shape[0])
    # Compare the bright/red layout of both pictures (codec noise aside).
    assert np.mean(np.abs(shown.astype(int) - expected.astype(int))) < 12
    strip = proxy.build_filmstrip(
        output,
        tmp_path / "strip",
        width=info["width"],
        height=info["height"],
        duration=2.0,
    )
    assert strip["sheets"] == 1 and strip["count"] == 3
    assert (tmp_path / "strip" / "sheet_000.jpg").is_file()


def test_audio_export_matches_the_preview_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore(tmp_path / "projects")
    project = store.import_path(write_wav(tmp_path / "talk.wav", source_audio()))
    _complete_run(project, store, monkeypatch, "semantic")
    project = store.get(project.id)
    proposal, timeline = _edited_timeline(project)
    stages: list[tuple[str, str]] = []
    destination = tmp_path / "out" / "talk_edited.wav"
    destination.parent.mkdir()
    record = export_timeline(
        project=project,
        proposal=proposal,
        timeline=timeline,
        destination=destination,
        overwrite=False,
        export_id="e1",
        on_stage=lambda stage, status: stages.append((stage, status)),
        room_tone=_room_tone(project, proposal),
        keep_render=True,
    )
    assert [stage for stage, status in stages if status == "completed"] == [
        "plan",
        "render",
        "publish",
    ]
    assert record["manual_boundaries"] == 1 and record["dropped_pauses"] == 0
    assert record["kept_pauses"] == 2 and record["inserted_pauses"] == 1
    rendered, _ = sf.read(
        project.exports_dir / "e1" / "final_cut.wav", dtype="float32", always_2d=True
    )
    derivation = derive_pieces(
        timeline["items"],
        proposal,
        sample_rate=SAMPLE_RATE,
        declick=declick_samples(SAMPLE_RATE),
        room_tone=_room_tone(project, proposal),
    )
    with SourceAudio(project.canonical_wav) as source:
        preview = render_range(source, derivation, 0, derivation.output_frames)
    assert np.array_equal(preview, rendered)
    published, rate = sf.read(destination, dtype="float32", always_2d=True)
    assert rate == SAMPLE_RATE and len(published) == len(preview)
    assert np.max(np.abs(published - preview)) < 2e-6  # 24-bit WAV
    with pytest.raises(FileExistsError):
        check_destination(project, destination, overwrite=False)
    with pytest.raises(ExportError, match="replace the source"):
        check_destination(project, project.source_path, overwrite=True)
    with pytest.raises(ExportError, match="file type"):
        check_destination(project, tmp_path / "out" / "talk.mp4", overwrite=False)


def test_untouched_export_reuses_voicecuts_own_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore(tmp_path / "projects")
    project = store.import_path(write_wav(tmp_path / "talk.wav", source_audio()))
    _complete_run(project, store, monkeypatch, "semantic")
    project = store.get(project.id)
    proposal = load_proposal(project.plan_path)
    project.pipeline_output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project.plan_path.parent / "final_cut.wav", project.pipeline_output)
    timeline = validate_timeline(
        {"proposal_sha256": proposal.plan_sha256, "items": proposal.timeline_items()},
        source_frames=FRAMES,
        proposal=proposal,
    )
    destination = tmp_path / "copy.wav"
    record = export_timeline(
        project=project,
        proposal=proposal,
        timeline=timeline,
        destination=destination,
        overwrite=False,
        export_id="e2",
    )
    assert record["reused_voicecut_output"] is True
    assert destination.read_bytes() == project.pipeline_output.read_bytes()


def test_video_export_cuts_the_picture_upright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = write_wav(tmp_path / "a.wav", source_audio())
    video = make_video(tmp_path / "phone.mp4", audio, seconds=6, rotation=90)
    store = ProjectStore(tmp_path / "projects")
    project = store.import_path(video)
    assert project.kind == "video"
    _complete_run(project, store, monkeypatch, "cuts")
    project = store.get(project.id)
    proposal, timeline = _edited_timeline(project)
    destination = tmp_path / "phone_edited.mp4"
    record = export_timeline(
        project=project,
        proposal=proposal,
        timeline=timeline,
        destination=destination,
        overwrite=False,
        export_id="v1",
    )
    manifest = read_json(project.exports_dir / "v1" / "video_render_manifest.json")
    info = manifest["output_media_info"]
    # The render the video was made from is gone; its manifest remains.
    assert not (project.exports_dir / "v1" / "final_cut.wav").exists()
    render = read_json(project.exports_dir / "v1" / "editor_render_manifest.json")
    assert render["final_cut_wav_sha256"]
    assert abs(info["duration_seconds"] - record["duration_seconds"]) < 0.1
    exported = frame_at(destination, 0.3)
    original = frame_at(video, 0.3)
    assert exported.shape == original.shape  # portrait, like the phone shows it
    assert np.mean(np.abs(exported.astype(int) - original.astype(int))) < 20
