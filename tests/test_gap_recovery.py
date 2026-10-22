from __future__ import annotations

import numpy as np

from voicecut.gap_recovery import (
    HOP_SECONDS,
    frame_levels,
    insert_words,
    uncovered_speech,
)
from voicecut.take_planner import load_transcript_words

RATE = 16_000


def _audio_with_speech(spans: list[tuple[float, float]], seconds: float = 6.0):
    rng = np.random.default_rng(0)
    audio = (1e-4 * rng.standard_normal(int(seconds * RATE))).astype(np.float32)
    for start, end in spans:
        time = np.arange(int((end - start) * RATE)) / RATE
        audio[int(start * RATE) : int(start * RATE) + len(time)] += (
            0.1 * np.sin(2 * np.pi * 220 * time)
        ).astype(np.float32)
    return audio


def test_frame_levels_use_five_millisecond_frames() -> None:
    levels = frame_levels(np.ones(RATE, np.float32), RATE)
    assert len(levels) == round(1 / HOP_SECONDS)
    assert np.allclose(levels, 0.0, atol=1e-4)


def test_uncovered_speech_finds_only_speech_without_words() -> None:
    audio = _audio_with_speech([(1.0, 1.6), (2.5, 3.4), (4.5, 4.6)])
    words = [{"word": "covered", "start": 1.0, "end": 1.6}]
    regions = uncovered_speech(frame_levels(audio, RATE), words)
    # The 0.9 s burst has no word; the 0.1 s click is too short to matter.
    assert len(regions) == 1
    start, end = regions[0]
    assert 2.45 < start < 2.55 and 3.35 < end < 3.45


def test_recovered_words_are_inserted_in_recording_order() -> None:
    transcript = {
        "audio_sha256": "x",
        "atoms": [
            {
                "atom_index": 0,
                "start": 0.0,
                "end": 5.0,
                "words": [
                    {"word": "but", "start": 0.5, "end": 0.7},
                    {"word": "dependence.", "start": 1.0, "end": 1.5},
                    {"word": "and", "start": 4.0, "end": 4.2},
                ],
            },
            {
                "atom_index": 1,
                "start": 6.0,
                "end": 7.0,
                "words": [{"word": "later", "start": 6.0, "end": 6.4}],
            },
        ],
    }
    recovered = [
        {"word": "still", "start": 2.0, "end": 2.3, "recovered_from_gap": True},
        {"word": "pays", "start": 2.3, "end": 2.6, "recovered_from_gap": True},
        {"word": "tail", "start": 5.4, "end": 5.6, "recovered_from_gap": True},
    ]
    updated = insert_words(transcript, recovered)
    words = load_transcript_words(updated)
    assert [word.text for word in words] == [
        "but",
        "dependence.",
        "still",
        "pays",
        "and",
        "tail",
        "later",
    ]
    assert [atom["atom_index"] for atom in updated["atoms"]] == [0, 1, 2]
    assert transcript["atoms"][0]["words"][2]["word"] == "and"  # input untouched
