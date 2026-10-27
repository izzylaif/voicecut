from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from voicecut import cut_render
from voicecut.common import read_json, write_json
from voicecut.cut_render import (
    PAUSE_CLASSES,
    AmbienceBank,
    PlanWord,
    analyze_signal,
    assign_breaks,
    build_ambience_bank,
    build_pieces,
    plan_timeline,
    refine_tail,
    render_pieces,
    render_take_plan,
)
from voicecut.mfa_alignment import MFAAlignmentError

RATE = 16_000
FLOOR = 1e-4


def _tone(duration: float, *, amplitude: float = 0.1, decay: float = 0.0) -> np.ndarray:
    time = np.arange(int(duration * RATE)) / RATE
    signal = amplitude * np.sin(2 * np.pi * 300 * time)
    ramp = int(0.01 * RATE)
    signal[:ramp] *= np.linspace(0.0, 1.0, ramp)
    if decay:
        tail = np.arange(int(decay * RATE)) / RATE
        decayed = (
            amplitude
            * np.sin(2 * np.pi * 300 * (time[-1] + tail))
            * np.exp(-tail / (decay / 6))
        )
        signal = np.concatenate([signal, decayed])
    else:
        signal[-ramp:] *= np.linspace(1.0, 0.0, ramp)
    return signal.astype(np.float32)


def _scene() -> tuple[np.ndarray, list[PlanWord], np.ndarray]:
    """Five tone 'words'; a long pause, a dropped word and a breath."""

    rng = np.random.default_rng(7)
    audio = (FLOOR * rng.standard_normal(7 * RATE)).astype(np.float32)
    layout = [
        ("One", 0.50, 0.80),
        ("two.", 0.85, 1.15),
        ("Three,", 3.15, 3.45),
        ("dropped", 4.00, 4.30),
        ("five.", 5.20, 5.50),
    ]
    words = []
    for index, (text, start, end) in enumerate(layout):
        first = int(start * RATE)
        audio[first : first + int((end - start) * RATE)] += _tone(end - start)
        words.append(
            PlanWord(
                id=index,
                text=text,
                start=start,
                end=end,
                core_start=start,
                core_end=end,
            )
        )
    breath = np.zeros(7 * 100 + 1, np.float32)
    audio[int(4.70 * RATE) : int(5.00 * RATE)] += (
        0.01 * rng.standard_normal(int(0.30 * RATE))
    ).astype(np.float32)
    breath[470:500] = 0.95
    return audio, words, breath


def _render(audio, words, breath, *, policy="semantic"):
    signal = analyze_signal(
        audio,
        RATE,
        word_spans=[(word.core_start, word.core_end) for word in words],
        breath=breath,
    )
    islands, joins = plan_timeline(
        words=words,
        signal=signal,
        duration=len(audio) / RATE,
        pause_policy=policy,
        use_breath=True,
    )
    bank = build_ambience_bank(signal, words, len(audio))
    pieces = build_pieces(
        islands=islands,
        joins=joins,
        words=words,
        signal=signal,
        bank=bank,
        sample_rate=RATE,
        total_samples=len(audio),
        pause_policy=policy,
        use_breath=True,
    )
    output, segments = render_pieces(audio[:, None], pieces, RATE)
    return signal, islands, joins, output[:, 0], segments


def _keep(words, items):
    assign_breaks(words, items)


def test_assign_breaks_uses_planner_punctuation_and_break_after() -> None:
    _, words, _ = _scene()
    _keep(
        words,
        [
            {
                "first_id": 0,
                "last_id": 2,
                "text": "One, two. Three",
                "break_after": "comma",
            },
            {"first_id": 4, "last_id": 4, "text": "Five.", "break_after": "paragraph"},
        ],
    )
    assert [word.kept for word in words] == [True, True, True, False, True]
    assert [word.break_after for word in words] == [
        "comma",
        "sentence",
        "comma",
        "none",
        "paragraph",
    ]


