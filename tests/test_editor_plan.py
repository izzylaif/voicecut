"""Deriving, laying out and rendering editor timelines (voicecut.editor.plan)."""

from __future__ import annotations

import copy
import json
import os
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from voicecut import cut_render
from voicecut.common import read_json, write_json
from voicecut.editor import ambience
from voicecut.editor.plan import (
    EDITOR_PLANNER_ID,
    PlanError,
    SourceAudio,
    build_editor_plan,
    declick_samples,
    derive_pieces,
    render_plan_to_wav,
    render_range,
    validate_editor_plan,
)
from voicecut.editor.timeline import (
    TimelineError,
    load_proposal,
    validate_timeline,
)
from voicecut.video_render import build_visual_timeline

from .editor_support import (
    FIXTURE_FRAMES,
    FIXTURE_RATE,
    FRAMES,
    SAMPLE_RATE,
    fixture_plan,
    render_voicecut,
    source_audio,
    write_wav,
)

FIXTURE = (
    Path(__file__).resolve().parents[1] / "frontend/tests/fixtures/layout_cases.json"
)


def _clip(
    start: int,
    end: int,
    start_origin: str = "voicecut",
    end_origin: str = "voicecut",
    *,
    id: str,
) -> dict[str, Any]:
    return {
        "kind": "clip",
        "id": id,
        "source_start_sample": start,
        "source_end_sample": end,
        "start_origin": start_origin,
        "end_origin": end_origin,
    }


def _pause(length: int, join: Any = None, *, id: str) -> dict[str, Any]:
    return {
        "kind": "pause",
        "id": id,
        "length": length,
        "origin": "voicecut" if join is not None else "manual",
        "join": join,
    }


def _clips(items: list[dict[str, Any]]) -> list[int]:
    return [index for index, item in enumerate(items) if item["kind"] == "clip"]


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    audio = source_audio()
    wav = write_wav(tmp_path / "work/00_media/source_audio.wav", audio)
    render_voicecut(tmp_path / "work", wav, monkeypatch=monkeypatch)
    plan_path = tmp_path / "work/05_final/final_boundary_plan.json"
    proposal = load_proposal(plan_path)
    final, _ = sf.read(
        tmp_path / "work/05_final/final_cut.wav", dtype="float32", always_2d=True
    )
    runs = ambience.ambience_runs(wav, proposal, tmp_path / "ambience.json")
    return {
        "wav": wav,
        "audio": audio,
        "proposal": proposal,
        "final": final,
        "plan": read_json(plan_path),
        "room_tone": ambience.AmbienceRoomTone(runs, SAMPLE_RATE),
        "runs": runs,
    }


def _timeline(
    proposal: Any, items: list[dict[str, Any]], **options: Any
) -> dict[str, Any]:
    return validate_timeline(
        {"proposal_sha256": proposal.plan_sha256 if proposal else None, "items": items},
        source_frames=FRAMES,
        proposal=proposal,
        **options,
    )


def _render(run: dict[str, Any], items: list[dict[str, Any]]) -> tuple[np.ndarray, Any]:
    timeline = _timeline(run["proposal"], items)
    derivation = derive_pieces(
        timeline["items"],
        run["proposal"],
        sample_rate=SAMPLE_RATE,
        declick=declick_samples(SAMPLE_RATE),
        room_tone=run["room_tone"],
    )
    rendered, _ = cut_render.render_pieces(run["audio"], derivation.pieces, SAMPLE_RATE)
    return rendered, derivation


def _with_inserted_pause(
    items: list[dict[str, Any]], length: int
) -> list[dict[str, Any]]:
    """Split the last clip in the middle and put a manual pause there."""

    items = copy.deepcopy(items)
    clip = _clips(items)[-1]
    middle = (
        items[clip]["source_start_sample"] + items[clip]["source_end_sample"]
    ) // 2
    items[clip : clip + 1] = [
        {**items[clip], "source_end_sample": middle, "end_origin": "manual"},
        _pause(length, id="m-pause"),
        {
            **items[clip],
            "id": "m-right",
            "source_start_sample": middle,
            "start_origin": "manual",
        },
    ]
    return items


def _pause_pieces(
    items: list[dict[str, Any]], derivation: Any
) -> list[tuple[dict, Any, dict]]:
    """Each pause item with its room-tone piece and placed record."""

    room = [
        (piece, record)
        for piece, record in zip(derivation.pieces, derivation.placed, strict=True)
        if piece.kind == "room_tone"
    ]
    pauses = [item for item in items if item["kind"] == "pause"]
    return [
        (item, piece, record)
        for item, (piece, record) in zip(pauses, room, strict=True)
    ]


