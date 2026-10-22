"""VoiceCut's proposal and the user's editable timeline.

* :class:`Proposal` is the immutable view of a sealed
  ``05_final/final_boundary_plan.json`` the editor needs: the retained source
  intervals with the fades VoiceCut gave them, the pause (room tone) VoiceCut
  inserted at every join, the lead-in/lead-out room tone and the transcript
  words.  ``Proposal.to_client`` is exactly what the browser uses to lay the
  timeline out, so browser and exporter agree on every output sample.
* An editor timeline is an ordered list of items: clips — non-overlapping
  source intervals in canonical WAV samples, in recording order — and pauses
  of room tone between them (or before the first and after the last clip).
  Each clip edge carries its provenance: ``voicecut`` (the sample VoiceCut
  chose for this kind of edge), ``manual`` (placed by the user) or ``source``
  (the start or end of the media).  A pause has a length and remembers which
  VoiceCut pause it was (``join``), if any; it stays put when the clips around
  it are edited.  :func:`validate_timeline` is the single gate every timeline
  passes before it is saved, previewed or exported.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import Any

from .. import cut_render
from ..common import read_json, sha256_file

TIMELINE_SCHEMA_VERSION = 3
PAUSE_ORIGINS = frozenset({"voicecut", "manual"})
MAX_PAUSE_SECONDS = 600
# cut_render.build_pieces: the lead-in fades in over 50 ms, the lead-out out
# over 100 ms.
LEAD_FADE_SECONDS = 0.05
TAIL_FADE_SECONDS = 0.1
ORIGINS = frozenset({"voicecut", "manual", "source"})
MEDIA_KINDS = frozenset({"audio", "video"})
SUPPORTED_PLANNERS = frozenset({cut_render.BOUNDARY_PLAN_NAME})


class TimelineError(ValueError):
    """A proposal or timeline payload is malformed or inconsistent."""


@dataclass(frozen=True)
class RoomTone:
    """One room-tone piece of the sealed plan, as placed by VoiceCut."""

    length: int
    fade_in: int
    fade_out: int
    trace: tuple[tuple[int, int], ...]
    join_index: int | None

    def to_client(self) -> dict[str, int]:
        return {
            "length": self.length,
            "fade_in": self.fade_in,
            "fade_out": self.fade_out,
        }

    def heard(self, *, after_clip: bool, before_clip: bool) -> int:
        """Length of the pause between its crossfades with the clips."""

        return (
            self.length
            - (self.fade_in if after_clip else 0)
            - (self.fade_out if before_clip else 0)
        )


@dataclass(frozen=True)
class Proposal:
    """What the editor uses from VoiceCut's sealed boundary plan."""

    plan_path: Path
    plan_sha256: str
    pause_policy: str
    sample_rate: int
    channels: int
    source_frames: int
    output_frames: int
    source_audio: Path
    source_audio_sha256: str
    intervals: tuple[tuple[int, int], ...]
    fades: tuple[tuple[int, int], ...]
    joins: tuple[RoomTone | None, ...]
    lead: RoomTone | None
    tail: RoomTone | None
    words: tuple[dict[str, Any], ...] = field(default=(), repr=False)
    #: The parts of the recording this run edited (:mod:`.scope`); ``None``
    #: for the whole recording.
    scope: tuple[tuple[int, int], ...] | None = None

    # -- lookups -----------------------------------------------------------
    def interval_starting_at(self, sample: int) -> int | None:
        return self._starts.get(sample)

    def interval_ending_at(self, sample: int) -> int | None:
        return self._ends.get(sample)

    def join_between(self, end: int, start: int) -> int | None:
        """Index of VoiceCut's join from an interval ending at ``end`` to the
        very next interval starting at ``start``."""

        index = self._ends.get(end)
        if index is None or index + 1 >= len(self.intervals):
            return None
        return index if self.intervals[index + 1][0] == start else None

    @cached_property
    def _starts(self) -> dict[int, int]:
        return {start: index for index, (start, _) in enumerate(self.intervals)}

    @cached_property
    def _ends(self) -> dict[int, int]:
        return {end: index for index, (_, end) in enumerate(self.intervals)}

    def segment_id(self, index: int) -> str:
        return f"vc-{index:04d}"

    def room_tone(self, join: Any) -> RoomTone | None:
        """VoiceCut's pause ``join``: ``"lead"``, ``"tail"`` or a join index."""

        if join == "lead":
            return self.lead
        if join == "tail":
            return self.tail
        if type(join) is int and 0 <= join < len(self.joins):
            return self.joins[join]
        return None

    def pause_item(self, join: Any) -> dict[str, Any]:
        tone = self.room_tone(join)
        assert tone is not None
        length = tone.heard(after_clip=join != "lead", before_clip=join != "tail")
        name = join if isinstance(join, str) else f"{join:04d}"
        return {
            "kind": "pause",
            "id": f"vp-{name}",
            "length": length,
            "origin": "voicecut",
            "join": join,
        }

    def timeline_items(self) -> list[dict[str, Any]]:
        """The editable timeline that reproduces VoiceCut's result exactly."""

        items: list[dict[str, Any]] = []
        if self.lead is not None:
            items.append(self.pause_item("lead"))
        for index, (start, end) in enumerate(self.intervals):
            items.append(
                {
                    "kind": "clip",
                    "id": self.segment_id(index),
                    "source_start_sample": start,
                    "source_end_sample": end,
                    "start_origin": "voicecut",
                    "end_origin": "voicecut",
                }
            )
            if index < len(self.joins) and self.joins[index] is not None:
                items.append(self.pause_item(index))
        if self.tail is not None:
            items.append(self.pause_item("tail"))
        return items

    def to_client(self) -> dict[str, Any]:
        return {
            "plan_sha256": self.plan_sha256,
            "pause_policy": self.pause_policy,
            "sample_rate": self.sample_rate,
            "source_frames": self.source_frames,
            "output_frames": self.output_frames,
            "segments": [
                {
                    "id": self.segment_id(index),
                    "start": start,
                    "end": end,
                    "fade_in": fade_in,
                    "fade_out": fade_out,
                }
                for index, ((start, end), (fade_in, fade_out)) in enumerate(
                    zip(self.intervals, self.fades, strict=True)
                )
            ],
            "joins": [join.to_client() if join else None for join in self.joins],
            "lead": self.lead.to_client() if self.lead else None,
            "tail": self.tail.to_client() if self.tail else None,
            "words": list(self.words),
        }


