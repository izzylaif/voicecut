"""Voice Cut on what the timeline keeps, not on the whole recording.

The user may trim a recording before Voice Cut (split it and delete parts),
or delete parts of Voice Cut's result before running it again.  Voice Cut
then edits only the parts that are left: the editor writes them, in order,
into one WAV -- an *excerpt* of the canonical WAV, sample for sample -- the
pipeline edits the excerpt as if it were the recording, and the sealed plan
it returns is mapped back onto the recording.

* :func:`timeline_scope` -- the parts of the recording Voice Cut edits;
* :func:`write_excerpt` -- the excerpt WAV;
* :class:`ExcerptMap` and :func:`map_plan` -- the plan in recording samples.

A kept interval of the excerpt that runs from one part into the next becomes
two intervals of the recording.  Between them is a hard cut, like any cut the
user makes, and it gets the editor's short declick crossfade; so does an edge
of VoiceCut's that falls exactly where one part ends and the next begins.
"""

from __future__ import annotations

import bisect
import itertools
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import soundfile as sf

from .. import cut_render
from ..common import sha256_file
from .timeline import Proposal

Ranges = list[tuple[int, int]]
EXCERPT_BLOCK_FRAMES = 1 << 18
# Join fields of a sealed plan that are times in the source, and whether each
# is the end of what lies before it (True) or the start of what follows.
JOIN_TIMES = {
    "left_tail": True,
    "limit_before": True,
    "right_head": False,
    "limit_after": False,
}
WORD_TIMES = {"start": False, "core_start": False, "end": True, "core_end": True}


class ScopeError(ValueError):
    """An excerpt or the plan made from it does not fit the recording."""