def _speech_runs(output: np.ndarray, signal) -> list[tuple[float, float]]:
    """Output intervals whose level is clearly speech (not ambience)."""

    hop = int(0.005 * RATE)
    frames = len(output) // hop
    level = 20 * np.log10(
        np.sqrt((output[: frames * hop].reshape(frames, hop) ** 2).mean(axis=1)) + 1e-12
    )
    loud = level > signal.speech_db - 20
    runs, start = [], None
    for index, value in enumerate(np.append(loud, False)):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append((start * 0.005, index * 0.005))
            start = None
    return runs


def test_long_pause_is_shortened_breath_removed_and_dropped_word_cut() -> None:
    audio, words, breath = _scene()
    _keep(
        words,
        [
            {
                "first_id": 0,
                "last_id": 2,
                "text": "One two. Three,",
                "break_after": "comma",
            },
            {"first_id": 4, "last_id": 4, "text": "five.", "break_after": "sentence"},
        ],
    )
    signal, islands, _joins, output, _ = _render(audio, words, breath)
    # "One two." stay one island; the 2 s pause and the cut form two joins.
    assert [(island.first_word, island.last_word) for island in islands] == [
        (0, 1),
        (2, 2),
        (4, 4),
    ]
    runs = _speech_runs(output, signal)
    assert len(runs) == 4  # One, two, Three, five -- "dropped" is gone
    sentence_gap = runs[2][0] - runs[1][1]
    comma_gap = runs[3][0] - runs[2][1]
    assert sentence_gap == pytest.approx(PAUSE_CLASSES["sentence"][1], abs=0.06)
    assert comma_gap == pytest.approx(PAUSE_CLASSES["comma"][1], abs=0.06)
    # Nothing but ambience between "Three," and "five.": the breath is gone.
    hop = int(0.005 * RATE)
    pause = output[int((runs[2][1] + 0.05) * RATE) : int((runs[3][0] - 0.03) * RATE)]
    frames = len(pause) // hop
    level = 20 * np.log10(
        np.sqrt((pause[: frames * hop].reshape(frames, hop) ** 2).mean(axis=1)) + 1e-12
    )
    assert level.max() < signal.sound_threshold_db


def _breathy_scene(
    *, quiet_after: float, quiet_before: float
) -> tuple[np.ndarray, list[PlanWord], np.ndarray]:
    """Tight narration: six words 0.4 s apart and a quiet breath in every gap,
    so no stretch of room tone is as long as the regular bank needs."""

    rng = np.random.default_rng(11)
    duration = 4.8
    audio = (FLOOR * rng.standard_normal(int(duration * RATE))).astype(np.float32)
    breath = np.zeros(int(duration * 100) + 1, np.float32)

    def breathe(start: float, end: float) -> None:
        first, last = int(start * RATE), int(end * RATE)
        audio[first:last] += (2 * FLOOR * rng.standard_normal(last - first)).astype(
            np.float32
        )
        breath[int(start * 100) : int(end * 100)] = 0.95

    words, start, breathing = [], 0.3, 0.0
    for index, text in enumerate(["One,", "two.", "Three,", "four.", "Five,", "six."]):
        breathe(breathing, start - quiet_before)
        first = int(start * RATE)
        audio[first : first + int(0.3 * RATE)] += _tone(0.3)
        words.append(
            PlanWord(
                id=index,
                text=text,
                start=start,
                end=start + 0.3,
                core_start=start,
                core_end=start + 0.3,
            )
        )
        breathing = start + 0.3 + quiet_after
        start += 0.7
    breathe(breathing, duration)
    return audio, words, breath


