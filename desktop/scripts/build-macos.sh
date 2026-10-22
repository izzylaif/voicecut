#!/usr/bin/env bash
# Build VoiceCut.app and its disk image for Macs, as a universal app: Apple
# Silicon and Intel, each installing its own engine (desktop/runtime).
#
#   desktop/scripts/build-macos.sh
#
# Writes desktop/build/VoiceCut-<version>-universal.dmg.  Without an Apple
# Developer ID the app is signed ad hoc: it runs on the Mac that built it.
# With one (see desktop/README.md, "Signing and notarization"):
#
#   APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#   APPLE_API_ISSUER=... APPLE_API_KEY=... APPLE_API_KEY_PATH=... \
#   desktop/scripts/build-macos.sh
#
# signs the app and the image with the hardened runtime and a secure
# timestamp, has Apple notarize both, and staples the tickets.  APPLE_ID,
# APPLE_PASSWORD (an app-specific password) and APPLE_TEAM_ID work instead of
# the App Store Connect API key.
#
# With TAURI_SIGNING_PRIVATE_KEY (and TAURI_SIGNING_PRIVATE_KEY_PASSWORD, if
# the key has one) it also writes what the app's updater installs:
# src-tauri/target/universal-apple-darwin/release/bundle/macos/VoiceCut.app.tar.gz
# and its signature (see desktop/README.md, "Updates").
#
# VOICECUT_TEST_BUILD=1 builds the end-to-end test variant instead (cargo
# feature `automation`, see src-tauri/src/automation.rs) and no disk image.
#
# Needs: Rust (cargo, and rustup for the aarch64-apple-darwin and
# x86_64-apple-darwin targets), Node.js, resvg (cargo install resvg), uv.
set -euo pipefail

# A CI run without these secrets gets them empty; empty means not set (an
# empty signing identity would replace the ad-hoc one).
for name in APPLE_SIGNING_IDENTITY APPLE_API_ISSUER APPLE_API_KEY APPLE_API_KEY_PATH \
  APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID APPLE_CERTIFICATE APPLE_CERTIFICATE_PASSWORD \
  TAURI_SIGNING_PRIVATE_KEY; do
  if [[ -z "${!name:-}" ]]; then
    unset "${name}"
  fi
done

DESKTOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "${DESKTOP}/.." && pwd)"
BUILD="${DESKTOP}/build"
UV="${UV:-$(command -v uv || echo "${HOME}/Library/Python/3.9/bin/uv")}"
VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' \
  "${DESKTOP}/src-tauri/tauri.conf.json")"
TARGET=universal-apple-darwin
DMG="${BUILD}/VoiceCut-${VERSION}-universal.dmg"
APP="${DESKTOP}/src-tauri/target/${TARGET}/release/bundle/macos/VoiceCut.app"
IDENTITY="${APPLE_SIGNING_IDENTITY:-}"
# The updater's package needs the signing key; without it there is none.
UPDATER=()
if [[ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" ]]; then
  UPDATER=(--config src-tauri/tauri.updater.conf.json)
fi

step() { printf '\n== %s\n' "$*"; }

notarize() {
  if [[ -n "${APPLE_API_KEY:-}" ]]; then
    xcrun notarytool submit "$1" --wait \
      --key "${APPLE_API_KEY_PATH}" --key-id "${APPLE_API_KEY}" --issuer "${APPLE_API_ISSUER}"
  else
    xcrun notarytool submit "$1" --wait \
      --apple-id "${APPLE_ID}" --password "${APPLE_PASSWORD}" --team-id "${APPLE_TEAM_ID}"
  fi
}

step "Building the editor frontend"
if [[ ! -d "${ROOT}/frontend/node_modules" ]]; then
  npm ci --prefix "${ROOT}/frontend" --no-audit --no-fund
fi
npm run build --prefix "${ROOT}/frontend"

step "Staging VoiceCut's runtime"
python3 "${DESKTOP}/scripts/stage_runtime.py" --platform macos-aarch64 --platform macos-x86_64

step "Rendering the icons"
"${DESKTOP}/scripts/make-icons.sh"

step "Building VoiceCut.app"
if [[ ! -d "${DESKTOP}/node_modules" ]]; then
  npm ci --prefix "${DESKTOP}" --no-audit --no-fund
fi
rustup target add aarch64-apple-darwin x86_64-apple-darwin
if [[ -n "${VOICECUT_TEST_BUILD:-}" ]]; then
  # Its own identifier: the Finder and Launch Services must not mistake it for
  # the VoiceCut you use and send it your documents.
  (cd "${DESKTOP}" && npx tauri build --target "${TARGET}" --bundles app \
    --features automation --config src-tauri/tauri.test.conf.json ${UPDATER[@]+"${UPDATER[@]}"})
  codesign --verify --deep --strict "${APP}"
  echo "test build: ${APP}"
  exit 0
fi
(cd "${DESKTOP}" && npx tauri build --target "${TARGET}" --bundles app \
  ${UPDATER[@]+"${UPDATER[@]}"})
codesign --verify --deep --strict --verbose=2 "${APP}"
lipo "${APP}/Contents/MacOS/voicecut-desktop" -verify_arch arm64 x86_64

step "Building the disk image"
rm -f "${DMG}"
"${UV}" tool run --from dmgbuild==1.6.7 dmgbuild \
  -s "${DESKTOP}/scripts/dmg_settings.py" \
  -D app="${APP}" \
  -D background="${BUILD}/dmg-background.png" \
  -D icon="${DESKTOP}/src-tauri/icons/icon.icns" \
  "VoiceCut" "${DMG}"

if [[ -n "${IDENTITY}" ]]; then
  step "Signing and notarizing the disk image"
  codesign --sign "${IDENTITY}" --timestamp "${DMG}"
  if [[ -n "${APPLE_API_KEY:-}${APPLE_ID:-}" ]]; then
    # `tauri build` notarized and stapled the app inside; now the image.
    notarize "${DMG}"
    xcrun stapler staple "${DMG}"
    spctl --assess --type open --context context:primary-signature --verbose=2 "${DMG}"
  fi
fi

step "Done"
du -sh "${APP}" "${DMG}"