def normalize(ranges: Iterable[Sequence[int]]) -> Ranges:
    """``ranges`` sorted, without empty ones, overlapping or touching merged."""

    merged: Ranges = []
    for start, end in sorted((int(start), int(end)) for start, end in ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract(
    ranges: Iterable[Sequence[int]], removed: Iterable[Sequence[int]]
) -> Ranges:
    """The parts of ``ranges`` outside every range of ``removed``."""

    cuts = normalize(removed)
    result: Ranges = []
    for start, end in normalize(ranges):
        cursor = start
        for cut_start, cut_end in cuts:
            if cut_end <= cursor:
                continue
            if cut_start >= end:
                break
            if cut_start > cursor:
                result.append((cursor, cut_start))
            cursor = max(cursor, cut_end)
        if cursor < end:
            result.append((cursor, end))
    return result


def timeline_scope(
    clips: Sequence[dict[str, Any]],
    proposal: Proposal | None,
    source_frames: int,
) -> Ranges:
    """The parts of the recording that Voice Cut edits when it runs now.

    Before any Voice Cut they are the clips on the timeline.  After one, they
    are the parts that run edited, without what the user deleted from its
    result and with anything the user put back: so a new run decides again
    what Voice Cut removed, but never brings back what the user removed.
    """

    on_timeline = normalize(
        (clip["source_start_sample"], clip["source_end_sample"]) for clip in clips
    )
    if proposal is None:
        return on_timeline
    edited = list(proposal.scope) if proposal.scope else [(0, source_frames)]
    deleted = subtract(proposal.intervals, on_timeline)
    return normalize(subtract(edited, deleted) + on_timeline)


def write_excerpt(
    audio_path: Path, ranges: Sequence[tuple[int, int]], destination: Path
) -> int:
    """Write ``ranges`` of ``audio_path``, in order, into one WAV of the same
    format, sample for sample; return its length in frames."""

    info = sf.info(str(audio_path))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    written = 0
    try:
        with (
            sf.SoundFile(str(audio_path)) as source,
            sf.SoundFile(
                str(temporary),
                "w",
                samplerate=info.samplerate,
                channels=info.channels,
                subtype=info.subtype,
                format="WAV",
            ) as excerpt,
        ):
            for start, end in ranges:
                source.seek(start)
                remaining = end - start
                while remaining > 0:
                    block = source.read(
                        min(remaining, EXCERPT_BLOCK_FRAMES),
                        dtype="float64",
                        always_2d=True,
                    )
                    if not len(block):
                        raise ScopeError(f"{audio_path} ends before sample {end}")
                    excerpt.write(block)
                    remaining -= len(block)
                    written += len(block)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return written


class ExcerptMap:
    """Where each sample of the excerpt of ``ranges`` is in the recording."""

    def __init__(self, ranges: Sequence[tuple[int, int]]) -> None:
        self.ranges = normalize(ranges)
        if not self.ranges:
            raise ScopeError("an excerpt needs at least one part of the recording")
        lengths = [end - start for start, end in self.ranges]
        self.offsets = list(itertools.accumulate(lengths[:-1], initial=0))
        self.frames = self.offsets[-1] + lengths[-1]
        # Excerpt positions where one part ends and the next begins.
        self.cuts = frozenset(self.offsets[1:])

    def _part(self, position: float, end: bool) -> int:
        index = bisect.bisect_right(self.offsets, position) - 1
        if end and index > 0 and position == self.offsets[index]:
            index -= 1  # the end of the part before, not the start of this one
        return max(0, index)

    def sample(self, position: int, *, end: bool = False) -> int:
        """The recording's sample at excerpt ``position``: for a position
        where two parts meet, the end of the first if ``end``, else the start
        of the second."""

        if not 0 <= position <= self.frames:
            raise ScopeError(f"sample {position} is outside the excerpt")
        index = self._part(position, end)
        return self.ranges[index][0] + position - self.offsets[index]

    def seconds(self, value: float, sample_rate: int, *, end: bool = False) -> float:
        position = min(max(float(value) * sample_rate, 0.0), float(self.frames))
        index = self._part(position, end)
        return (self.ranges[index][0] + position - self.offsets[index]) / sample_rate

    def ranges_of(self, start: int, end: int) -> Ranges:
        """The recording's ranges that the excerpt's ``start..end`` plays."""

        result: Ranges = []
        for (source_start, source_end), offset in zip(
            self.ranges, self.offsets, strict=True
        ):
            low = max(start, offset)
            high = min(end, offset + source_end - source_start)
            if low < high:
                result.append(
                    (source_start + low - offset, source_start + high - offset)
                )
        return result


def _fit(fade_in: int, fade_out: int, length: int) -> tuple[int, int]:
    """Keep both fades inside a (possibly very short) piece."""

    if fade_in + fade_out > length:
        fade_in = min(fade_in, length // 2)
        fade_out = min(fade_out, length - fade_in)
    return fade_in, fade_out


def _map_trace(
    trace: Sequence[Sequence[int]], excerpt: ExcerptMap, sample_rate: int
) -> Ranges:
    """A room tone trace of the excerpt in the recording.

    The renderer crossfades each range of a trace into the next over
    ``AMBIENCE_CROSSFADE_SECONDS``.  A range running across a cut becomes two,
    with one crossfade more; a further range of the same quiet audio, two
    crossfades long, makes up the samples that crossfade takes.
    """

    overlap = int(cut_render.AMBIENCE_CROSSFADE_SECONDS * sample_rate)
    mapped: Ranges = []
    for start, end in trace:
        pieces = excerpt.ranges_of(int(start), int(end))
        mapped += pieces
        if len(pieces) > 1:
            first, last = max(pieces, key=lambda piece: piece[1] - piece[0])
            mapped += [(first, min(last, first + 2 * overlap))] * (len(pieces) - 1)
    return mapped


def map_plan(
    plan: dict[str, Any],
    excerpt: ExcerptMap,
    *,
    excerpt_plan: Path,
    source_audio: Path,
    source_audio_sha256: str,
    source_frames: int,
    declick: int,
) -> dict[str, Any]:
    """The sealed plan VoiceCut made of the excerpt, in the recording's samples.

    Everything the editor reads from a plan is mapped: the kept intervals and
    their fades, the joins, every piece's place in the output and the room
    tone it plays, and the words.  ``editor_scope`` records the parts.
    """

    rate = int(plan["source_sample_rate"])
    if plan.get("source_frame_count") != excerpt.frames:
        raise ScopeError("Voice Cut's plan was not made from this excerpt")
    raw_intervals = plan["source_intervals"]
    raw_joins = plan.get("joins") or []

    intervals: list[dict[str, Any]] = []
    # Excerpt span of each new interval, and the new intervals of each old one.
    spans: list[tuple[int, int]] = []
    parts_of: list[list[int]] = []
    for interval in raw_intervals:
        start = int(interval["source_start_sample"])
        end = int(interval["source_end_sample"])
        first = len(intervals)
        cursor = start
        for source_start, source_end in excerpt.ranges_of(start, end):
            length = source_end - source_start
            intervals.append(
                {
                    **interval,
                    "source_interval_index": len(intervals),
                    "source_start_sample": source_start,
                    "source_end_sample": source_end,
                }
            )
            spans.append((cursor, cursor + length))
            cursor += length
        if len(intervals) == first:
            raise ScopeError("Voice Cut kept an interval outside the excerpt")
        parts_of.append(list(range(first, len(intervals))))

    joins: list[dict[str, Any]] = []
    new_join: dict[int, int] = {}
    for index, parts in enumerate(parts_of):
        for left, right in itertools.pairwise(parts):
            joins.append(
                {
                    "join_id": f"timeline_cut_{len(joins):05d}",
                    "join_kind": "timeline_cut",
                    "left_source_interval_index": left,
                    "right_source_interval_index": right,
                    "inserted_pause_samples": 0,
                    "contiguous_in_source": False,
                }
            )
        if index + 1 < len(parts_of):
            left, right = parts[-1], parts_of[index + 1][0]
            join = dict(raw_joins[index]) if index < len(raw_joins) else {}
            for key, end in JOIN_TIMES.items():
                if isinstance(join.get(key), (int, float)):
                    join[key] = excerpt.seconds(join[key], rate, end=end)
            join.update(
                left_source_interval_index=left,
                right_source_interval_index=right,
                contiguous_in_source=intervals[left]["source_end_sample"]
                == intervals[right]["source_start_sample"],
            )
            new_join[index] = len(joins)
            joins.append(join)

    pieces: list[cut_render.Piece] = []
    for piece in plan["pieces"]:
        fade_in = int(piece["fade_in_samples"])
        fade_out = int(piece["fade_out_samples"])
        if piece["kind"] == "source":
            parts = parts_of[int(piece["source_interval_index"])]
            for number, part in enumerate(parts):
                span_start, span_end = spans[part]
                part_in = fade_in if number == 0 else 0
                part_out = fade_out if number == len(parts) - 1 else 0
                if span_start in excerpt.cuts:
                    part_in = max(part_in, declick)
                if span_end in excerpt.cuts:
                    part_out = max(part_out, declick)
                part_in, part_out = _fit(part_in, part_out, span_end - span_start)
                pieces.append(
                    cut_render.Piece(
                        "source",
                        intervals[part]["source_start_sample"],
                        intervals[part]["source_end_sample"],
                        fade_in=part_in,
                        fade_out=part_out,
                        interval_index=part,
                    )
                )
            continue
        join = piece.get("join_index")
        pieces.append(
            cut_render.Piece(
                "room_tone",
                0,
                int(piece["output_end_sample"]) - int(piece["output_start_sample"]),
                fade_in=fade_in,
                fade_out=fade_out,
                join_index=None if join is None else new_join[int(join)],
                trace=_map_trace(
                    piece.get("ambience_source_ranges") or [], excerpt, rate
                ),
            )
        )
    placed = cut_render.place_pieces(pieces)

    words = []
    for word in plan.get("words") or []:
        mapped = dict(word)
        for key, end in WORD_TIMES.items():
            if isinstance(word.get(key), (int, float)):
                mapped[key] = excerpt.seconds(word[key], rate, end=end)
        words.append(mapped)

    return {
        **plan,
        "source_audio": str(source_audio),
        "source_audio_sha256": source_audio_sha256,
        "source_frame_count": source_frames,
        "expected_output_frame_count": placed[-1]["output_end_sample"],
        "words": words,
        "source_intervals": intervals,
        "joins": joins,
        "pieces": placed,
        "output_segments": cut_render._contiguous_trace(placed),
        "editor_scope": {
            "ranges": [list(part) for part in excerpt.ranges],
            "excerpt_plan": str(excerpt_plan),
            "excerpt_plan_sha256": sha256_file(excerpt_plan),
            "excerpt_frames": excerpt.frames,
            "declick_samples": declick,
        },
    }
