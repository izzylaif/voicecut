#!/bin/sh
# Render the film and bring its sound to -14 LUFS: out/voicecut-promo.mp4.
set -e
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-../.venv/bin/python}"
if [ -z "${FFMPEG:-}" ]; then
  if [ -x ../.mfa-env/bin/ffmpeg ]; then FFMPEG=../.mfa-env/bin/ffmpeg; else FFMPEG=ffmpeg; fi
fi
export FFMPEG
npx remotion render VoiceCutPromo out/voicecut-promo-raw.mp4 "$@"
"$PYTHON" scripts/master.py out/voicecut-promo-raw.mp4 out/voicecut-promo.mp4
