#!/bin/sh
# Everything the film needs that is not in git: the recordings and their data,
# the narrator's lines (recorded only when data/script.json changed) and the
# sound effects. Uses the repository's Python and FFmpeg when they are set up.
set -e
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-../.venv/bin/python}"
if [ -z "${FFMPEG:-}" ]; then
  if [ -x ../.mfa-env/bin/ffmpeg ]; then FFMPEG=../.mfa-env/bin/ffmpeg; else FFMPEG=ffmpeg; fi
fi
export FFMPEG
"$PYTHON" scripts/prepare_data.py
"$PYTHON" scripts/voiceover.py
"$PYTHON" scripts/sfx.py
