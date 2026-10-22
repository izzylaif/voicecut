"""Voice Cut on the parts left on the timeline: scope, excerpt, mapped plan."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from voicecut import cut_render
from voicecut.common import sha256_file, write_json
from voicecut.editor import plan as editor_plan
from voicecut.editor.scope import (
    ExcerptMap,
    ScopeError,
    map_plan,
    normalize,
    subtract,
    timeline_scope,
    write_excerpt,
)
from voicecut.editor.timeline import load_proposal, validate_timeline

from .editor_support import FIXTURE_FRAMES, FIXTURE_RATE, fixture_plan

RECORDING_FRAMES = 700_000
# Three parts of the recording, 480 000 samples together: the fixture plan's
# excerpt.  Its cuts fall inside the interval 90 000..140 000 (at 100 000) and
# exactly where the interval 200 000..260 000 starts.
PARTS = [(10_000, 110_000), (150_000, 250_000), (300_000, 580_000)]


def _clip(start: int, end: int, name: str = "c") -> dict[str, Any]:
    return {
        "kind": "clip",
        "id": name,
        "source_start_sample": start,
        "source_end_sample": end,
        "start_origin": "manual",
        "end_origin": "manual",
    }


def _pieces(records: list[dict[str, Any]]) -> list[cut_render.Piece]:
    return [
        cut_render.Piece(
            record["kind"],
            record["source_start_sample"] if record["kind"] == "source" else 0,
            record["source_end_sample"]
            if record["kind"] == "source"
            else record["output_end_sample"] - record["output_start_sample"],
            fade_in=record["fade_in_samples"],
            fade_out=record["fade_out_samples"],
            interval_index=record["source_interval_index"],
            join_index=record["join_index"],
            trace=[tuple(item) for item in record["ambience_source_ranges"]],
        )
        for record in records
    ]


@pytest.fixture
def recording(tmp_path: Path) -> tuple[Path, np.ndarray]:
    audio = np.random.default_rng(7).standard_normal((RECORDING_FRAMES, 1))
    audio = (0.1 * audio).astype(np.float32)
    path = tmp_path / "source_audio.wav"
    sf.write(path, audio, FIXTURE_RATE, subtype="FLOAT")
    return path, audio


def _mapped(
    tmp_path: Path, path: Path, plan: dict[str, Any], declick: int
) -> dict[str, Any]:
    excerpt_plan = tmp_path / "excerpt_plan.json"
    write_json(excerpt_plan, plan)
    return map_plan(
        plan,
        ExcerptMap(PARTS),
        excerpt_plan=excerpt_plan,
        source_audio=path,
        source_audio_sha256=sha256_file(path),
        source_frames=RECORDING_FRAMES,
        declick=declick,
    )


def test_ranges_merge_and_subtract() -> None:
    assert normalize([(5, 9), (0, 3), (3, 4), (8, 12), (6, 6)]) == [(0, 4), (5, 12)]
    assert subtract([(0, 10), (20, 30)], [(2, 4), (8, 22), (29, 40)]) == [
        (0, 2),
        (4, 8),
        (22, 29),
    ]
    assert subtract([(0, 10)], []) == [(0, 10)]


def test_the_scope_keeps_what_the_user_kept_and_reconsiders_voicecuts_cuts(
    tmp_path: Path,
) -> None:
    # Before Voice Cut: the clips on the timeline, a split's halves merged.
    clips = [_clip(0, 100, "a"), _clip(100, 200, "b"), _clip(500, 900, "c")]
    assert timeline_scope(clips, None, 1000) == [(0, 200), (500, 900)]

    # After a Voice Cut of the whole recording that kept 50..300 and 400..700.
    plan = fixture_plan()
    path = tmp_path / "plan.json"
    write_json(path, plan)
    proposal = load_proposal(path)
    assert proposal.scope is None
    kept = [
        _clip(start, end, f"k{index}")
        for index, (start, end) in enumerate(proposal.intervals)
    ]
    # Untouched, a new run edits the whole recording again.
    assert timeline_scope(kept, proposal, FIXTURE_FRAMES) == [(0, FIXTURE_FRAMES)]
    # The user deleted the second interval and trimmed the last one's end: a
    # new run leaves both out, but decides again on what Voice Cut removed.
    edited = [kept[0], kept[2], _clip(300_000, 400_000, "k3")]
    assert timeline_scope(edited, proposal, FIXTURE_FRAMES) == [
        (0, 90_000),
        (140_000, 400_000),
        (420_000, FIXTURE_FRAMES),
    ]
    # Something put back (this edge dragged past where it was) stays in.
    edited[0] = _clip(10_000, 72_000, "k0")
    assert timeline_scope(edited, proposal, FIXTURE_FRAMES)[0] == (0, 90_000)


def test_the_excerpt_and_its_map(recording, tmp_path: Path) -> None:
    path, audio = recording
    excerpt = tmp_path / "parts" / "excerpt.wav"
    assert write_excerpt(path, PARTS, excerpt) == FIXTURE_FRAMES
    written, rate = sf.read(excerpt, dtype="float32", always_2d=True)
    assert rate == FIXTURE_RATE and sf.info(str(excerpt)).subtype == "FLOAT"
    assert np.array_equal(
        written, np.concatenate([audio[start:end] for start, end in PARTS])
    )

    places = ExcerptMap(PARTS)
    assert places.frames == FIXTURE_FRAMES and places.cuts == {100_000, 200_000}
    assert places.sample(0) == 10_000 and places.sample(99_999) == 109_999
    # Where two parts meet: the first one's end, or the next one's start.
    assert places.sample(100_000, end=True) == 110_000
    assert places.sample(100_000) == 150_000
    assert places.sample(FIXTURE_FRAMES, end=True) == 580_000
    assert places.seconds(2.0, FIXTURE_RATE) == pytest.approx(106_000 / FIXTURE_RATE)
    assert places.seconds(2.5, FIXTURE_RATE) == pytest.approx(170_000 / FIXTURE_RATE)
    assert places.ranges_of(90_000, 210_000) == [
        (100_000, 110_000),
        (150_000, 250_000),
        (300_000, 310_000),
    ]
    with pytest.raises(ScopeError):
        places.sample(FIXTURE_FRAMES + 1)


def test_the_plan_of_the_excerpt_plays_the_same_from_the_recording(
    recording, tmp_path: Path
) -> None:
    path, audio = recording
    excerpt = tmp_path / "excerpt.wav"
    write_excerpt(path, PARTS, excerpt)
    excerpt_audio, _ = sf.read(excerpt, dtype="float32", always_2d=True)
    plan = fixture_plan()

    # Without declicks the recording plays exactly what the excerpt played.
    mapped = _mapped(tmp_path, path, plan, declick=0)
    heard, _ = cut_render.render_pieces(
        excerpt_audio, _pieces(plan["pieces"]), FIXTURE_RATE
    )
    played, _ = cut_render.render_pieces(audio, _pieces(mapped["pieces"]), FIXTURE_RATE)
    assert np.array_equal(heard, played)
    assert mapped["expected_output_frame_count"] == plan["expected_output_frame_count"]

    assert [
        (item["source_start_sample"], item["source_end_sample"])
        for item in mapped["source_intervals"]
    ] == [
        (34_000, 82_000),
        (100_000, 110_000),  # 90 000..140 000, split at the cut
        (150_000, 190_000),
        (300_000, 360_000),
        (400_000, 520_000),
    ]
    assert [join.get("join_kind") for join in mapped["joins"]] == [
        None,
        "timeline_cut",
        None,
        None,
    ]
    second = next(word for word in mapped["words"] if word["text"] == "Second")
    assert second["start"] == pytest.approx((10_000 + 91_200) / FIXTURE_RATE)
    assert second["end"] == pytest.approx((150_000 + 39_200) / FIXTURE_RATE)
    assert mapped["editor_scope"]["ranges"] == [list(part) for part in PARTS]

    # The editor reads it like any plan, and its timeline plays the same.
    mapped_path = tmp_path / "mapped.json"
    write_json(mapped_path, mapped)
    proposal = load_proposal(mapped_path)
    assert proposal.scope == tuple(PARTS)
    assert len(proposal.intervals) == 5 and len(proposal.joins) == 4
    items = proposal.timeline_items()
    timeline = validate_timeline(
        {"schema_version": 3, "proposal_sha256": proposal.plan_sha256, "items": items},
        source_frames=RECORDING_FRAMES,
        proposal=proposal,
        sample_rate=FIXTURE_RATE,
    )
    derivation = editor_plan.derive_pieces(
        timeline["items"], proposal, sample_rate=FIXTURE_RATE, declick=0
    )
    edited, _ = cut_render.render_pieces(audio, derivation.pieces, FIXTURE_RATE)
    assert np.array_equal(edited, played)
    # Unedited, the next run edits the same parts again.
    assert (
        timeline_scope(
            [item for item in items if item["kind"] == "clip"],
            proposal,
            RECORDING_FRAMES,
        )
        == PARTS
    )

    # With the editor's declick the cut inside an interval gets a crossfade;
    # the interval starting at a cut already fades in over more than that.
    declick = editor_plan.declick_samples(FIXTURE_RATE)
    mapped = _mapped(tmp_path, path, plan, declick=declick)
    assert (
        mapped["expected_output_frame_count"]
        == plan["expected_output_frame_count"] - declick
    )
    split = [piece for piece in mapped["pieces"] if piece["kind"] == "source"][1:3]
    assert [split[0]["fade_out_samples"], split[1]["fade_in_samples"]] == [
        declick,
        declick,
    ]
    write_json(mapped_path, mapped)
    proposal = load_proposal(mapped_path)
    derivation = editor_plan.derive_pieces(
        proposal.timeline_items(), proposal, sample_rate=FIXTURE_RATE, declick=declick
    )
    assert derivation.output_frames == mapped["expected_output_frame_count"]


def test_room_tone_across_a_cut_keeps_its_length_from_the_same_quiet_audio(
    recording, tmp_path: Path
) -> None:
    path, audio = recording
    plan = copy.deepcopy(fixture_plan())
    tone = next(piece for piece in plan["pieces"] if piece["join_index"] == 2)
    length = tone["output_end_sample"] - tone["output_start_sample"]
    tone["ambience_source_ranges"] = [[195_000, 195_000 + length]]
    mapped = _mapped(tmp_path, path, plan, declick=0)
    trace = next(
        piece["ambience_source_ranges"]
        for piece in mapped["pieces"]
        if piece["kind"] == "room_tone"
        and piece["join_index"] is not None
        and piece["output_end_sample"] - piece["output_start_sample"] == length
    )
    overlap = int(cut_render.AMBIENCE_CROSSFADE_SECONDS * FIXTURE_RATE)
    assert trace == [
        [245_000, 250_000],
        [300_000, 300_000 + length - 5_000],
        [245_000, 245_000 + 2 * overlap],
    ]
    assembled = cut_render._assemble_room_tone(
        audio, [tuple(item) for item in trace], length, FIXTURE_RATE
    )
    assert np.all(np.abs(assembled[-overlap:]) > 0)


def test_a_plan_of_other_audio_is_refused(recording, tmp_path: Path) -> None:
    path, _ = recording
    plan = fixture_plan()
    plan["source_frame_count"] += 1
    with pytest.raises(ScopeError, match="not made from this excerpt"):
        _mapped(tmp_path, path, plan, declick=0)
