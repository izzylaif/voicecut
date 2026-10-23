"""Transcribe each recorded line with Whisper and compare it with the script.

    PATH=<ffmpeg dir>:$PATH <mlx-whisper python> scripts/verify_vo.py
"""

import difflib
import json
import re
from pathlib import Path

import mlx_whisper

ROOT = Path(__file__).resolve().parents[1]


def words(text: str) -> list[str]:
    text = text.lower().replace("voicecut", "voice cut").replace("-", " ")
    return re.findall(r"[a-z0-9']+", text)


def main() -> int:
    script = json.loads((ROOT / "data" / "script.json").read_text(encoding="utf-8"))
    vo = json.loads((ROOT / "src" / "data" / "vo.json").read_text(encoding="utf-8"))
    bad = 0
    for line_id, text in script["lines"].items():
        heard = mlx_whisper.transcribe(
            str(ROOT / "public" / "audio" / "vo" / f"{line_id}.wav"),
            path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
            language="en",
        )["text"].strip()
        ratio = difflib.SequenceMatcher(None, words(text), words(heard)).ratio()
        flag = "ok " if ratio > 0.85 else "BAD"
        bad += flag == "BAD"
        print(f"{flag} {line_id} {vo[line_id]['seconds']:5.2f}s {ratio:.2f}  {heard}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
