from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from voicecut import transcribe_mlx
from voicecut.transcribe_mlx import SAMPLE_RATE, run_asr, serialize_result


class _FakeCrisperWhisper:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def transcribe(self, audio: np.ndarray, **kwargs: object) -> SimpleNamespace:
        self.calls.append({"samples": len(audio), **kwargs})
        words = [
            SimpleNamespace(word="In", start=0.10, end=0.20),
            SimpleNamespace(word="a", start=0.22, end=0.26),
            SimpleNamespace(word="tr", start=0.28, end=0.40),
            SimpleNamespace(word="in", start=0.58, end=0.70),
        ]
        return SimpleNamespace(text="In a tr in", language="en", words=words)


def test_crisperwhisper_result_keeps_every_restart_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeCrisperWhisper()
    monkeypatch.setattr(transcribe_mlx, "_crisperwhisper_model", lambda model: fake)
    clip = np.zeros(SAMPLE_RATE, dtype=np.float32)

    raw = run_asr(
        clip,
        model="nyralabs/CrisperWhisper2.0_large",
        language="en",
        word_timestamps=True,
        prompt=None,
        engine="crisperwhisper",
    )
    serialized = serialize_result(raw, offset=10.0, clip_start=10.0, clip_end=11.0)

    assert fake.calls == [
        {
            "samples": SAMPLE_RATE,
            "sr": SAMPLE_RATE,
            "language": "en",
            "mode": "verbatim",
            "word_timestamps": True,
        }
    ]
    assert [word["word"] for word in serialized["words"]] == ["In", "a", "tr", "in"]
    assert serialized["words"][2]["start"] == pytest.approx(10.28)
    assert serialized["words"][3]["end"] == pytest.approx(10.70)
    assert serialized["segments"][0]["end"] == pytest.approx(11.0)


def test_crisperwhisper_rejects_a_decoding_prompt() -> None:
    with pytest.raises(ValueError, match="prompt"):
        run_asr(
            np.zeros(SAMPLE_RATE, dtype=np.float32),
            model="nyralabs/CrisperWhisper2.0_large",
            language="en",
            word_timestamps=True,
            prompt="verbatim please",
            engine="crisperwhisper",
        )



def test_pytorch_stages_never_load_ctranslate2() -> None:
    """On Intel Macs PyTorch cannot compute in a process that loaded
    CTranslate2 (faster-whisper): CTranslate2's wheel brings its own MKL, and
    PyTorch's matrix products then fail or crash.  Whisper runs in processes
    of its own (ctranslate2 imports PyTorch there but never computes with it);
    the stages that compute with PyTorch must stay free of CTranslate2."""

    pytest.importorskip("whisperx")
    code = (
        "import sys, voicecut.analyze, voicecut.ctc_enrich, voicecut.align_ctc, "
        "voicecut.breath_detection, voicecut.full_pipeline, voicecut.editor.worker, "
        "whisperx.alignment; "
        "print(sorted({'ctranslate2', 'faster_whisper'} & set(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"
