#!/usr/bin/env bash
# Render the hand-drawn SVGs in desktop/icons into what the bundle needs:
#   src-tauri/icons/icon.icns              the app icon
#   src-tauri/icons/VoiceCutDocument.icns  the icon of .vc documents
#   src-tauri/icons/icon.ico               the Windows app icon
#   src-tauri/icons/VoiceCutDocument.ico   ...and of .vcproj documents there
#   src-tauri/icons/*.png                  window and Linux icons
#   build/dmg-background.png (+ @2x)       the disk image window
#   ui/mark.svg                            the mark on the app's own pages
#
# Needs resvg (cargo install resvg) and macOS iconutil.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
RESVG="${RESVG:-$(command -v resvg || echo "${HOME}/.cargo/bin/resvg")}"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

render() { "${RESVG}" -w "$2" -h "$2" "$1" "$3"; }

iconset() {
  local svg="$1" name="$2" set="${WORK}/$2.iconset"
  mkdir -p "${set}"
  for size in 16 32 128 256 512; do
    render "${svg}" "${size}" "${set}/icon_${size}x${size}.png"
    render "${svg}" "$((size * 2))" "${set}/icon_${size}x${size}@2x.png"
  done
  iconutil -c icns "${set}" -o "src-tauri/icons/${name}.icns"
}

# Windows: an .ico of PNG images (Vista and later read those).
ico() {
  local svg="$1" name="$2"
  for size in 16 24 32 48 64 128 256; do
    render "${svg}" "${size}" "${WORK}/${name}-${size}.png"
  done
  python3 - "${WORK}/${name}" "src-tauri/icons/${name}.ico" <<'PYTHON'
import struct
import sys
from pathlib import Path

sizes = (16, 24, 32, 48, 64, 128, 256)
images = [Path(f"{sys.argv[1]}-{size}.png").read_bytes() for size in sizes]
offset = 6 + 16 * len(images)
entries, data = b"", b""
for size, png in zip(sizes, images):
    entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                           len(png), offset + len(data))
    data += png
Path(sys.argv[2]).write_bytes(struct.pack("<HHH", 0, 1, len(images)) + entries + data)
PYTHON
}

mkdir -p src-tauri/icons build
iconset icons/voicecut.svg icon
iconset icons/document.svg VoiceCutDocument
render icons/voicecut.svg 32 src-tauri/icons/32x32.png
render icons/voicecut.svg 128 src-tauri/icons/128x128.png
render icons/voicecut.svg 256 src-tauri/icons/128x128@2x.png
render icons/voicecut.svg 1024 src-tauri/icons/icon.png
ico icons/voicecut.svg icon
ico icons/document.svg VoiceCutDocument
render icons/document.svg 256 src-tauri/icons/VoiceCutDocument.png
"${RESVG}" -w 660 -h 420 icons/dmg-background.svg build/dmg-background.png
"${RESVG}" -w 1320 -h 840 icons/dmg-background.svg build/dmg-background@2x.png
cp icons/voicecut.svg ui/mark.svg
echo "icons written to desktop/src-tauri/icons"
