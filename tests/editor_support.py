"""Synthetic recordings and VoiceCut plans shared by the editor tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from voicecut import cut_render
from voicecut.common import write_json
from voicecut.mfa_alignment import MFAAlignmentError

SAMPLE_RATE = 16_000
CHANNELS = 2
SECONDS = 6
FRAMES = SAMPLE_RATE * SECONDS

# (text, start, end) of the synthetic narration; "drop" is a removed retake
# and a breath sits in the long pause before "Five.".
WORDS = [
    ("One", 0.50, 0.80),
    ("two.", 0.85, 1.15),
    ("drop", 1.60, 1.90),
    ("Three,", 2.40, 2.70),
    ("four.", 2.75, 3.05),
    ("Five.", 4.60, 4.90),
]
ITEMS = [
    {"first_id": 0, "last_id": 1, "text": "One two.", "break_after": "sentence"},
    {
        "first_id": 3,
        "last_id": 5,
        "text": "Three, four. Five.",
        "break_after": "sentence",
    },
]
BREATH = (3.80, 4.10)

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not on PATH",
)


def source_audio() -> np.ndarray:
    rng = np.random.default_rng(3)
    audio = 1e-4 * rng.standard_normal((FRAMES, CHANNELS))
    for _, start, end in WORDS:
        time = np.arange(int((end - start) * SAMPLE_RATE)) / SAMPLE_RATE
        tone = 0.1 * np.sin(2 * np.pi * 300 * time)
        ramp = int(0.01 * SAMPLE_RATE)
        tone[:ramp] *= np.linspace(0, 1, ramp)
        tone[-ramp:] *= np.linspace(1, 0, ramp)
        first = int(start * SAMPLE_RATE)
        audio[first : first + len(tone)] += tone[:, None]
    first, last = (int(value * SAMPLE_RATE) for value in BREATH)
    audio[first:last] += 0.01 * rng.standard_normal((last - first, CHANNELS))
    return audio.astype(np.float32)


def write_wav(path: Path, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, audio, sample_rate, subtype="FLOAT")
    return path


def render_voicecut(
    work_dir: Path,
    audio_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    policy: str = "semantic",
) -> dict[str, Any]:
    """Run the real VoiceCut renderer on the synthetic take plan.

    MFA is unavailable (Whisper timing is used) and breaths are faked, so the
    run is fast and needs no models.
    """

    plan_path = work_dir / "04_semantic_plan" / "take_plan.json"
    write_json(
        plan_path,
        {
            "status": "complete",
            "line_gap_seconds": 0.25,
            "words": [
                {"id": index, "text": text, "start": start, "end": end}
                for index, (text, start, end) in enumerate(WORDS)
            ],
            "items": ITEMS,
        },
    )

    def no_mfa(**_: object) -> dict[str, object]:
        raise MFAAlignmentError("mfa_runtime_failed", "not available in tests")

    def breaths(mono: np.ndarray, sample_rate: int, **_: object) -> np.ndarray:
        probabilities = np.zeros(len(mono) * 100 // sample_rate + 1, np.float32)
        probabilities[int(BREATH[0] * 100) : int(BREATH[1] * 100)] = 1.0
        return probabilities

    monkeypatch.setattr(cut_render, "mfa_core_timings", no_mfa)
    monkeypatch.setattr(cut_render, "breath_probabilities", breaths)
    return cut_render.render_take_plan(
        audio_path=audio_path,
        plan_path=plan_path,
        output_dir=work_dir / "05_final",
        pause_policy=policy,
    )


FIXTURE_RATE = 48_000
FIXTURE_FRAMES = 480_000


def fixture_plan() -> dict[str, Any]:
    """A hand-made sealed plan exercising every kind of fade and pause."""

    def tone(
        length: int, fade_in: int, fade_out: int, join: int | None, at: int
    ) -> cut_render.Piece:
        return cut_render.Piece(
            "room_tone",
            0,
            length,
            fade_in=fade_in,
            fade_out=fade_out,
            join_index=join,
            trace=[(at, at + length)],
        )

    def source(
        start: int, end: int, fade_in: int, fade_out: int, index: int
    ) -> cut_render.Piece:
        return cut_render.Piece(
            "source",
            start,
            end,
            fade_in=fade_in,
            fade_out=fade_out,
            interval_index=index,
        )

    pieces = [
        tone(16_800, 2_400, 480, None, 430_000),
        source(24_000, 72_000, 480, 480, 0),
        tone(19_680, 480, 480, 0, 430_000),
        source(90_000, 140_000, 480, 192, 1),
        source(200_000, 260_000, 192, 1_440, 2),
        tone(5_760, 1_440, 480, 2, 430_000),
        source(300_000, 420_000, 480, 480, 3),
        tone(29_280, 480, 4_800, None, 430_000),
    ]
    placed = cut_render.place_pieces(pieces)
    words = [
        ("Lead", 0.52, 0.80, True),
        ("in.", 1.40, 1.48, True),
        ("Dropped", 1.55, 1.85, False),
        ("Second", 1.90, 2.90, True),
        ("third", 4.20, 5.40, True),
        ("fourth", 6.30, 8.70, True),
    ]
    return {
        "planner": cut_render.BOUNDARY_PLAN_NAME,
        "renderer": cut_render.RENDERER_NAME,
        "status": "safe",
        "pause_policy": "semantic",
        "source_audio": "/nonexistent/source_audio.wav",
        "source_audio_sha256": "0" * 64,
        "source_sample_rate": FIXTURE_RATE,
        "source_channel_count": 1,
        "source_frame_count": FIXTURE_FRAMES,
        "expected_output_frame_count": placed[-1]["output_end_sample"],
        "words": [
            {
                "text": text,
                "start": start,
                "end": end,
                "kept": kept,
                "alignment": "mfa",
                "core_start": start,
                "core_end": end,
            }
            for text, start, end, kept in words
        ],
        "source_intervals": [
            {
                "source_start_sample": piece.source_start,
                "source_end_sample": piece.source_end,
            }
            for piece in pieces
            if piece.kind == "source"
        ],
        "joins": [{"join_id": f"join_{index:05d}"} for index in range(3)],
        "pieces": placed,
    }


def make_video(
    path: Path,
    audio: Path,
    *,
    seconds: float,
    rotation: int | None = None,
    hflip: bool = False,
) -> Path:
    """A small H.264 test clip with ``audio`` and an optional display matrix.

    The picture is white on its top half and black below, so its orientation
    can be read back from any frame.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    plain = path.with_name(f"{path.stem}.plain.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=25",
            "-i",
            str(audio),
            "-t",
            f"{seconds}",
            "-vf",
            (
                "drawbox=x=0:y=0:w=iw:h=ih/2:color=white:t=fill,"
                "drawbox=x=0:y=0:w=iw/4:h=ih:color=red:t=fill"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(plain),
        ],
        check=True,
    )
    if rotation is None and not hflip:
        plain.replace(path)
        return path
    options = []
    if rotation is not None:
        options += ["-display_rotation", str(rotation)]
    if hflip:
        options += ["-display_hflip"]
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *options,
            "-i",
            str(plain),
            "-c",
            "copy",
            str(path),
        ],
        check=True,
    )
    plain.unlink()
    return path


def frame_at(video: Path, seconds: float) -> np.ndarray:
    """Decode one RGB frame (as players show it) into an array."""

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:stream_side_data=rotation",
            "-of",
            "json",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    import json

    stream = json.loads(probe.stdout)["streams"][0]
    rotation = 0
    for item in stream.get("side_data_list") or []:
        rotation = int(item.get("rotation", 0))
    width, height = stream["width"], stream["height"]
    if rotation % 180:
        width, height = height, width
    raw = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{seconds}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return np.frombuffer(raw, np.uint8).reshape(height, width, 3)
