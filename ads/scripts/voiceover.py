"""Record the narrator's lines of data/script.json with Gemini TTS.

The takes are kept in data/vo/<id>.flac with what each one says in
data/vo/takes.json: a line is recorded again only when its text, the voice
or the direction changes (that needs GEMINI_API_KEY), so the film's voice
stays the same from one build to the next. Each take is then trimmed to a
short pad of silence and sped up by the script's tempo into
public/audio/vo/<id>.wav, and the durations go to src/data/vo.json.

    python scripts/voiceover.py            # record what changed
    python scripts/voiceover.py vo04       # record vo04 again anyway
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
TAKES = ROOT / "data" / "vo"
OUT = ROOT / "public" / "audio" / "vo"
RATE = 24_000
PAD_SECONDS = 0.06
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")


def trim(audio: np.ndarray) -> np.ndarray:
    """Cut leading and trailing silence, keeping a short pad."""

    loud = np.nonzero(np.abs(audio) > 10 ** (-45 / 20))[0]
    if not len(loud):
        return audio
    pad = int(PAD_SECONDS * RATE)
    return audio[max(0, loud[0] - pad) : min(len(audio), loud[-1] + pad)]


def faster(audio: np.ndarray, tempo: float) -> np.ndarray:
    """``audio`` played ``tempo`` times faster at the same pitch (FFmpeg atempo)."""

    if tempo == 1:
        return audio
    with tempfile.TemporaryDirectory() as folder:
        source, target = Path(folder) / "in.wav", Path(folder) / "out.wav"
        sf.write(source, audio, RATE, subtype="FLOAT")
        subprocess.run(
            [FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
             "-filter:a", f"atempo={tempo}", "-c:a", "pcm_f32le", str(target)],
            check=True,
        )  # fmt: skip
        result, _ = sf.read(target, dtype="float32")
    return result


def record(script: dict, text: str) -> np.ndarray:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=script["model"],
        contents=f"{script['style']}: {text}",
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=script["voice"]
                    )
                )
            ),
        ),
    )
    part = response.candidates[0].content.parts[0]
    pcm = np.frombuffer(part.inline_data.data, dtype="<i2").astype(np.float32)
    return pcm / 32768.0


def main() -> int:
    script = json.loads((ROOT / "data" / "script.json").read_text(encoding="utf-8"))
    TAKES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    index_path = TAKES / "takes.json"
    takes = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    again = set(sys.argv[1:])
    durations = {}
    for line_id, text in script["lines"].items():
        key = hashlib.sha256(
            json.dumps([script["model"], script["voice"], script["style"], text]).encode()
        ).hexdigest()[:16]
        take = TAKES / f"{line_id}.flac"
        if takes.get(line_id) != key or not take.exists() or line_id in again:
            for attempt in range(4):
                try:
                    audio = record(script, text)
                    break
                except Exception as error:  # noqa: BLE001 - retry a flaky call
                    print(f"{line_id}: {error}; retrying", file=sys.stderr)
                    time.sleep(5 * (attempt + 1))
            else:
                raise SystemExit(f"{line_id}: no audio")
            sf.write(take, audio, RATE, subtype="PCM_16")
            takes[line_id] = key
            index_path.write_text(json.dumps(takes, indent=2) + "\n", encoding="utf-8")
        audio, _ = sf.read(take, dtype="float32")
        audio = faster(trim(audio), float(script.get("tempo", 1)))
        sf.write(OUT / f"{line_id}.wav", audio, RATE, subtype="PCM_16")
        durations[line_id] = {"text": text, "seconds": round(len(audio) / RATE, 3)}
        print(f"{line_id} {durations[line_id]['seconds']:5.2f}s  {text}")
    (ROOT / "src" / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "src" / "data" / "vo.json").write_text(json.dumps(durations, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
