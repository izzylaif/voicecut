from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from voicecut.breath_detection import (
    RESPIRO_FRAME_HOP_SAMPLES,
    RESPIRO_SAMPLE_RATE,
    BreathDetectionError,
    breath_probabilities,
    load_respiro_runtime,
    verify_respiro_runtime,
)


class _FakeRuntime:
    """Echo a constant probability per frame so chunk stitching is visible."""

    def __init__(self) -> None:
        self.lengths: list[int] = []

    def infer(self, waveform: np.ndarray) -> np.ndarray:
        self.lengths.append(len(waveform))
        frames = len(waveform) // RESPIRO_FRAME_HOP_SAMPLES + 1
        return np.full(frames, 0.25 * len(self.lengths), np.float32)


def test_whole_recording_probabilities_stitch_overlapping_chunks() -> None:
    runtime = _FakeRuntime()
    seconds = 70
    mono = np.zeros(seconds * 48_000, np.float32)
    probabilities = breath_probabilities(mono, 48_000, runtime=runtime)
    assert len(probabilities) == seconds * 100 + 1
    # 30 s chunks overlapping by 2 s: starts at 0, 28 and 56 s.
    assert runtime.lengths == [30 * RESPIRO_SAMPLE_RATE] * 2 + [
        14 * RESPIRO_SAMPLE_RATE
    ]
    assert probabilities[0] == 0.25
    assert probabilities[28 * 100] == 0.25  # first chunk still owns 28-29 s
    assert probabilities[29 * 100] == 0.5  # second chunk owns its centre
    assert probabilities[-1] == 0.75


def test_runtime_verification_fails_closed_on_hash_mismatch(tmp_path: Path) -> None:
    (tmp_path / "modules.py").write_bytes(b"not the pinned module")
    (tmp_path / "respiro-en.pt").write_bytes(b"not the pinned checkpoint")
    (tmp_path / "LICENSE").write_bytes(b"not the pinned license")

    with pytest.raises(BreathDetectionError, match="hash mismatch"):
        verify_respiro_runtime(tmp_path)


@pytest.mark.skipif(
    os.environ.get("VOICECUT_RUN_BREATH_INTEGRATION") != "1",
    reason="set VOICECUT_RUN_BREATH_INTEGRATION=1 to load pinned Respiro-en",
)
def test_pinned_respiro_runtime_opt_in_integration() -> None:
    runtime = load_respiro_runtime(device="cpu")
    probabilities = breath_probabilities(
        np.zeros(2 * RESPIRO_SAMPLE_RATE, np.float32),
        RESPIRO_SAMPLE_RATE,
        runtime=runtime,
    )
    assert runtime.device_name == "cpu"
    assert len(probabilities) == 201
    assert np.all((0.0 <= probabilities) & (probabilities <= 1.0))