def _pause_spans(derivation: Any) -> list[int]:
    """Heard length of every pause (between its crossfades)."""

    return [
        segment["output_end_sample"] - segment["output_start_sample"]
        for segment in cut_render._contiguous_trace(derivation.placed)
        if segment["kind"] == "room_tone"
    ]


def test_untouched_timeline_reproduces_voicecut_sample_for_sample(
    run: dict[str, Any],
) -> None:
    proposal = run["proposal"]
    items = proposal.timeline_items()
    rendered, derivation = _render(run, items)
    assert np.array_equal(rendered, run["final"])
    assert derivation.output_frames == proposal.output_frames
    assert len(derivation.kept_pauses) == sum(item["kind"] == "pause" for item in items)
    assert derivation.declicks == 0 and derivation.inserted_pauses == 0


def test_trimming_next_to_a_pause_keeps_the_pause(run: dict[str, Any]) -> None:
    proposal = run["proposal"]
    items = proposal.timeline_items()
    _, original = _render(run, items)
    first = _clips(items)[0]
    assert items[first - 1]["join"] == "lead"  # VoiceCut's pause before clip 1
    items[first] = {
        **items[first],
        "source_start_sample": items[first]["source_start_sample"] - 160,
        "start_origin": "manual",
    }
    last = _clips(items)[-1]
    assert items[last + 1]["join"] == "tail"
    items[last] = {
        **items[last],
        "source_end_sample": items[last]["source_end_sample"] + 240,
        "end_origin": "manual",
    }
    rendered, derivation = _render(run, items)
    # The clips got 160 + 240 samples longer; both pauses kept their length.
    assert derivation.output_frames == original.output_frames + 400
    assert _pause_spans(derivation) == _pause_spans(original)
    assert derivation.kept_pauses == ["lead", "tail"]
    assert derivation.declicks == 2
    # Retained speech away from the fades is copied untouched.
    record = next(item for item in derivation.placed if item["kind"] == "source")
    begin = record["fade_in_samples"]
    stop = (
        record["source_end_sample"]
        - record["source_start_sample"]
        - record["fade_out_samples"]
    )
    offset = record["output_start_sample"]
    source = record["source_start_sample"]
    assert np.array_equal(
        rendered[offset + begin : offset + stop],
        run["audio"][source + begin : source + stop],
    )


