"""Turn an editor timeline into overlap-add pieces and render them.

The editor never re-runs the pipeline.  It derives ``cut_render`` pieces from
the user's timeline of clips and pauses, keeping VoiceCut's own work wherever
it still applies:

* a clip edge that is still VoiceCut's keeps VoiceCut's fade; any other cut
  gets a short declick crossfade; clips that continue the same source audio
  (a split) and the media's own start/end get no fade unless a pause is next
  to them;
* every pause is room tone, crossfaded into the clips beside it with their
  edge fades, exactly the way ``cut_render.build_pieces`` lays a pause down.
  A pause VoiceCut made, still the same length between the same fades, keeps
  VoiceCut's room tone; any other pause takes clean room tone from the
  recording's ambience bank (:mod:`.ambience`).

The same pieces drive both the export (:func:`build_editor_plan` +
``cut_render.render_pieces``) and the browser preview (:func:`render_range`),
which renders any stretch of the output with identical arithmetic.  The
browser's timeline layout (``frontend/src/model/layout.ts``) is a port of
:func:`derive_pieces`; ``tests/test_editor_plan.py`` pins both to one fixture.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import numpy as np
import soundfile as sf

from .. import cut_render, video_render
from .timeline import (
    LEAD_FADE_SECONDS,
    MEDIA_KINDS,
    TAIL_FADE_SECONDS,
    Proposal,
    clips_and_gaps,
)

EDITOR_PLANNER_ID = video_render.EDITOR_BOUNDARY_PLAN_PLANNER
EDITOR_PLAN_SCHEMA_VERSION = 3
DECLICK_SECONDS = 0.002

RoomToneSource = Callable[[int, int, str], list[tuple[int, int]]]


class PlanError(RuntimeError):
    """An editor plan cannot be derived, validated or rendered safely."""


def declick_samples(sample_rate: int) -> int:
    return round(DECLICK_SECONDS * sample_rate)


@dataclass(frozen=True)
class Derivation:
    """Pieces of one timeline plus how much of VoiceCut's work they keep."""

    pieces: list[cut_render.Piece]
    placed: list[dict[str, Any]]
    #: VoiceCut pauses still on the timeline ("lead", "tail" or join index).
    kept_pauses: list[Any]
    inserted_pauses: int
    declicks: int

    @property
    def output_frames(self) -> int:
        return self.placed[-1]["output_end_sample"] if self.placed else 0


