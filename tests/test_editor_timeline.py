"""VoiceCut proposals and editor timeline validation (voicecut.editor.timeline)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from voicecut.common import write_json
from voicecut.editor.peaks import BASE_SPP, compute_base_peaks, fine_peaks
from voicecut.editor.timeline import (
    TimelineError,
    full_source_items,
    load_proposal,
    matches_proposal,
    timeline_revision,
    validate_timeline,
)

from .editor_support import FIXTURE_FRAMES, fixture_plan


@pytest.fixture
def proposal(tmp_path: Path):
    path = tmp_path / "final_boundary_plan.json"
    write_json(path, fixture_plan())
    return load_proposal(path)


def _timeline(proposal, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "proposal_sha256": proposal.plan_sha256,
        "items": items,
    }


def _clips(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item["kind"] == "clip"]


def test_proposal_exposes_intervals_fades_pauses_and_words(proposal) -> None:
    assert proposal.intervals == (
        (24_000, 72_000),
        (90_000, 140_000),
        (200_000, 260_000),
        (300_000, 420_000),
    )
    assert proposal.fades[1] == (480, 192)
    assert proposal.joins[0] is not None and proposal.joins[0].length == 19_680
    assert proposal.joins[1] is None
    assert proposal.lead is not None and proposal.tail is not None
    assert proposal.join_between(72_000, 90_000) == 0
    assert proposal.join_between(72_000, 200_000) is None
    client = proposal.to_client()
    assert client["segments"][0] == {
        "id": "vc-0000",
        "start": 24_000,
        "end": 72_000,
        "fade_in": 480,
        "fade_out": 480,
    }
    assert client["words"][2] == {
        "text": "Dropped",
        "start": 74_400,
        "end": 88_800,
        "kept": False,
    }


def test_proposal_rejects_plans_of_older_renderers(tmp_path: Path) -> None:
    plan = fixture_plan()
    plan["renderer"] = "something_else"
    write_json(tmp_path / "plan.json", plan)
    with pytest.raises(TimelineError, match="older VoiceCut"):
        load_proposal(tmp_path / "plan.json")
    plan = fixture_plan()
    plan["planner"] = "authoritative_single_pass_boundary_plan_v1"
    write_json(tmp_path / "plan.json", plan)
    with pytest.raises(TimelineError, match="planner"):
        load_proposal(tmp_path / "plan.json")


def test_lead_in_of_older_plans_keeps_its_fade(tmp_path: Path, proposal) -> None:
    # Older plans record 0 as the first piece's fade-in; it was rendered with 50 ms.
    plan = fixture_plan()
    plan["pieces"][0]["fade_in_samples"] = 0
    write_json(tmp_path / "older.json", plan)
    assert load_proposal(tmp_path / "older.json").lead == proposal.lead


def test_proposal_timeline_has_its_pauses_in_place(proposal) -> None:
    items = proposal.timeline_items()
    assert [(item["kind"], item.get("join"), item.get("length")) for item in items] == [
        ("pause", "lead", 16_320),
        ("clip", None, None),
        ("pause", 0, 18_720),
        ("clip", None, None),
        ("clip", None, None),
        ("pause", 2, 3_840),
        ("clip", None, None),
        ("pause", "tail", 28_800),
    ]


def test_validation_accepts_the_proposal_and_normalises(proposal) -> None:
    raw = _timeline(proposal, proposal.timeline_items())
    raw["items"][1]["extra"] = "dropped"
    timeline = validate_timeline(raw, source_frames=FIXTURE_FRAMES, proposal=proposal)
    assert "extra" not in timeline["items"][1]
    assert matches_proposal(timeline, proposal)
    assert timeline_revision(timeline) == timeline_revision(
        validate_timeline(
            _timeline(proposal, proposal.timeline_items()),
            source_frames=FIXTURE_FRAMES,
            proposal=proposal,
        )
    )
    resized = _timeline(proposal, proposal.timeline_items())
    resized["items"][0]["length"] += 1
    assert not matches_proposal(
        validate_timeline(resized, source_frames=FIXTURE_FRAMES, proposal=proposal),
        proposal,
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: c[0].update(source_end_sample=24_000), "must satisfy"),
        (lambda c: c[0].update(source_end_sample=73_000), "not a VoiceCut end"),
        (lambda c: c[0].update(start_origin="source"), "media edge"),
        (lambda c: c[0].update(start_origin="robot"), "unknown"),
        (lambda c: c[1].update(id="vc-0000"), "duplicated"),
        (
            lambda c: c[3].update(
                source_end_sample=FIXTURE_FRAMES + 1, end_origin="manual"
            ),
            "must satisfy",
        ),
        (lambda c: c[0].update(source_start_sample=1.5), "integer"),
    ],
)
def test_validation_rejects_broken_clips(proposal, change, message: str) -> None:
    items = proposal.timeline_items()
    change(_clips(items))
    with pytest.raises(TimelineError, match=message):
        validate_timeline(
            _timeline(proposal, items), source_frames=FIXTURE_FRAMES, proposal=proposal
        )


def test_clips_may_come_in_any_order_and_more_than_once(proposal) -> None:
    clips = _clips(proposal.timeline_items())
    copy = {**clips[0], "id": "m-copy"}
    overlapping = {**clips[1], "id": "m-wide", "source_start_sample": 30_000}
    overlapping["start_origin"] = "manual"
    order = [clips[2], clips[0], overlapping, copy, clips[3]]
    timeline = validate_timeline(
        _timeline(proposal, order), source_frames=FIXTURE_FRAMES, proposal=proposal
    )
    assert [item["id"] for item in timeline["items"]] == [
        "vc-0002",
        "vc-0000",
        "m-wide",
        "m-copy",
        "vc-0003",
    ]
    assert not matches_proposal(timeline, proposal)


def _pause(
    length: int, *, id: str, origin: str = "manual", join: Any = None
) -> dict[str, Any]:
    return {"kind": "pause", "id": id, "length": length, "origin": origin, "join": join}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda items: items.insert(1, _pause(100, id="m-1")), "follows another pause"),
        (lambda items: items[0].update(length=0), "invalid length"),
        (lambda items: items[0].update(length=601 * 48_000), "invalid length"),
        (lambda items: items[2].update(join=1), "not one of VoiceCut's pauses"),
        (lambda items: items[2].update(join="lead"), "appears twice"),
        (lambda items: items[0].update(origin="robot"), "unknown origin"),
        (lambda items: items[0].update(kind="gap"), "unknown kind"),
    ],
)
def test_validation_rejects_broken_pauses(proposal, change, message: str) -> None:
    items = proposal.timeline_items()
    change(items)
    with pytest.raises(TimelineError, match=message):
        validate_timeline(
            _timeline(proposal, items), source_frames=FIXTURE_FRAMES, proposal=proposal
        )


def test_manual_pauses_and_video_projects(proposal) -> None:
    items = proposal.timeline_items()
    items.insert(4, _pause(48_000, id="m-1", join=3))
    timeline = validate_timeline(
        _timeline(proposal, items), source_frames=FIXTURE_FRAMES, proposal=proposal
    )
    assert timeline["items"][4] == _pause(
        48_000, id="m-1"
    )  # a manual pause refers to no join
    with pytest.raises(TimelineError, match="video"):
        validate_timeline(
            _timeline(proposal, items),
            source_frames=FIXTURE_FRAMES,
            proposal=proposal,
            allow_pauses=False,
        )


def test_validation_ties_a_timeline_to_its_proposal(proposal) -> None:
    raw = _timeline(proposal, proposal.timeline_items())
    raw["proposal_sha256"] = "older"
    with pytest.raises(TimelineError, match="different Voice Cut result"):
        validate_timeline(raw, source_frames=FIXTURE_FRAMES, proposal=proposal)
    # Before Voice Cut there are no VoiceCut origins or pauses to reference.
    with pytest.raises(TimelineError, match="not a VoiceCut"):
        validate_timeline(
            {"proposal_sha256": None, "items": _clips(proposal.timeline_items())},
            source_frames=FIXTURE_FRAMES,
            proposal=None,
        )
    whole = validate_timeline(
        {"proposal_sha256": None, "items": full_source_items(FIXTURE_FRAMES)},
        source_frames=FIXTURE_FRAMES,
        proposal=None,
    )
    assert not matches_proposal(whole, proposal)
    with pytest.raises(TimelineError, match="at least one clip"):
        validate_timeline(
            {"proposal_sha256": None, "items": [_pause(10, id="m")]},
            source_frames=FIXTURE_FRAMES,
            proposal=None,
        )


def test_base_and_fine_peaks_are_true_min_max(tmp_path: Path) -> None:
    rng = np.random.default_rng(5)
    audio = (rng.standard_normal((BASE_SPP * 9 + 77, 2)) * 0.2).astype(np.float32)
    audio[300, 1] = 0.9
    path = tmp_path / "audio.wav"
    sf.write(path, audio, 48_000, subtype="FLOAT")
    output = tmp_path / "peaks.bin"
    assert compute_base_peaks(path, output) == 10
    values = np.frombuffer(output.read_bytes(), "<i2").reshape(-1, 2)
    for index, (low, high) in enumerate(values):
        block = audio[index * BASE_SPP : (index + 1) * BASE_SPP]
        assert low == round(block.min() * 32767)
        assert high == round(block.max() * 32767)
    assert values[1, 1] == round(0.9 * 32767)
    fine = np.frombuffer(fine_peaks(path, spp=32, first_bin=8, count=4), "<i2").reshape(
        -1, 2
    )
    for index, (low, high) in enumerate(fine):
        block = audio[(8 + index) * 32 : (9 + index) * 32]
        assert (low, high) == (round(block.min() * 32767), round(block.max() * 32767))
    assert fine_peaks(path, spp=32, first_bin=10**6, count=4) == b""
    with pytest.raises(ValueError):
        fine_peaks(path, spp=7, first_bin=0, count=4)