def _pause_levels(output: np.ndarray, gaps: list[tuple[float, float]]) -> float:
    hop = int(0.005 * RATE)
    loudest = -200.0
    for start, end in gaps:
        pause = output[int((start + 0.03) * RATE) : int((end - 0.03) * RATE)]
        frames = len(pause) // hop
        level = 20 * np.log10(
            np.sqrt((pause[: frames * hop].reshape(frames, hop) ** 2).mean(axis=1))
            + 1e-12
        )
        loudest = max(loudest, float(level.max()))
    return loudest


def test_breaths_in_tight_narration_become_short_run_room_tone() -> None:
    audio, words, breath = _breathy_scene(quiet_after=0.12, quiet_before=0.06)
    _keep(
        words,
        [
            {
                "first_id": 0,
                "last_id": 5,
                "text": "One, two. Three, four. Five, six.",
                "break_after": "paragraph",
            }
        ],
    )
    signal, _, joins, output, _ = _render(audio, words, breath)
    assert build_ambience_bank(signal, words, len(audio)).status == "short_runs"
    runs = _speech_runs(output, signal)
    gaps = [(left[1], right[0]) for left, right in pairwise(runs)]
    assert len(gaps) == len(joins) == 5
    for (start, end), join in zip(gaps, joins):
        assert end - start == pytest.approx(join["desired_gap_seconds"], abs=0.04)
        assert end - start > 0.3
        assert join["room_tone_seconds"] > 0
    # The breaths (6 dB above the floor) are gone from every pause.
    assert _pause_levels(output, gaps) < 20 * np.log10(FLOOR) + 4


def _breathing_only(policy: str):
    audio, words, breath = _breathy_scene(quiet_after=0.0, quiet_before=0.0)
    _keep(
        words,
        [
            {
                "first_id": 0,
                "last_id": 5,
                "text": "One, two. Three, four. Five, six.",
                "break_after": "paragraph",
            }
        ],
    )
    signal, _, joins, output, _ = _render(audio, words, breath, policy=policy)
    assert build_ambience_bank(signal, words, len(audio)).status == "unavailable"
    runs = _speech_runs(output, signal)
    gaps = [(left[1], right[0]) for left, right in pairwise(runs)]
    assert len(gaps) == len(joins) == 5
    return joins, output, gaps


def test_pause_without_clean_material_is_filled_with_silence() -> None:
    joins, output, gaps = _breathing_only("semantic")
    for (start, end), join in zip(gaps, joins):
        assert end - start == pytest.approx(join["desired_gap_seconds"], abs=0.04)
        assert end - start > 0.3
        assert join["silent_room_tone"]
    assert _pause_levels(output, gaps) < -120


def test_video_pause_without_clean_material_keeps_its_recorded_length() -> None:
    # A video cannot insert silence under its pictures: the pause keeps the
    # recorded breath rather than shrink.
    joins, _, gaps = _breathing_only("cuts")
    assert [end - start for start, end in gaps] == pytest.approx([0.4] * 5, abs=0.04)
    assert all(join["kept_recorded_gap"] for join in joins)


def test_retained_speech_is_copied_sample_exact() -> None:
    audio, words, breath = _scene()
    _keep(
        words,
        [
            {
                "first_id": 0,
                "last_id": 2,
                "text": "One two. Three.",
                "break_after": "sentence",
            }
        ],
    )
    _, _, _, output, segments = _render(audio, words, breath)
    source = [segment for segment in segments if segment["kind"] == "source"]
    for segment in source:
        inner_in = segment["fade_in_samples"]
        inner_out = segment["fade_out_samples"]
        out = output[
            segment["output_start_sample"] + inner_in : segment["output_end_sample"]
            - inner_out
        ]
        src = audio[
            segment["source_start_sample"] + inner_in : segment["source_end_sample"]
            - inner_out
        ]
        np.testing.assert_array_equal(out, src)