def _int(value: Any, name: str) -> int:
    if type(value) is not int:
        raise TimelineError(f"{name} must be an integer")
    return value


def _room_tone(piece: dict[str, Any]) -> RoomTone:
    return RoomTone(
        length=_int(piece["output_end_sample"], "piece end")
        - _int(piece["output_start_sample"], "piece start"),
        fade_in=_int(piece["fade_in_samples"], "piece fade_in"),
        fade_out=_int(piece["fade_out_samples"], "piece fade_out"),
        trace=tuple(
            (int(start), int(end)) for start, end in piece["ambience_source_ranges"]
        ),
        join_index=piece.get("join_index"),
    )


def _words(plan: dict[str, Any], sample_rate: int) -> tuple[dict[str, Any], ...]:
    """Transcript words in source samples, best available timing first."""

    words: list[dict[str, Any]] = []
    for word in plan.get("words") or []:
        if not isinstance(word, dict) or not isinstance(word.get("text"), str):
            continue
        aligned = word.get("alignment") not in (None, "whisper")
        start = word.get("core_start") if aligned else word.get("start")
        end = word.get("core_end") if aligned else word.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        words.append(
            {
                "text": word["text"],
                "start": round(float(start) * sample_rate),
                "end": round(float(end) * sample_rate),
                "kept": bool(word.get("kept", False)),
            }
        )
    return tuple(words)