def test_resized_and_inserted_pauses_are_clean_room_tone(run: dict[str, Any]) -> None:
    proposal = run["proposal"]
    items = proposal.timeline_items()
    _, original = _render(run, items)
    items[-1] = {**items[-1], "length": items[-1]["length"] + SAMPLE_RATE // 2}
    items = _with_inserted_pause(items, SAMPLE_RATE)
    rendered, derivation = _render(run, items)
    # Half a second longer pause plus a 1 s pause: exactly 1.5 s more.
    assert derivation.output_frames == original.output_frames + SAMPLE_RATE * 3 // 2
    assert derivation.inserted_pauses == 1
    _, piece, record = next(
        entry
        for entry in _pause_pieces(items, derivation)
        if entry[0]["id"] == "m-pause"
    )
    assert piece.trace, "the inserted pause takes room tone from the bank"
    for low, high in piece.trace:
        assert any(start <= low and high <= stop for start, stop in run["runs"])
    heard = rendered[
        record["output_start_sample"] + record["fade_in_samples"] : record[
            "output_end_sample"
        ]
        - record["fade_out_samples"]
    ]
    assert float(np.sqrt(np.mean(heard**2))) < 1e-3  # room tone, not speech
    with SourceAudio(run["wav"]) as source:
        cuts = sorted(
            {
                0,
                derivation.output_frames,
                *np.random.default_rng(1)
                .integers(0, derivation.output_frames, 25)
                .tolist(),
            }
        )
        parts = [
            render_range(source, derivation, low, high) for low, high in pairwise(cuts)
        ]
        assert np.array_equal(np.concatenate(parts), rendered)
        assert (
            len(render_range(source, derivation, derivation.output_frames - 10, 10**9))
            == 10
        )


def test_room_tone_is_the_same_every_time(run: dict[str, Any]) -> None:
    items = _with_inserted_pause(run["proposal"].timeline_items(), SAMPLE_RATE // 3)
    first, _ = _render(run, items)
    second, _ = _render(run, items)
    assert np.array_equal(first, second)


def test_deleting_a_pause_removes_its_room_tone(run: dict[str, Any]) -> None:
    proposal = run["proposal"]
    items = proposal.timeline_items()
    _, original = _render(run, items)
    removed = items.pop()  # the lead-out
    assert removed["join"] == "tail"
    _, derivation = _render(run, items)
    assert derivation.kept_pauses == ["lead"]
    assert derivation.output_frames == original.output_frames - removed["length"]


def test_split_clips_continue_the_audio_without_fades(run: dict[str, Any]) -> None:
    proposal = run["proposal"]
    items = proposal.timeline_items()
    clip = _clips(items)[-1]
    start, end = items[clip]["source_start_sample"], items[clip]["source_end_sample"]
    middle = (start + end) // 2
    items[clip : clip + 1] = [
        {**items[clip], "source_end_sample": middle, "end_origin": "manual"},
        {
            **items[clip],
            "id": "split",
            "source_start_sample": middle,
            "start_origin": "manual",
        },
    ]
    rendered, derivation = _render(run, items)
    assert np.array_equal(rendered, run["final"])
    assert derivation.declicks == 0


def test_short_clips_keep_their_fades_inside_the_piece(run: dict[str, Any]) -> None:
    items = run["proposal"].timeline_items()
    clip = _clips(items)[-1]
    end = items[clip]["source_end_sample"]
    items[clip] = {
        **items[clip],
        "source_start_sample": end - 50,
        "start_origin": "manual",
    }
    _, derivation = _render(run, items)
    piece = [piece for piece in derivation.pieces if piece.kind == "source"][-1]
    assert piece.fade_in + piece.fade_out <= piece.source_end - piece.source_start


def test_exported_plan_renders_exactly_like_the_preview(
    run: dict[str, Any], tmp_path: Path
) -> None:
    proposal = run["proposal"]
    items = proposal.timeline_items()
    second = _clips(items)[1]
    items[second] = {
        **items[second],
        "source_start_sample": items[second]["source_start_sample"] - 320,
        "start_origin": "manual",
    }
    items.pop()  # VoiceCut's lead-out pause
    timeline = _timeline(proposal, items)
    plan = build_editor_plan(
        timeline=timeline,
        proposal=proposal,
        media_kind="audio",
        source_audio=run["wav"],
        source_audio_sha256=proposal.source_audio_sha256,
        sample_rate=SAMPLE_RATE,
        channels=run["audio"].shape[1],
        source_frames=FRAMES,
        room_tone=run["room_tone"],
    )
    assert plan["planner"] == EDITOR_PLANNER_ID
    assert plan["manual_boundary_count"] == 1
    assert {item["id"] for item in plan["dropped"]} == {"tail"}
    output = tmp_path / "edited.wav"
    result = render_plan_to_wav(audio_path=run["wav"], plan=plan, output_path=output)
    rendered, _ = sf.read(output, dtype="float32", always_2d=True)
    assert result["frame_count"] == len(rendered) == plan["expected_output_frame_count"]
    with SourceAudio(run["wav"]) as source:
        derivation = derive_pieces(
            timeline["items"],
            proposal,
            sample_rate=SAMPLE_RATE,
            declick=declick_samples(SAMPLE_RATE),
            room_tone=run["room_tone"],
        )
        assert np.array_equal(
            render_range(source, derivation, 0, derivation.output_frames), rendered
        )


def test_validate_editor_plan_rejects_tampering(run: dict[str, Any]) -> None:
    proposal = run["proposal"]
    plan = build_editor_plan(
        timeline=_timeline(proposal, proposal.timeline_items()),
        proposal=proposal,
        media_kind="audio",
        source_audio=run["wav"],
        source_audio_sha256=proposal.source_audio_sha256,
        sample_rate=SAMPLE_RATE,
        channels=2,
        source_frames=FRAMES,
    )
    broken = json.loads(json.dumps(plan))
    broken["expected_output_frame_count"] += 1
    with pytest.raises(PlanError):
        validate_editor_plan(broken)
    broken = json.loads(json.dumps(plan))
    broken["pieces"][1]["fade_in"] = 10**7
    with pytest.raises(PlanError):
        validate_editor_plan(broken)


def test_timelines_of_the_previous_format_keep_their_pauses(
    run: dict[str, Any],
) -> None:
    proposal = run["proposal"]
    segments = [
        {key: value for key, value in item.items() if key != "kind"}
        for item in proposal.timeline_items()
        if item["kind"] == "clip"
    ]
    converted = validate_timeline(
        {
            "schema_version": 2,
            "proposal_sha256": proposal.plan_sha256,
            "segments": segments,
        },
        source_frames=FRAMES,
        proposal=proposal,
    )
    assert converted["items"] == proposal.timeline_items()
    # A boundary the user had moved had lost VoiceCut's pause then; it stays lost.
    segments[0] = {
        **segments[0],
        "source_end_sample": segments[0]["source_end_sample"] + 16,
        "end_origin": "manual",
    }
    converted = validate_timeline(
        {
            "schema_version": 2,
            "proposal_sha256": proposal.plan_sha256,
            "segments": segments,
        },
        source_frames=FRAMES,
        proposal=proposal,
    )
    assert not any(
        item["kind"] == "pause" and item["join"] == 0 for item in converted["items"]
    )


def test_video_timelines_have_no_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = source_audio()
    wav = write_wav(tmp_path / "work/00_media/source_audio.wav", audio)
    render_voicecut(tmp_path / "work", wav, monkeypatch=monkeypatch, policy="cuts")
    proposal = load_proposal(tmp_path / "work/05_final/final_boundary_plan.json")
    items = proposal.timeline_items()
    assert all(item["kind"] == "clip" for item in items)
    items[0] = {
        **items[0],
        "source_end_sample": items[0]["source_end_sample"] + 800,
        "end_origin": "manual",
    }
    plan = build_editor_plan(
        timeline=_timeline(proposal, items, allow_pauses=False),
        proposal=proposal,
        media_kind="video",
        source_audio=wav,
        source_audio_sha256=proposal.source_audio_sha256,
        sample_rate=SAMPLE_RATE,
        channels=2,
        source_frames=FRAMES,
    )
    timeline, _, frames = build_visual_timeline(plan)
    assert frames == plan["expected_output_frame_count"]
    assert timeline[0].source_start_sample == items[0]["source_start_sample"]
    with pytest.raises(TimelineError, match="video"):
        _timeline(proposal, [*items, _pause(1000, id="m-p")], allow_pauses=False)


# ---------------------------------------------------------------------------
# Browser layout parity (frontend/src/model/layout.ts)


def _cases() -> list[tuple[str, bool, list[dict[str, Any]]]]:
    vc = [(24_000, 72_000), (90_000, 140_000), (200_000, 260_000), (300_000, 420_000)]
    pauses = {"lead": 16_320, 0: 18_720, 2: 3_840, "tail": 28_800}

    def proposal_items() -> list[dict[str, Any]]:
        items = [_pause(pauses["lead"], "lead", id="vp-lead")]
        for index, (start, end) in enumerate(vc):
            items.append(_clip(start, end, id=f"vc-{index:04d}"))
            if index in pauses:
                items.append(_pause(pauses[index], index, id=f"vp-{index:04d}"))
        items.append(_pause(pauses["tail"], "tail", id="vp-tail"))
        return items

    def clip_at(items: list[dict[str, Any]], number: int) -> int:
        return _clips(items)[number]

    cases: list[tuple[str, bool, list[dict[str, Any]]]] = [
        ("untouched", True, proposal_items())
    ]
    items = proposal_items()
    items[clip_at(items, 0)] = _clip(24_000, 80_000, "voicecut", "manual", id="vc-0000")
    cases.append(("extended end keeps the pause", True, items))
    items = proposal_items()
    items[clip_at(items, 1)] = _clip(
        95_000, 140_000, "manual", "voicecut", id="vc-0001"
    )
    cases.append(("trimmed start", True, items))
    items = proposal_items()
    del items[clip_at(items, 2) : clip_at(items, 2) + 2]
    cases.append(("deleted clip and its pause", True, items))
    items = proposal_items()
    at = clip_at(items, 3)
    items[at : at + 1] = [
        _clip(300_000, 360_000, "voicecut", "manual", id="vc-0003"),
        _clip(360_000, 420_000, "manual", "voicecut", id="m-split"),
    ]
    cases.append(("split", True, items))
    items = proposal_items()
    items[at : at + 1] = [
        _clip(300_000, 360_000, "voicecut", "manual", id="vc-0003"),
        _pause(48_000, id="m-pause"),
        _clip(360_000, 420_000, "manual", "voicecut", id="m-split"),
    ]
    cases.append(("pause inserted inside a clip", True, items))
    items = proposal_items()
    items[0] = {**items[0], "length": 40_000}
    items[clip_at(items, 0) + 1] = {**items[clip_at(items, 0) + 1], "length": 9_000}
    cases.append(("resized pauses", True, items))
    items = proposal_items()
    items[0] = {**items[0], "length": 1_000}
    cases.append(("lead-in shorter than its fade", True, items))
    items = proposal_items()
    del items[clip_at(items, 0) + 1]
    cases.append(("deleted pause", True, items))
    items = proposal_items()
    items[clip_at(items, 0)] = _clip(0, 72_000, "source", "voicecut", id="vc-0000")
    cases.append(("extended to the start of the recording", True, items))
    items = proposal_items()
    items[clip_at(items, 2)] = _clip(
        200_000, 300_000, "voicecut", "manual", id="vc-0002"
    )
    cases.append(("extended up to the next clip across a pause", True, items))
    items = proposal_items()
    items[clip_at(items, 1)] = _clip(
        139_000, 140_000, "manual", "voicecut", id="vc-0001"
    )
    cases.append(("very short clip", True, items))
    items = proposal_items()
    items[clip_at(items, 2)] = _clip(
        259_000, 260_000, "manual", "voicecut", id="vc-0002"
    )
    cases.append(("very short clip with a long fade", True, items))
    # Moved: the last clip first, its pause and the tail joined where it left.
    cases.append(
        (
            "moved clip",
            True,
            [
                _pause(pauses["lead"], "lead", id="vp-lead"),
                _clip(*vc[3], id="vc-0003"),
                _clip(*vc[0], id="vc-0000"),
                _pause(pauses[0], 0, id="vp-0000"),
                _clip(*vc[1], id="vc-0001"),
                _clip(*vc[2], id="vc-0002"),
                _pause(pauses["tail"], "tail", id="vp-tail"),
            ],
        )
    )
    items = proposal_items()
    at = clip_at(items, 2)
    items.insert(at + 1, _clip(*vc[1], id="m-copy"))
    cases.append(("copied clip", True, items))
    cases.append(
        (
            "whole recording before Voice Cut",
            False,
            [_clip(0, FIXTURE_FRAMES, "source", "source", id="src-0000")],
        )
    )
    cases.append(
        (
            "edited before Voice Cut",
            False,
            [
                _pause(12_000, id="m-lead"),
                _clip(0, 100_000, "source", "manual", id="src-0000"),
                _clip(150_000, 300_000, "manual", "manual", id="m-1"),
                _pause(24_000, id="m-gap"),
                _clip(300_000, FIXTURE_FRAMES, "manual", "source", id="m-2"),
            ],
        )
    )
    return cases


def layout_fixture(tmp_path: Path) -> dict[str, Any]:
    plan_path = tmp_path / "final_boundary_plan.json"
    write_json(plan_path, fixture_plan())
    proposal = load_proposal(plan_path)
    assert [item for item in proposal.timeline_items()] == _cases()[0][2]
    client = proposal.to_client()
    client["plan_sha256"] = "fixture"
    cases = []
    for name, with_proposal, items in _cases():
        used = proposal if with_proposal else None
        timeline = validate_timeline(
            {"proposal_sha256": used.plan_sha256 if used else None, "items": items},
            source_frames=FIXTURE_FRAMES,
            proposal=used,
            sample_rate=FIXTURE_RATE,
        )
        derivation = derive_pieces(
            timeline["items"],
            used,
            sample_rate=FIXTURE_RATE,
            declick=declick_samples(FIXTURE_RATE),
        )
        cases.append(
            {
                "name": name,
                "with_proposal": with_proposal,
                "items": items,
                "placed": [
                    {
                        key: record[key]
                        for key in (
                            "kind",
                            "output_start_sample",
                            "output_end_sample",
                            "source_start_sample",
                            "source_end_sample",
                            "fade_in_samples",
                            "fade_out_samples",
                            "source_interval_index",
                        )
                    }
                    for record in derivation.placed
                ],
                "trace": cut_render._contiguous_trace(derivation.placed),
                "total": derivation.output_frames,
            }
        )
    return {
        "sample_rate": FIXTURE_RATE,
        "source_frames": FIXTURE_FRAMES,
        "proposal": client,
        "cases": cases,
    }


def test_browser_layout_fixture_is_current(tmp_path: Path) -> None:
    """The browser's layout port is tested against this fixture (vitest)."""

    expected = layout_fixture(tmp_path)
    if os.environ.get("VOICECUT_WRITE_FIXTURES"):
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(expected, indent=1) + "\n", encoding="utf-8")
    assert FIXTURE.is_file(), "run with VOICECUT_WRITE_FIXTURES=1 to create the fixture"
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == expected
