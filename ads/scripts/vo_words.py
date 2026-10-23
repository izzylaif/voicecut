"""Word timings of every narrator line (Whisper), for syncing the visuals.

    PATH=<ffmpeg dir>:$PATH <mlx-whisper python> scripts/vo_words.py
"""

import json
from pathlib import Path

import mlx_whisper

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    vo = json.loads((ROOT / "src" / "data" / "vo.json").read_text(encoding="utf-8"))
    out = {}
    for line_id in vo:
        result = mlx_whisper.transcribe(
            str(ROOT / "public" / "audio" / "vo" / f"{line_id}.wav"),
            path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
            language="en",
            word_timestamps=True,
        )
        out[line_id] = [
            {"w": w["word"].strip(), "s": round(w["start"], 3), "e": round(w["end"], 3)}
            for segment in result["segments"]
            for w in segment["words"]
        ]
        print(line_id, " ".join(f"{w['w']}@{w['s']}" for w in out[line_id]))
    (ROOT / "src" / "data" / "vo-words.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