def load_proposal(plan_path: Path) -> Proposal:
    """Read a sealed ``final_boundary_plan.json`` into a :class:`Proposal`."""

    plan_path = Path(plan_path).resolve()
    plan = read_json(plan_path)
    if not isinstance(plan, dict):
        raise TimelineError("final boundary plan root must be an object")
    if plan.get("planner") not in SUPPORTED_PLANNERS:
        raise TimelineError(
            f"unsupported boundary plan planner {plan.get('planner')!r}; "
            "re-run Voice Cut with this version of VoiceCut"
        )
    if plan.get("renderer") != cut_render.RENDERER_NAME:
        raise TimelineError("boundary plan was rendered by an older VoiceCut")
    if plan.get("status") != "safe":
        raise TimelineError("the boundary plan is not marked safe")
    sample_rate = _int(plan.get("source_sample_rate"), "source_sample_rate")
    source_frames = _int(plan.get("source_frame_count"), "source_frame_count")
    raw_intervals = plan.get("source_intervals")
    raw_pieces = plan.get("pieces")
    raw_joins = plan.get("joins")
    if not raw_intervals or not isinstance(raw_pieces, list):
        raise TimelineError("boundary plan has no retained intervals or pieces")
    intervals: list[tuple[int, int]] = []
    for index, interval in enumerate(raw_intervals):
        start = _int(interval.get("source_start_sample"), "interval start")
        end = _int(interval.get("source_end_sample"), "interval end")
        if not 0 <= start < end <= source_frames:
            raise TimelineError(f"source interval {index} leaves the media")
        if intervals and start < intervals[-1][1]:
            raise TimelineError(f"source interval {index} overlaps its left")
        intervals.append((start, end))
    fades: list[tuple[int, int] | None] = [None] * len(intervals)
    joins: list[RoomTone | None] = [None] * max(0, len(intervals) - 1)
    for piece in raw_pieces:
        if piece.get("kind") == "source":
            fades[piece["source_interval_index"]] = (
                int(piece["fade_in_samples"]),
                int(piece["fade_out_samples"]),
            )
        elif piece.get("join_index") is not None:
            joins[int(piece["join_index"])] = _room_tone(piece)
    if any(fade is None for fade in fades):
        raise TimelineError("boundary plan has an interval without a source piece")
    first, last = raw_pieces[0], raw_pieces[-1]
    lead = (
        _room_tone(first)
        if first.get("kind") == "room_tone" and first.get("join_index") is None
        else None
    )
    tail = (
        _room_tone(last)
        if last.get("kind") == "room_tone"
        and last.get("join_index") is None
        and last is not first
        else None
    )
    if lead is not None and lead.fade_in == 0:
        # Plans written before place_pieces recorded the first piece's own fade
        # say 0 here, but the lead-in was rendered with the usual 50 ms fade.
        lead = replace(
            lead,
            fade_in=min(
                int(LEAD_FADE_SECONDS * sample_rate), lead.length - lead.fade_out
            ),
        )
    if isinstance(raw_joins, list) and len(raw_joins) != len(joins):
        raise TimelineError("boundary plan joins do not match its intervals")
    scope = None
    if isinstance(plan.get("editor_scope"), dict):
        parts = plan["editor_scope"].get("ranges")
        if not isinstance(parts, list) or not parts:
            raise TimelineError("boundary plan has no edited parts")
        scope = tuple(
            (_int(start, "part start"), _int(end, "part end")) for start, end in parts
        )
        if any(not 0 <= start < end <= source_frames for start, end in scope):
            raise TimelineError("an edited part leaves the media")
    return Proposal(
        plan_path=plan_path,
        plan_sha256=sha256_file(plan_path),
        pause_policy=str(plan.get("pause_policy") or "semantic"),
        sample_rate=sample_rate,
        channels=_int(plan.get("source_channel_count"), "source_channel_count"),
        source_frames=source_frames,
        output_frames=_int(
            plan.get("expected_output_frame_count"), "expected_output_frame_count"
        ),
        source_audio=Path(str(plan.get("source_audio"))),
        source_audio_sha256=str(plan.get("source_audio_sha256")),
        intervals=tuple(intervals),
        fades=tuple(fade for fade in fades if fade is not None),
        joins=tuple(joins),
        lead=lead,
        tail=tail,
        words=_words(plan, sample_rate),
        scope=scope,
    )