def test_tail_keeps_a_natural_decay_beyond_the_aligned_word_end() -> None:
    rng = np.random.default_rng(3)
    audio = (FLOOR * rng.standard_normal(2 * RATE)).astype(np.float32)
    word = _tone(0.3, decay=0.12)
    audio[int(0.5 * RATE) : int(0.5 * RATE) + len(word)] += word
    signal = analyze_signal(audio, RATE, word_spans=[(0.5, 0.8)], breath=None)
    tail, status = refine_tail(signal, core_end=0.8, limit=2.0, use_breath=False)
    assert status == "quiet"
    assert 0.85 < tail < 0.95


def test_contiguous_output_trace_maps_source_one_to_one() -> None:
    audio, words, breath = _scene()
    _keep(
        words,
        [
            {
                "first_id": 0,
                "last_id": 2,
                "text": "One two. Three,",
                "break_after": "comma",
            },
            {"first_id": 4, "last_id": 4, "text": "five.", "break_after": "sentence"},
        ],
    )
    _, _, _, output, segments = _render(audio, words, breath)
    trace = cut_render._contiguous_trace(segments)
    cursor = 0
    for segment in trace:
        assert segment["output_start_sample"] == cursor
        cursor = segment["output_end_sample"]
        if segment["kind"] == "source":
            assert (
                segment["source_end_sample"] - segment["source_start_sample"]
                == segment["output_end_sample"] - segment["output_start_sample"]
            )
    assert cursor == len(output)


def test_cut_policy_uses_only_source_samples() -> None:
    audio, words, breath = _scene()
    _keep(
        words,
        [
            {
                "first_id": 0,
                "last_id": 2,
                "text": "One two. Three,",
                "break_after": "comma",
            },
            {"first_id": 4, "last_id": 4, "text": "five.", "break_after": "sentence"},
        ],
    )
    _, _, _, _, segments = _render(audio, words, breath, policy="cuts")
    assert {segment["kind"] for segment in segments} == {"source"}


def test_ambience_bank_avoids_words_and_breaths() -> None:
    audio, words, breath = _scene()
    signal = analyze_signal(
        audio, RATE, word_spans=[(w.start, w.end) for w in words], breath=breath
    )
    bank: AmbienceBank = build_ambience_bank(signal, words, len(audio))
    assert bank.runs
    for start, end in bank.runs:
        for word in words:
            assert end / RATE <= word.start or start / RATE >= word.end
        assert end / RATE <= 4.7 or start / RATE >= 5.0


def test_render_take_plan_end_to_end_with_whisper_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio, words, _ = _scene()
    source = tmp_path / "source.wav"
    sf.write(source, audio, RATE, subtype="FLOAT")
    plan = tmp_path / "take_plan.json"
    write_json(
        plan,
        {
            "status": "complete",
            "line_gap_seconds": 0.25,
            "words": [
                {"id": w.id, "text": w.text, "start": w.start, "end": w.end}
                for w in words
            ],
            "items": [
                {
                    "first_id": 0,
                    "last_id": 2,
                    "text": "One two. Three,",
                    "break_after": "comma",
                },
                {
                    "first_id": 4,
                    "last_id": 4,
                    "text": "five.",
                    "break_after": "sentence",
                },
            ],
        },
    )

    def no_mfa(**_: object) -> dict[str, object]:
        raise MFAAlignmentError("mfa_runtime_failed", "not installed in tests")

    monkeypatch.setattr(cut_render, "mfa_core_timings", no_mfa)
    manifest = render_take_plan(
        audio_path=source,
        plan_path=plan,
        output_dir=tmp_path / "final",
        breath_cleanup="off",
    )
    assert manifest["status"] == "complete"
    assert manifest["mfa_aligned_words"] == 0
    assert manifest["cuts"] == 1
    rendered, rate = sf.read(manifest["final_cut_wav"])
    assert rate == RATE
    assert len(rendered) == manifest["frame_count"]
    boundary = read_json(Path(manifest["final_boundary_plan"]))
    assert boundary["planner"] == "authoritative_single_pass_boundary_plan_v2"
    assert boundary["status"] == "safe"
    assert boundary["expected_output_frame_count"] == manifest["frame_count"]
    assert [
        (interval["start_word_id"], interval["end_word_id"])
        for interval in boundary["source_intervals"]
    ] == [(0, 2), (2, 3), (4, 5)]


