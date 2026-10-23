"""Bring the rendered film's sound to -14 LUFS (streaming loudness) with a
-1.5 dBTP ceiling, two-pass, and copy the picture as it is.

    python scripts/master.py out/voicecut-promo-raw.mp4 out/voicecut-promo.mp4
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
TARGET = "I=-14:TP=-1.5:LRA=11"


def main() -> int:
    source, target = sys.argv[1], sys.argv[2]
    probe = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-i", source, "-af", f"loudnorm={TARGET}:print_format=json", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    measured = json.loads(probe[probe.rindex("{") : probe.rindex("}") + 1])
    print("before:", {key: measured[key] for key in ("input_i", "input_tp", "input_lra")})
    loudnorm = (
        f"loudnorm={TARGET}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true"
    )
    subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", source,
         "-c:v", "copy", "-af", f"{loudnorm},aresample=48000", "-c:a", "aac", "-b:a", "256k",
         "-movflags", "+faststart", target],
        check=True,
    )  # fmt: skip
    check = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-i", target, "-af", f"loudnorm={TARGET}:print_format=json", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    after = json.loads(check[check.rindex("{") : check.rindex("}") + 1])
    print("after:", {key: after[key] for key in ("input_i", "input_tp", "input_lra")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