def full_source_items(source_frames: int) -> list[dict[str, Any]]:
    """The timeline before Voice Cut: the whole recording as one clip."""

    return [
        {
            "kind": "clip",
            "id": "src-0000",
            "source_start_sample": 0,
            "source_end_sample": source_frames,
            "start_origin": "source",
            "end_origin": "source",
        }
    ]


def _items_from_segments(
    segments: list[Any], proposal: Proposal | None
) -> list[dict[str, Any]]:
    """Read a timeline of the previous format (clips only).

    Its pauses were implied: VoiceCut's pause stayed wherever both facing
    edges were VoiceCut's own and the clips were neighbours in its result.
    """

    items: list[dict[str, Any]] = []
    if proposal is not None and proposal.lead is not None:
        items.append(proposal.pause_item("lead"))
    for index, segment in enumerate(segments):
        items.append(
            {"kind": "clip", **segment} if isinstance(segment, dict) else segment
        )
        following = segments[index + 1] if index + 1 < len(segments) else None
        if (
            proposal is not None
            and isinstance(segment, dict)
            and isinstance(following, dict)
            and segment.get("end_origin") == "voicecut"
            and following.get("start_origin") == "voicecut"
        ):
            join = proposal.join_between(
                segment.get("source_end_sample"), following.get("source_start_sample")
            )
            if join is not None and proposal.joins[join] is not None:
                items.append(proposal.pause_item(join))
    if proposal is not None and proposal.tail is not None:
        items.append(proposal.pause_item("tail"))
    return items


def validate_timeline(
    timeline: Any,
    *,
    source_frames: int,
    proposal: Proposal | None,
    allow_pauses: bool = True,
    sample_rate: int | None = None,
) -> dict[str, Any]:
    """Validate a timeline payload and return its normalised form.

    The normalised timeline holds only ``schema_version``,
    ``proposal_sha256`` and ``items``; anything else is dropped.  Timelines
    of the previous format (``segments`` only) are converted.
    """

    if not isinstance(timeline, dict):
        raise TimelineError("timeline must be an object")
    version = timeline.get("schema_version", TIMELINE_SCHEMA_VERSION)
    if version not in (2, TIMELINE_SCHEMA_VERSION):
        raise TimelineError("unsupported timeline schema version")
    proposal_sha = timeline.get("proposal_sha256")
    if proposal_sha is not None and not isinstance(proposal_sha, str):
        raise TimelineError("proposal_sha256 must be a string or null")
    expected_sha = proposal.plan_sha256 if proposal else None
    if proposal_sha != expected_sha:
        raise TimelineError("timeline belongs to a different Voice Cut result")
    if version == 2:
        raw = timeline.get("segments")
        if not isinstance(raw, list):
            raise TimelineError("timeline segments must be a list")
        raw_items = _items_from_segments(raw, proposal)
    else:
        raw_items = timeline.get("items")
        if not isinstance(raw_items, list):
            raise TimelineError("timeline items must be a list")
    starts = {start for start, _ in proposal.intervals} if proposal else set()
    ends = {end for _, end in proposal.intervals} if proposal else set()
    rate = sample_rate or (proposal.sample_rate if proposal else None)
    longest = MAX_PAUSE_SECONDS * rate if rate else None
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    joins_used: set[Any] = set()
    previous_end = 0
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise TimelineError(f"item {index} must be an object")
        item_id = raw.get("id")
        if not isinstance(item_id, str) or not item_id or len(item_id) > 64:
            raise TimelineError(f"item {index} has no usable id")
        if item_id in seen:
            raise TimelineError(f"item id {item_id!r} is duplicated")
        seen.add(item_id)
        kind = raw.get("kind")
        if kind == "pause":
            items.append(
                _pause(raw, item_id, proposal, joins_used, allow_pauses, longest)
            )
            if len(items) > 1 and items[-2]["kind"] == "pause":
                raise TimelineError(f"pause {item_id} follows another pause")
            continue
        if kind != "clip":
            raise TimelineError(f"item {item_id} has an unknown kind")
        start = _int(raw.get("source_start_sample"), f"clip {item_id} start")
        end = _int(raw.get("source_end_sample"), f"clip {item_id} end")
        if not 0 <= start < end <= source_frames:
            raise TimelineError(
                f"clip {item_id} must satisfy 0 <= start < end <= "
                f"{source_frames} (got {start}..{end})"
            )
        if start < previous_end:
            raise TimelineError(f"clip {item_id} overlaps the previous clip")
        previous_end = end
        start_origin = raw.get("start_origin")
        end_origin = raw.get("end_origin")
        for origin, sample, edge, anchors, media_edge in (
            (start_origin, start, "start", starts, 0),
            (end_origin, end, "end", ends, source_frames),
        ):
            if origin not in ORIGINS:
                raise TimelineError(f"clip {item_id} has an unknown {edge} origin")
            if origin == "voicecut" and sample not in anchors:
                raise TimelineError(
                    f"clip {item_id} {edge} {sample} is not a VoiceCut {edge}"
                )
            if origin == "source" and sample != media_edge:
                raise TimelineError(f"clip {item_id} {edge} is not a media edge")
        items.append(
            {
                "kind": "clip",
                "id": item_id,
                "source_start_sample": start,
                "source_end_sample": end,
                "start_origin": start_origin,
                "end_origin": end_origin,
            }
        )
    if not any(item["kind"] == "clip" for item in items):
        raise TimelineError("timeline must contain at least one clip")
    return {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "proposal_sha256": expected_sha,
        "items": items,
    }