def test_loud_speech_after_an_early_aligned_end_is_never_cut() -> None:
    rng = np.random.default_rng(5)
    audio = (FLOOR * rng.standard_normal(2 * RATE)).astype(np.float32)
    audio[int(0.5 * RATE) : int(1.2 * RATE)] += _tone(0.7)
    signal = analyze_signal(audio, RATE, word_spans=[(0.5, 0.7)], breath=None)
    # The aligner claims the word ends at 0.7 s; the sound lasts until 1.2 s.
    tail, status = refine_tail(signal, core_end=0.7, limit=2.0, use_breath=False)
    assert status == "quiet"
    assert tail >= 1.2


def test_faint_noise_after_a_word_is_not_mistaken_for_connected_speech() -> None:
    rng = np.random.default_rng(11)
    audio = (FLOOR * rng.standard_normal(4 * RATE)).astype(np.float32)
    audio[int(0.5 * RATE) : int(0.8 * RATE)] += _tone(0.3)
    # Half a second of noise hovering around the sound threshold, then a breath.
    audio[int(0.8 * RATE) : int(1.3 * RATE)] += (
        4 * FLOOR * rng.standard_normal(int(0.5 * RATE))
    ).astype(np.float32)
    audio[int(1.6 * RATE) : int(1.9 * RATE)] += (
        0.01 * rng.standard_normal(int(0.3 * RATE))
    ).astype(np.float32)
    audio[int(2.5 * RATE) : int(2.8 * RATE)] += _tone(0.3)
    breath = np.zeros(4 * 100 + 1, np.float32)
    breath[160:190] = 1.0
    words = [
        PlanWord(0, "word.", 0.5, 0.8, core_start=0.5, core_end=0.8),
        PlanWord(1, "Next", 2.5, 2.8, core_start=2.5, core_end=2.8),
    ]
    _keep(
        words,
        [
            {
                "first_id": 0,
                "last_id": 1,
                "text": "word. Next",
                "break_after": "sentence",
            }
        ],
    )
    signal, islands, joins, output, _ = _render(audio, words, breath)
    assert len(islands) == 2
    assert joins[0]["kind"] == "pause"
    runs = _speech_runs(output, signal)
    assert runs[1][0] - runs[0][1] < PAUSE_CLASSES["sentence"][2] + 0.25


def test_pause_is_found_from_the_waveform_when_alignment_hides_it() -> None:
    rng = np.random.default_rng(13)
    audio = (FLOOR * rng.standard_normal(5 * RATE)).astype(np.float32)
    audio[int(0.5 * RATE) : int(0.8 * RATE)] += _tone(0.3)
    audio[int(3.0 * RATE) : int(3.3 * RATE)] += _tone(0.3)
    # The aligner packed the second word into the silent hesitation.
    words = [
        PlanWord(0, "the", 0.5, 0.8, core_start=0.5, core_end=0.8),
        PlanWord(1, "delay", 3.0, 3.3, core_start=0.85, core_end=1.2),
        PlanWord(2, "grows.", 3.3, 3.5, core_start=3.0, core_end=3.3),
    ]
    _keep(
        words,
        [
            {
                "first_id": 0,
                "last_id": 2,
                "text": "the delay grows.",
                "break_after": "sentence",
            }
        ],
    )
    signal, _, joins, output, _ = _render(audio, words, None)
    assert [join["kind"] for join in joins] == ["pause"]
    runs = _speech_runs(output, signal)
    assert runs[1][0] - runs[0][1] == pytest.approx(PAUSE_CLASSES["none"][1], abs=0.06)
