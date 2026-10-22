"""Render a video from VoiceCut's final, sample-accurate audio edit timeline.

VoiceCut does not interpret pictures. Selected source-audio intervals therefore
select the corresponding source-video intervals, joined directly, without
inserted pauses or frame holds. This module never slows, loops, or invents
motion.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .common import read_json, sha256_file, write_json
from .media import (
    VIDEO_OUTPUT_EXTENSIONS,
    MediaError,
    Runner,
    _prepare_destination,
    _replace_validated,
    _resolve_output_destination,
    _run,
    _tool_path,
    filter_script_arguments,
    probe_media,
)


class VideoRenderError(MediaError):
    """The audio edit timeline cannot be applied safely to the source video."""


# Planner id of plans derived by the interactive editor (``voicecut.editor``).
# Defined here so this module never imports editor code.
EDITOR_BOUNDARY_PLAN_PLANNER = "voicecut_editor_boundary_plan_v1"

AUTHORITATIVE_BOUNDARY_PLAN_RENDERERS = frozenset(
    {
        "authoritative_single_pass_boundary_plan_v2",
        EDITOR_BOUNDARY_PLAN_PLANNER,
    }
)


@dataclass(frozen=True)
class VisualTimelineSegment:
    """One source-video span placed on the output timeline."""

    clip_index: int
    source_start_sample: int
    source_end_sample: int
    output_start_sample: int
    output_end_sample: int


def _object(path: Path, *, description: str) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise VideoRenderError(f"{description} must contain a JSON object: {path}")
    return value


def _integer(value: Any, *, description: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise VideoRenderError(f"{description} must be an integer >= {minimum}")
    return value


def _timeline_record(
    segment: VisualTimelineSegment,
    *,
    sample_rate: int,
) -> dict[str, Any]:
    value = asdict(segment)
    value.update(
        {
            "source_start_seconds": segment.source_start_sample / sample_rate,
            "source_end_seconds": segment.source_end_sample / sample_rate,
            "source_duration_seconds": (
                segment.source_end_sample - segment.source_start_sample
            )
            / sample_rate,
            "output_start_seconds": segment.output_start_sample / sample_rate,
            "output_end_seconds": segment.output_end_sample / sample_rate,
        }
    )
    return value


def _append_source_segment(
    timeline: list[VisualTimelineSegment],
    *,
    clip_index: int,
    source_start: int,
    source_end: int,
    output_cursor: int,
) -> int:
    if source_end < source_start:
        raise VideoRenderError("visual source intervals cannot move backward")
    if source_end == source_start:
        return output_cursor
    frame_count = source_end - source_start
    source_output_end = output_cursor + frame_count
    timeline.append(
        VisualTimelineSegment(
            clip_index=clip_index,
            source_start_sample=source_start,
            source_end_sample=source_end,
            output_start_sample=output_cursor,
            output_end_sample=source_output_end,
        )
    )
    return source_output_end


def _build_single_pass_visual_timeline(
    boundary_plan: dict[str, Any],
) -> tuple[list[VisualTimelineSegment], int, int]:
    sample_rate = _integer(
        boundary_plan.get("source_sample_rate"),
        description="boundary-plan source sample rate",
        minimum=1,
    )
    expected_frames = _integer(
        boundary_plan.get("expected_output_frame_count"),
        description="boundary-plan output frame count",
        minimum=1,
    )
    raw_segments = boundary_plan.get("output_segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise VideoRenderError("boundary plan contains no output trace")
    if any(
        isinstance(segment, dict) and segment.get("kind") == "room_tone"
        for segment in raw_segments
    ):
        raise VideoRenderError(
            "a video edit cannot contain an inserted room-tone segment"
        )

    timeline: list[VisualTimelineSegment] = []
    output_cursor = 0
    for expected_index, segment in enumerate(raw_segments):
        if (
            not isinstance(segment, dict)
            or segment.get("segment_index") != expected_index
        ):
            raise VideoRenderError("boundary-plan output segments are not ordered")
        output_start = _integer(
            segment.get("output_start_sample"),
            description=f"output segment {expected_index} start",
        )
        output_end = _integer(
            segment.get("output_end_sample"),
            description=f"output segment {expected_index} end",
            minimum=1,
        )
        if output_start != output_cursor or output_end <= output_start:
            raise VideoRenderError("boundary-plan output trace is discontinuous")
        kind = segment.get("kind")
        if kind == "source":
            source_start = _integer(
                segment.get("source_start_sample"),
                description=f"output segment {expected_index} source start",
            )
            source_end = _integer(
                segment.get("source_end_sample"),
                description=f"output segment {expected_index} source end",
                minimum=1,
            )
            if source_end - source_start != output_end - output_start:
                raise VideoRenderError("source/output segment durations differ")
            output_cursor = _append_source_segment(
                timeline,
                clip_index=_integer(
                    segment.get("source_interval_index"),
                    description="source interval index",
                ),
                source_start=source_start,
                source_end=source_end,
                output_cursor=output_cursor,
            )
        else:
            raise VideoRenderError("boundary plan contains an untraceable segment")
        if output_cursor != output_end:
            raise VideoRenderError("visual timeline diverges from audio trace")
    if output_cursor != expected_frames:
        raise VideoRenderError("visual timeline does not match final audio duration")
    return timeline, sample_rate, expected_frames


def build_visual_timeline(
    boundary_plan: dict[str, Any],
) -> tuple[list[VisualTimelineSegment], int, int]:
    """Translate a sealed boundary plan into its source-motion timeline."""

    if boundary_plan.get("planner") not in AUTHORITATIVE_BOUNDARY_PLAN_RENDERERS:
        raise VideoRenderError("unsupported boundary plan planner")
    return _build_single_pass_visual_timeline(boundary_plan)


def load_visual_timeline(
    final_render_manifest_path: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[VisualTimelineSegment],
    int,
    int,
]:
    """Load the sealed single-pass boundary plan and its visual timeline."""

    final_render_manifest_path = final_render_manifest_path.resolve()
    final = _object(final_render_manifest_path, description="final render manifest")
    if final.get("status") != "complete":
        raise VideoRenderError("video rendering requires a complete final render")
    boundary_value = final.get("final_boundary_plan")
    if not isinstance(boundary_value, str) or not boundary_value:
        raise VideoRenderError("final render has no authoritative boundary plan")
    boundary_path = Path(boundary_value).resolve()
    if not boundary_path.is_file():
        raise FileNotFoundError(boundary_path)
    boundary_plan = _object(boundary_path, description="final boundary plan")
    expected_boundary_sha = final.get("final_boundary_plan_sha256")
    if not isinstance(expected_boundary_sha, str):
        raise VideoRenderError("final render does not seal its boundary plan")
    if sha256_file(boundary_path) != expected_boundary_sha:
        raise VideoRenderError("final boundary plan changed after audio rendering")
    if boundary_plan.get("status") != "safe":
        raise VideoRenderError("video rendering requires a safe boundary plan")
    timeline, sample_rate, expected_frames = build_visual_timeline(boundary_plan)
    return final, boundary_plan, timeline, sample_rate, expected_frames


def _video_codec_arguments(extension: str) -> list[str]:
    if extension in {".mkv", ".mov", ".mp4"}:
        arguments = [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]
        if extension in {".mov", ".mp4"}:
            arguments.extend(["-movflags", "+faststart"])
        return arguments
    if extension == ".webm":
        return [
            "-c:v",
            "libvpx-vp9",
            "-crf",
            "28",
            "-b:v",
            "0",
            "-row-mt",
            "1",
            "-c:a",
            "libopus",
            "-b:a",
            "160k",
        ]
    supported = ", ".join(sorted(VIDEO_OUTPUT_EXTENSIONS))
    raise VideoRenderError(
        f"unsupported video output extension {extension!r}; use {supported}"
    )


def _filter_graph(
    timeline: list[VisualTimelineSegment],
    *,
    sample_rate: int,
    video_stream_index: int,
    stream_offset_seconds: float,
    expected_duration_seconds: float,
) -> str:
    filters: list[str] = []
    labels: list[str] = []
    for index, segment in enumerate(timeline):
        source_start = segment.source_start_sample / sample_rate
        source_end = segment.source_end_sample / sample_rate
        visual_start = source_start + stream_offset_seconds
        visual_end = source_end + stream_offset_seconds
        if visual_start < -1e-6 or visual_end <= visual_start:
            raise VideoRenderError(
                "selected audio interval has no corresponding source-video interval"
            )
        output_start = segment.output_start_sample / sample_rate
        chain = (
            f"[0:{video_stream_index}]setpts=PTS-STARTPTS,"
            f"trim=start={max(0.0, visual_start):.9f}:end={visual_end:.9f},"
            "settb=expr=1/1000000,"
            f"setpts=PTS-STARTPTS+{output_start:.9f}/TB"
        )
        label = f"v{index}"
        filters.append(f"{chain}[{label}]")
        labels.append(f"[{label}]")
    if len(labels) == 1:
        filters.append(f"{labels[0]}null[vjoined]")
    else:
        filters.append(
            "".join(labels)
            + f"interleave=nb_inputs={len(labels)}:duration=longest[vjoined]"
        )
    filters.append(
        "[vjoined]"
        "tpad=stop_mode=clone:stop_duration=1,"
        f"trim=duration={expected_duration_seconds:.9f},"
        "setpts=PTS-STARTPTS,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        "format=yuv420p[vout]"
    )
    return ";\n".join(filters)


def render_edited_video(
    *,
    source_video: Path,
    media_input_manifest_path: Path,
    final_render_manifest_path: Path,
    output_path: Path,
    manifest_path: Path | None = None,
    ffmpeg: str | Path = "ffmpeg",
    ffprobe: str | Path = "ffprobe",
    overwrite: bool = False,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Apply VoiceCut's exact audio edit intervals to a source video's pictures."""

    source_video = source_video.resolve()
    source_info = probe_media(source_video, ffprobe=ffprobe, runner=runner)
    if source_info.video_stream is None:
        raise VideoRenderError("video rendering requires a real video stream")
    media_input_manifest_path = media_input_manifest_path.resolve()
    media_input = _object(
        media_input_manifest_path,
        description="media input manifest",
    )
    if (
        media_input.get("source_media") != str(source_video)
        or media_input.get("source_media_sha256") != sha256_file(source_video)
        or media_input.get("source_kind") != "video"
    ):
        raise VideoRenderError(
            "source video does not match the media used to create the transcript"
        )

    (
        final,
        boundary_plan,
        timeline,
        sample_rate,
        expected_frames,
    ) = load_visual_timeline(final_render_manifest_path)
    canonical_audio = media_input.get("canonical_audio")
    if not isinstance(canonical_audio, str):
        raise VideoRenderError("media input manifest has no canonical audio")
    canonical_audio_path = Path(canonical_audio).resolve()
    canonical_audio_sha = media_input.get("canonical_audio_sha256")
    if (
        not isinstance(canonical_audio_sha, str)
        or not canonical_audio_path.is_file()
        or sha256_file(canonical_audio_path) != canonical_audio_sha
    ):
        raise VideoRenderError(
            "canonical source audio changed after media input preparation"
        )
    if (
        final.get("source_audio") != str(canonical_audio_path)
        or final.get("source_audio_sha256") != canonical_audio_sha
    ):
        raise VideoRenderError(
            "final edit was not produced from this video's canonical audio"
        )
    final_audio_value = final.get("final_cut_wav")
    if not isinstance(final_audio_value, str):
        raise VideoRenderError("final render manifest has no final audio")
    final_audio = Path(final_audio_value).resolve()
    if not final_audio.is_file():
        raise FileNotFoundError(final_audio)
    if isinstance(final.get("final_cut_wav_sha256"), str) and final[
        "final_cut_wav_sha256"
    ] != sha256_file(final_audio):
        raise VideoRenderError("final edited WAV changed after final rendering")
    final_audio_info = probe_media(final_audio, ffprobe=ffprobe, runner=runner)
    expected_duration = expected_frames / sample_rate
    audio_tolerance = max(1.0 / sample_rate, 0.0001)
    if abs(final_audio_info.duration_seconds - expected_duration) > audio_tolerance:
        raise VideoRenderError(
            "final edited WAV duration does not match its semantic timeline"
        )

    audio_start = source_info.audio_stream.start_seconds
    video_start = source_info.video_stream.start_seconds
    stream_offset = audio_start - video_start
    visual_duration = (
        source_info.video_stream.duration_seconds or source_info.duration_seconds
    )
    for segment in timeline:
        visual_start = segment.source_start_sample / sample_rate + stream_offset
        visual_end = segment.source_end_sample / sample_rate + stream_offset
        if visual_start < -0.001 or visual_end > visual_duration + 0.1:
            raise VideoRenderError(
                "selected audio lies outside the available source-video timeline; "
                "automatic frame recovery would risk desynchronization"
            )

    output_path = _resolve_output_destination(output_path)
    if output_path == source_video:
        raise VideoRenderError("edited video output cannot replace the source video")
    extension = output_path.suffix.lower()
    codec_arguments = _video_codec_arguments(extension)
    resolved_manifest_path = (
        manifest_path.resolve()
        if manifest_path is not None
        else output_path.with_name(f"{output_path.stem}.video_render_manifest.json")
    )
    if resolved_manifest_path == output_path:
        raise VideoRenderError("video manifest path cannot equal the output video")
    if resolved_manifest_path.exists() and not overwrite:
        raise FileExistsError(resolved_manifest_path)
    temporary = _prepare_destination(output_path, overwrite=overwrite)
    filter_script = temporary.with_suffix(temporary.suffix + ".filter.txt")
    filter_script.write_text(
        _filter_graph(
            timeline,
            sample_rate=sample_rate,
            video_stream_index=source_info.video_stream.index,
            stream_offset_seconds=stream_offset,
            expected_duration_seconds=expected_duration,
        ),
        encoding="utf-8",
    )
    ffmpeg_path = _tool_path(ffmpeg)
    try:
        _run(
            [
                ffmpeg_path,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source_video),
                "-i",
                str(final_audio),
                *filter_script_arguments(ffmpeg_path, filter_script),
                "-map",
                "[vout]",
                "-map",
                "1:a:0",
                "-map_metadata",
                "0",
                "-map_chapters",
                "-1",
                *codec_arguments,
                "-fps_mode",
                "vfr",
                "-max_muxing_queue_size",
                "1024",
                str(temporary),
            ],
            runner=runner,
        )
        output_info = probe_media(temporary, ffprobe=ffprobe, runner=runner)
        if output_info.video_stream is None:
            raise VideoRenderError("rendered artifact contains no video stream")
        frame_rate = output_info.video_stream.average_frame_rate or 25.0
        duration_tolerance = max(0.12, 2.0 / frame_rate)
        encoded_video_duration = (
            output_info.video_stream.duration_seconds or output_info.duration_seconds
        )
        encoded_audio_duration = (
            output_info.audio_stream.duration_seconds or output_info.duration_seconds
        )
        if (
            abs(output_info.duration_seconds - expected_duration) > duration_tolerance
            or abs(encoded_video_duration - expected_duration) > duration_tolerance
            or abs(encoded_audio_duration - expected_duration) > duration_tolerance
            or abs(encoded_video_duration - encoded_audio_duration) > duration_tolerance
        ):
            raise VideoRenderError(
                "rendered audio/video streams are not synchronized to the final "
                f"timeline ({encoded_video_duration:.6f}s video, "
                f"{encoded_audio_duration:.6f}s audio, "
                f"{expected_duration:.6f}s expected)"
            )
        _replace_validated(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
        if filter_script.exists():
            filter_script.unlink()

    output_info = probe_media(output_path, ffprobe=ffprobe, runner=runner)
    manifest = {
        "schema_version": 1,
        "renderer": "voicecut_visual_timeline_v1",
        "status": "complete",
        "visual_policy": (
            "normal-speed selected source video with clear cuts and no "
            "inserted frame holds"
        ),
        "frame_quantization_policy": (
            "source video cuts occur on decoded frame timestamps; the last "
            "selected frame is extended only when needed to cover a sub-frame "
            "duration remainder"
        ),
        "source_video": str(source_video),
        "source_video_sha256": sha256_file(source_video),
        "media_input_manifest": str(media_input_manifest_path),
        "media_input_manifest_sha256": sha256_file(media_input_manifest_path),
        "final_render_manifest": str(final_render_manifest_path.resolve()),
        "final_render_manifest_sha256": sha256_file(
            final_render_manifest_path.resolve()
        ),
        "final_boundary_plan": final["final_boundary_plan"],
        "final_boundary_plan_sha256": sha256_file(Path(final["final_boundary_plan"])),
        "final_audio": str(final_audio),
        "final_audio_sha256": sha256_file(final_audio),
        "output_video": str(output_path),
        "output_video_sha256": sha256_file(output_path),
        "video_render_manifest": str(resolved_manifest_path),
        "output_media_info": asdict(output_info),
        "source_sample_rate": sample_rate,
        "expected_output_frame_count": expected_frames,
        "expected_output_duration_seconds": expected_duration,
        "source_audio_start_seconds": audio_start,
        "source_video_start_seconds": video_start,
        "audio_to_video_stream_offset_seconds": stream_offset,
        "visual_timeline": [
            _timeline_record(segment, sample_rate=sample_rate) for segment in timeline
        ],
        "visual_source_segments": len(timeline),
        "boundary_plan_renderer": boundary_plan.get("planner"),
    }
    write_json(resolved_manifest_path, manifest)
    return manifest
