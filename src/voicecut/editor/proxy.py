"""Browser preview media for video projects: an upright proxy and a filmstrip.

The browser never decodes the original video.  FFmpeg transcodes it once to a
small H.264 MP4 with dense keyframes so seeking at every cut is fast.  FFmpeg
applies the source's display matrix (rotation and mirroring) while doing so,
exactly as it does when VoiceCut renders the exported video, so the preview
and the export always show the picture the same way up.  The proxy's time 0
is the first video frame, the same origin ``video_render`` cuts from.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROXY_MAX_SIDE = 1280
PROXY_BITRATE = "2500k"
PROXY_GOP_FRAMES = 15
FILMSTRIP_HEIGHT = 90
FILMSTRIP_COLUMNS = 10
FILMSTRIP_ROWS = 10
FILMSTRIP_MAX_THUMBS = 1200

ProgressCallback = Callable[[float], None]


class ProxyError(RuntimeError):
    """Preview media could not be produced."""


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise ProxyError(f"{name} is required but was not found on PATH")
    return path


def _encoders(ffmpeg: str) -> str:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def _run_with_progress(
    command: list[str],
    *,
    duration: float,
    on_progress: ProgressCallback | None,
) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None
    for line in process.stdout:
        key, _, value = line.strip().partition("=")
        if key == "out_time_us" and on_progress and duration > 0:
            try:
                seconds = int(value) / 1_000_000
            except ValueError:
                continue
            on_progress(max(0.0, min(1.0, seconds / duration)))
    stderr = process.stderr.read() if process.stderr else ""
    if process.wait() != 0:
        raise ProxyError(stderr.strip()[-2000:] or "ffmpeg failed")


def probe_video(path: Path) -> dict[str, Any]:
    """Width, height, duration and frame rate of the first video stream."""

    result = subprocess.run(
        [
            _tool("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate,duration:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ProxyError(result.stderr.strip() or f"cannot probe {path}")
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise ProxyError(f"{path} has no video stream")
    stream = streams[0]
    numerator, _, denominator = str(stream.get("avg_frame_rate") or "0/1").partition(
        "/"
    )
    try:
        fps = float(numerator) / float(denominator or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    duration = stream.get("duration") or (payload.get("format") or {}).get("duration")
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "fps": fps if math.isfinite(fps) and fps > 0 else None,
        "duration": float(duration or 0.0),
    }


def build_proxy(
    source: Path,
    output: Path,
    *,
    duration: float,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Transcode ``source`` to an upright preview MP4 at ``output``."""

    ffmpeg = _tool("ffmpeg")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.tmp.mp4")
    video_filter = (
        "setpts=PTS-STARTPTS,"
        f"scale=w='min(iw,{PROXY_MAX_SIDE})':h='min(ih,{PROXY_MAX_SIDE})'"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "format=yuv420p"
    )
    # Hardware decode + encode is several times faster on Apple Silicon; the
    # software encoder is the portable fallback.
    attempts: list[tuple[list[str], list[str]]] = []
    if "h264_videotoolbox" in _encoders(ffmpeg):
        attempts.append(
            (
                ["-hwaccel", "videotoolbox", "-i", str(source)],
                ["-c:v", "h264_videotoolbox", "-b:v", PROXY_BITRATE],
            )
        )
    attempts.append(
        (
            ["-i", str(source)],
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "26",
                "-tune",
                "fastdecode",
            ],
        )
    )
    errors: list[str] = []
    for input_arguments, codec_arguments in attempts:
        command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *input_arguments,
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            video_filter,
            *codec_arguments,
            "-g",
            str(PROXY_GOP_FRAMES),
            "-fps_mode",
            "passthrough",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(temporary),
        ]
        try:
            _run_with_progress(command, duration=duration, on_progress=on_progress)
        except ProxyError as error:
            errors.append(str(error))
            temporary.unlink(missing_ok=True)
            continue
        temporary.replace(output)
        info = probe_video(output)
        info["encoder"] = codec_arguments[1]
        return info
    raise ProxyError("could not build the video preview: " + " | ".join(errors))


def filmstrip_interval(duration: float) -> float:
    """Seconds between filmstrip thumbnails (at most FILMSTRIP_MAX_THUMBS)."""

    return float(max(1, math.ceil(duration / FILMSTRIP_MAX_THUMBS)))


def build_filmstrip(
    proxy: Path,
    output_dir: Path,
    *,
    width: int,
    height: int,
    duration: float,
) -> dict[str, Any]:
    """Tile thumbnails of ``proxy`` into JPEG sheets of 10 x 10 frames.

    Thumbnail ``k`` shows the proxy at ``k * interval`` seconds and sits in
    sheet ``k // 100`` at column ``k % 10`` and row ``(k // 10) % 10``.
    """

    ffmpeg = _tool("ffmpeg")
    interval = filmstrip_interval(duration)
    thumb_height = FILMSTRIP_HEIGHT
    thumb_width = max(2, round(width * thumb_height / height / 2) * 2)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    result = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(proxy),
            "-vf",
            (
                f"fps=1/{interval:g},scale={thumb_width}:{thumb_height},"
                f"tile={FILMSTRIP_COLUMNS}x{FILMSTRIP_ROWS}"
            ),
            "-q:v",
            "5",
            "-start_number",
            "0",
            str(output_dir / "sheet_%03d.jpg"),
        ],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        raise ProxyError(result.stderr.strip()[-2000:] or "filmstrip failed")
    sheets = sorted(output_dir.glob("sheet_*.jpg"))
    return {
        "interval": interval,
        "count": max(0, math.floor(duration / interval) + 1),
        "thumb_width": thumb_width,
        "thumb_height": thumb_height,
        "columns": FILMSTRIP_COLUMNS,
        "rows": FILMSTRIP_ROWS,
        "sheets": len(sheets),
    }