def _edge_fade(
    *,
    edge: int,
    neighbour: int | None,
    origin: str,
    voicecut_fade: int | None,
    declick: int,
    length: int,
    beside_pause: bool,
) -> tuple[int, bool]:
    """Fade at one clip edge and whether it is a manual declick."""

    if not beside_pause and (neighbour == edge or origin == "source"):
        return 0, False  # the next clip continues this audio, or a media edge
    if origin == "voicecut" and voicecut_fade is not None:
        return voicecut_fade, False
    fade = min(declick, length // 2)
    return fade, fade > 0


def _fit_fades(fade_in: int, fade_out: int, length: int) -> tuple[int, int]:
    """Keep both fades inside a (possibly very short) piece."""

    if fade_in + fade_out > length:
        fade_in = min(fade_in, length // 2)
        fade_out = min(fade_out, length - fade_in)
    return fade_in, fade_out


def derive_pieces(
    items: Sequence[dict[str, Any]],
    proposal: Proposal | None,
    *,
    sample_rate: int,
    declick: int,
    room_tone: RoomToneSource | None = None,
) -> Derivation:
    """Derive overlap-add pieces from a validated timeline's items."""

    clips, gaps = clips_and_gaps({"items": list(items)})
    if not clips:
        raise PlanError("the timeline has no clips")
    count = len(clips)
    fades: list[tuple[int, int]] = []
    declicks = 0
    for index, clip in enumerate(clips):
        start = clip["source_start_sample"]
        end = clip["source_end_sample"]
        pause_before = gaps[index] is not None
        pause_after = gaps[index + 1] is not None
        previous = clips[index - 1] if index and not pause_before else None
        following = clips[index + 1] if index + 1 < count and not pause_after else None
        voicecut_in = voicecut_out = None
        if proposal is not None:
            at = proposal.interval_starting_at(start)
            voicecut_in = proposal.fades[at][0] if at is not None else None
            at = proposal.interval_ending_at(end)
            voicecut_out = proposal.fades[at][1] if at is not None else None
        fade_in, declicked_in = _edge_fade(
            edge=start,
            neighbour=previous["source_end_sample"] if previous else None,
            origin=clip["start_origin"],
            voicecut_fade=voicecut_in,
            declick=declick,
            length=end - start,
            beside_pause=pause_before,
        )
        fade_out, declicked_out = _edge_fade(
            edge=end,
            neighbour=following["source_start_sample"] if following else None,
            origin=clip["end_origin"],
            voicecut_fade=voicecut_out,
            declick=declick,
            length=end - start,
            beside_pause=pause_after,
        )
        declicks += int(declicked_in) + int(declicked_out)
        fades.append(_fit_fades(fade_in, fade_out, end - start))

    pieces: list[cut_render.Piece] = []
    kept: list[Any] = []
    inserted = 0
    for position in range(count + 1):
        gap = gaps[position]
        if gap is not None:
            after_clip, before_clip = position > 0, position < count
            heard = gap["length"]
            # A pause at either end of the timeline fades from/into silence.
            fade_in = (
                fades[position - 1][1]
                if after_clip
                else min(int(LEAD_FADE_SECONDS * sample_rate), heard)
            )
            fade_out = (
                fades[position][0]
                if before_clip
                else min(int(TAIL_FADE_SECONDS * sample_rate), heard)
            )
            length = (
                heard
                + (fade_in if after_clip else 0)
                + (fade_out if before_clip else 0)
            )
            tone = (
                proposal.room_tone(gap["join"])
                if proposal is not None and gap["origin"] == "voicecut"
                else None
            )
            if gap["origin"] == "voicecut":
                kept.append(gap["join"])
            else:
                inserted += 1
            if tone is not None and (tone.length, tone.fade_in, tone.fade_out) == (
                length,
                fade_in,
                fade_out,
            ):
                trace = list(tone.trace)
            elif room_tone is not None:
                near = (
                    clips[position - 1]["source_end_sample"]
                    if after_clip
                    else clips[position]["source_start_sample"]
                )
                trace = room_tone(length, near, gap["id"])
            else:
                trace = []
            pieces.append(
                cut_render.Piece(
                    "room_tone",
                    0,
                    length,
                    fade_in=fade_in,
                    fade_out=fade_out,
                    join_index=gap["join"] if type(gap["join"]) is int else None,
                    trace=trace,
                )
            )
        if position < count:
            clip = clips[position]
            pieces.append(
                cut_render.Piece(
                    "source",
                    clip["source_start_sample"],
                    clip["source_end_sample"],
                    fade_in=fades[position][0],
                    fade_out=fades[position][1],
                    interval_index=position,
                )
            )
    return Derivation(
        pieces=pieces,
        placed=cut_render.place_pieces(pieces),
        kept_pauses=kept,
        inserted_pauses=inserted,
        declicks=declicks,
    )


def _piece_record(piece: cut_render.Piece, segment_id: str | None) -> dict[str, Any]:
    return {
        "kind": piece.kind,
        "source_start_sample": piece.source_start,
        "source_end_sample": piece.source_end,
        "fade_in": piece.fade_in,
        "fade_out": piece.fade_out,
        "interval_index": piece.interval_index,
        "join_index": piece.join_index,
        "editor_segment_id": segment_id,
        "trace": [list(item) for item in piece.trace],
    }


def pieces_from_records(records: Sequence[dict[str, Any]]) -> list[cut_render.Piece]:
    return [
        cut_render.Piece(
            kind=record["kind"],
            source_start=int(record["source_start_sample"]),
            source_end=int(record["source_end_sample"]),
            fade_in=int(record["fade_in"]),
            fade_out=int(record["fade_out"]),
            interval_index=record["interval_index"],
            join_index=record["join_index"],
            trace=[(int(start), int(end)) for start, end in record["trace"]],
        )
        for record in records
    ]


def build_editor_plan(
    *,
    timeline: dict[str, Any],
    proposal: Proposal | None,
    media_kind: str,
    source_audio: Path,
    source_audio_sha256: str,
    sample_rate: int,
    channels: int,
    source_frames: int,
    room_tone: RoomToneSource | None = None,
) -> dict[str, Any]:
    """Build the sealed editor boundary plan the export renders and publishes.

    The plan has the shape ``video_render`` accepts for
    :data:`EDITOR_PLANNER_ID` (sample-exact ``output_segments``).
    """

    if media_kind not in MEDIA_KINDS:
        raise PlanError(f"unsupported media kind: {media_kind!r}")
    clips, _ = clips_and_gaps(timeline)
    declick = declick_samples(sample_rate)
    derivation = derive_pieces(
        timeline["items"],
        proposal,
        sample_rate=sample_rate,
        declick=declick,
        room_tone=room_tone,
    )
    by_interval = {
        record["source_interval_index"]: record
        for record in derivation.placed
        if record["kind"] == "source"
    }
    editor_segments = [
        {
            **clip,
            "output_start_sample": by_interval[index]["output_start_sample"],
            "output_end_sample": by_interval[index]["output_end_sample"],
        }
        for index, clip in enumerate(clips)
    ]
    pauses = []
    if proposal is not None:
        pauses = [
            join
            for join in ("lead", *range(len(proposal.joins)), "tail")
            if proposal.room_tone(join) is not None
        ]
    kept = set(derivation.kept_pauses)
    dropped = [
        {
            "kind": "pause",
            "id": join if isinstance(join, str) else f"join_{join:05d}",
            "reason": "deleted",
        }
        for join in pauses
        if join not in kept
    ]
    if proposal is not None:
        pause_policy = proposal.pause_policy
    else:
        pause_policy = "cuts" if media_kind == "video" else "semantic"
    plan = {
        "schema_version": EDITOR_PLAN_SCHEMA_VERSION,
        "planner": EDITOR_PLANNER_ID,
        "renderer": cut_render.RENDERER_NAME,
        "status": "safe",
        "pause_policy": pause_policy,
        "media_kind": media_kind,
        "source_audio": str(source_audio),
        "source_audio_sha256": source_audio_sha256,
        "source_sample_rate": sample_rate,
        "source_channel_count": channels,
        "source_frame_count": source_frames,
        "derived_from_final_boundary_plan": (
            str(proposal.plan_path) if proposal else None
        ),
        "derived_from_final_boundary_plan_sha256": (
            proposal.plan_sha256 if proposal else None
        ),
        "editor_timeline": timeline,
        "editor_segments": editor_segments,
        "pieces": [
            _piece_record(
                piece,
                clips[piece.interval_index]["id"] if piece.kind == "source" else None,
            )
            for piece in derivation.pieces
        ],
        "output_segments": cut_render._contiguous_trace(derivation.placed),
        "expected_output_frame_count": derivation.output_frames,
        "manual_boundary_count": sum(
            1
            for clip in clips
            for key in ("start_origin", "end_origin")
            if clip[key] == "manual"
        ),
        "declick_samples": declick,
        "declick_fade_count": derivation.declicks,
        "carried_over": {
            "pauses": len(kept),
            "inserted_pauses": derivation.inserted_pauses,
        },
        "dropped": dropped,
    }
    validate_editor_plan(plan)
    return plan


def validate_editor_plan(plan: dict[str, Any]) -> None:
    """Check the structural invariants of a derived editor plan."""

    if plan.get("planner") != EDITOR_PLANNER_ID or plan.get("status") != "safe":
        raise PlanError("not a safe editor plan")
    if plan.get("schema_version") != EDITOR_PLAN_SCHEMA_VERSION:
        raise PlanError("unsupported editor plan schema version")
    frames = plan.get("source_frame_count")
    if type(frames) is not int or frames <= 0:
        raise PlanError("editor plan has no source frame count")
    records = plan.get("pieces")
    if not isinstance(records, list) or not records:
        raise PlanError("editor plan has no pieces")
    for index, record in enumerate(records):
        start, end = record["source_start_sample"], record["source_end_sample"]
        fade_in, fade_out = record["fade_in"], record["fade_out"]
        if (
            end <= start
            or fade_in < 0
            or fade_out < 0
            or fade_in + fade_out > end - start
        ):
            raise PlanError(f"piece {index} has invalid geometry or fades")
        if record["kind"] == "source":
            if not 0 <= start < end <= frames:
                raise PlanError(f"piece {index} leaves the canonical source")
        elif record["kind"] == "room_tone":
            # An empty trace is a silent pause (the recording had no clean
            # room tone to offer).
            if any(
                not 0 <= int(first) < int(last) <= frames
                for first, last in record["trace"]
            ):
                raise PlanError(f"piece {index} takes room tone outside the source")
        else:
            raise PlanError(f"piece {index} has an unknown kind")
    placed = cut_render.place_pieces(pieces_from_records(records))
    if placed[-1]["output_end_sample"] != plan.get("expected_output_frame_count"):
        raise PlanError("editor plan frame count differs from its pieces")
    if plan.get("output_segments") != cut_render._contiguous_trace(placed):
        raise PlanError("editor plan output trace differs from its pieces")


# ---------------------------------------------------------------------------
# Rendering


class SourceAudio:
    """Random access to a canonical float WAV without loading all of it.

    Supports exactly what ``cut_render``'s room-tone assembly needs from an
    audio array: ``shape`` and slicing with ``[start:end]``.
    """

    def __init__(self, path: Path) -> None:
        self._file = sf.SoundFile(str(path))
        self.sample_rate = int(self._file.samplerate)
        self.shape = (int(self._file.frames), int(self._file.channels))

    def __getitem__(self, key: slice) -> np.ndarray:
        if not isinstance(key, slice) or key.step not in (None, 1):
            raise TypeError("SourceAudio supports contiguous slices only")
        start, stop, _ = key.indices(self.shape[0])
        if stop <= start:
            return np.zeros((0, self.shape[1]), np.float32)
        self._file.seek(start)
        return self._file.read(stop - start, dtype="float32", always_2d=True)

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def render_range(
    source: SourceAudio,
    derivation: Derivation,
    start: int,
    stop: int,
) -> np.ndarray:
    """Render output samples ``[start, stop)`` exactly as ``render_pieces`` does.

    Every output sample is the same float32 sum of the same faded source
    samples, so concatenated ranges equal the full render bit for bit.
    """

    start = max(0, start)
    stop = min(derivation.output_frames, stop)
    channels = source.shape[1]
    output = np.zeros((max(0, stop - start), channels), np.float32)
    if stop <= start:
        return output
    for piece, record in zip(derivation.pieces, derivation.placed, strict=True):
        piece_start = record["output_start_sample"]
        piece_end = record["output_end_sample"]
        low, high = max(start, piece_start), min(stop, piece_end)
        if low >= high:
            continue
        length = piece_end - piece_start
        first, last = low - piece_start, high - piece_start
        if piece.kind == "source":
            chunk = source[piece.source_start + first : piece.source_start + last]
        else:
            chunk = cut_render._assemble_room_tone(
                source, piece.trace, length, source.sample_rate
            )[first:last].copy()
        fade_in, fade_out = record["fade_in_samples"], record["fade_out_samples"]
        if fade_in and first < fade_in:
            until = min(last, fade_in)
            curve = cut_render._equal_power(fade_in, True)
            chunk[: until - first] *= curve[first:until, None]
        if fade_out and last > length - fade_out:
            begin = max(first, length - fade_out)
            curve = cut_render._equal_power(fade_out, False)
            chunk[begin - first :] *= curve[
                begin - (length - fade_out) : last - (length - fade_out), None
            ]
        output[low - start : high - start] += chunk
    return output


def render_plan_to_wav(
    *,
    audio_path: Path,
    plan: dict[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    """Render a validated editor plan with the pipeline's own renderer."""

    validate_editor_plan(plan)
    audio, sample_rate = sf.read(str(audio_path), dtype="float32", always_2d=True)
    if (
        int(sample_rate) != plan["source_sample_rate"]
        or audio.shape[1] != plan["source_channel_count"]
        or len(audio) != plan["source_frame_count"]
    ):
        raise PlanError("canonical audio does not match the editor plan")
    rendered, placed = cut_render.render_pieces(
        audio, pieces_from_records(plan["pieces"]), int(sample_rate)
    )
    if len(rendered) != plan["expected_output_frame_count"]:
        raise PlanError("rendered length differs from the editor plan")
    # Retained speech outside the fades must be copied sample for sample.
    for record in placed:
        if record["kind"] != "source":
            continue
        first = record["source_start_sample"] + record["fade_in_samples"]
        last = record["source_end_sample"] - record["fade_out_samples"]
        offset = record["output_start_sample"] + record["fade_in_samples"]
        if last > first and not np.array_equal(
            rendered[offset : offset + last - first], audio[first:last]
        ):
            raise PlanError("retained speech changed during the editor render")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_path), rendered, int(sample_rate), subtype="FLOAT")
    return {
        "frame_count": len(rendered),
        "sample_rate": int(sample_rate),
        "channel_count": int(audio.shape[1]),
        "duration_seconds": len(rendered) / int(sample_rate),
    }
