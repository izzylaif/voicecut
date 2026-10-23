"""Turn VoiceCut's real run on the example recording into the film's data.

Reads the repository's public example (examples/media/audio/example_en.wav,
the raw take), Voice Cut's result of it (data/example_en_edited.flac) and its
sealed plan (data/example_en_plan.json, data/example_en_take_plan.json), and
writes:

* public/audio/raw.wav, public/audio/clean.wav — the two recordings, lifted
  to the narrator's loudness;
* public/img/voicecut-icon.png — the app icon;
* src/data/example.json — waveform peaks of both, every word with its time
  and whether Voice Cut kept it, the kept intervals, what each removed part
  was (silence, retake, false start, breath), and the transcript lines.

    python scripts/prepare_data.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parent
RAW = REPOSITORY / "examples" / "media" / "audio" / "example_en.wav"
EDITED = ROOT / "data" / "example_en_edited.flac"
ICON = REPOSITORY / "desktop" / "src-tauri" / "icons" / "icon.png"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
BINS_PER_SECOND = 100
ZOOM = (25.55, 27.75)  # a breath between "operations." and "In this video"
ZOOM_BINS_PER_SECOND = 900


def peaks(audio: np.ndarray, rate: int, per_second: int, scale: float) -> list[float]:
    """Loudest absolute sample of each bin, scaled to 0..1."""

    size = rate // per_second
    count = len(audio) // size
    blocks = np.abs(audio[: count * size]).reshape(count, size).max(axis=1)
    return [round(float(v), 4) for v in np.clip(blocks / scale, 0, 1)]


def main() -> int:
    raw, rate = sf.read(RAW, dtype="float32")
    clean, clean_rate = sf.read(EDITED, dtype="float32")
    assert rate == clean_rate == 48_000
    plan = json.loads((ROOT / "data" / "example_en_plan.json").read_text())
    take = json.loads((ROOT / "data" / "example_en_take_plan.json").read_text())
    out_audio = ROOT / "public" / "audio"
    out_audio.mkdir(parents=True, exist_ok=True)
    # The recording is quieter than the narrator: lift both versions alike,
    # with a limiter to keep the peaks.
    for source, target in ((RAW, "raw.wav"), (EDITED, "clean.wav")):
        subprocess.run(
            [FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
             "-af", "volume=7dB,alimiter=limit=0.89:attack=4:release=60:level=disabled",
             "-c:a", "pcm_s24le", str(out_audio / target)],
            check=True,
        )  # fmt: skip

    image = ROOT / "public" / "img"
    image.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON, image / "voicecut-icon.png")

    scale = float(np.abs(raw).max())
    kept = [
        (i["source_start_sample"] / rate, i["source_end_sample"] / rate)
        for i in plan["source_intervals"]
    ]
    duration = len(raw) / rate
    words = [
        {
            "id": index,
            "text": w["text"],
            "start": round(w["start"], 3),
            "end": round(w["end"], 3),
            "kept": bool(w["kept"]),
        }
        for index, w in enumerate(plan["words"])
    ]

    # What Voice Cut took out, in the recording's time.
    removed = []
    cursor = 0.0
    for start, end in kept:
        if start > cursor:
            removed.append([cursor, start])
        cursor = end
    if cursor < duration:
        removed.append([cursor, duration])
    regions = []
    for start, end in removed:
        inside = [w for w in words if w["start"] >= start - 0.05 and w["end"] <= end + 0.05]
        if inside and len(inside) > 4:
            first = inside[0]["start"]
            if first - start > 0.3:
                regions.append({"start": start, "end": first, "kind": "silence"})
            regions.append({"start": first, "end": end, "kind": "retake"})
        elif inside:
            regions.append({"start": start, "end": end, "kind": "false-start"})
        elif end - start > 0.7:
            regions.append({"start": start, "end": end, "kind": "silence"})
        else:
            # Voice Cut cleaned each of these pauses (pauses_with_noise_replaced).
            regions.append({"start": start, "end": end, "kind": "breath"})
    for region in regions:
        region["start"] = round(region["start"], 3)
        region["end"] = round(region["end"], 3)

    lines = [
        {
            "text": item["text"],
            "first": item["first_id"],
            "last": item["last_id"],
        }
        for item in take["items"]
    ]

    zoom_start, zoom_end = ZOOM
    zoom = raw[int(zoom_start * rate) : int(zoom_end * rate)]
    data = {
        "rate": BINS_PER_SECOND,
        "duration": round(duration, 3),
        "cleanDuration": round(len(clean) / rate, 3),
        "raw": peaks(raw, rate, BINS_PER_SECOND, scale),
        "clean": peaks(clean, rate, BINS_PER_SECOND, scale),
        "kept": [[round(a, 3), round(b, 3)] for a, b in kept],
        # Where each kept interval starts in Voice Cut's result.
        "keptOut": [
            round(piece["output_start_sample"] / rate, 4)
            for piece in plan["pieces"]
            if piece["kind"] == "source"
        ],
        "regions": regions,
        "words": words,
        "lines": lines,
        "zoom": {
            "start": zoom_start,
            "end": zoom_end,
            "rate": ZOOM_BINS_PER_SECOND,
            "peaks": peaks(zoom, rate, ZOOM_BINS_PER_SECOND, scale),
        },
    }
    target = ROOT / "src" / "data" / "example.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    kinds = {}
    for region in regions:
        kinds[region["kind"]] = kinds.get(region["kind"], 0) + 1
    print(f"raw {duration:.2f}s, clean {len(clean) / rate:.2f}s, regions {kinds}")
    for region in regions:
        print(region)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