def _pause(
    raw: dict[str, Any],
    item_id: str,
    proposal: Proposal | None,
    joins_used: set[Any],
    allow_pauses: bool,
    longest: int | None,
) -> dict[str, Any]:
    if not allow_pauses:
        raise TimelineError("pauses are not available in video projects")
    length = _int(raw.get("length"), f"pause {item_id} length")
    if length <= 0 or (longest is not None and length > longest):
        raise TimelineError(f"pause {item_id} has an invalid length")
    origin = raw.get("origin")
    if origin not in PAUSE_ORIGINS:
        raise TimelineError(f"pause {item_id} has an unknown origin")
    join = raw.get("join")
    if origin == "voicecut":
        if proposal is None or proposal.room_tone(join) is None:
            raise TimelineError(f"pause {item_id} is not one of VoiceCut's pauses")
        if join in joins_used:
            raise TimelineError(f"VoiceCut's pause {join!r} appears twice")
        joins_used.add(join)
    else:
        join = None
    return {
        "kind": "pause",
        "id": item_id,
        "length": length,
        "origin": origin,
        "join": join,
    }


def clips_and_gaps(
    timeline: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any] | None]]:
    """The clips of a normalised timeline and the pause (or none) before each
    clip and after the last one: ``len(gaps) == len(clips) + 1``."""

    clips: list[dict[str, Any]] = []
    gaps: list[dict[str, Any] | None] = [None]
    for item in timeline["items"]:
        if item["kind"] == "clip":
            clips.append(item)
            gaps.append(None)
        else:
            gaps[-1] = item
    return clips, gaps


def timeline_revision(timeline: dict[str, Any]) -> str:
    """Content hash of a normalised timeline (its preview revision)."""

    encoded = json.dumps(
        [timeline.get("proposal_sha256"), timeline["items"]],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def matches_proposal(timeline: dict[str, Any], proposal: Proposal | None) -> bool:
    """Whether a normalised timeline reproduces VoiceCut's result untouched."""

    if proposal is None:
        return False

    def shape(item: dict[str, Any]) -> tuple[Any, ...]:
        if item["kind"] == "pause":
            return ("pause", item["length"], item["origin"], item["join"])
        return (
            "clip",
            item["source_start_sample"],
            item["source_end_sample"],
            item["start_origin"],
            item["end_origin"],
        )

    return [shape(item) for item in timeline["items"]] == [
        shape(item) for item in proposal.timeline_items()
    ]
