#!/usr/bin/env bash
# Re-lock the Python environments the desktop app installs on first launch.
#
#   desktop/scripts/lock-runtime.sh            keep every pinned version
#   desktop/scripts/lock-runtime.sh --upgrade  move to the newest versions
#
# The locks are hashed, so the app installs exactly what was tested.  They
# start from pyproject.toml's extras (the same ones scripts/install.sh
# installs) and keep the versions already pinned unless --upgrade is given.
# Wheels must exist for macOS 14 on Apple Silicon: users have no compiler.
#
# The MFA environment is locked from a working one:
#   micromamba env export -p "$PWD/.mfa-env" --explicit --md5 \
#     > desktop/runtime/mfa-osx-arm64.txt
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
UV="${UV:-uv}"
export MACOSX_DEPLOYMENT_TARGET=14.0

compile() {
  local python="$1" output="$2"
  shift 2
  "${UV}" pip compile pyproject.toml "$@" \
    --python-version "${python}" \
    --python-platform aarch64-apple-darwin \
    --generate-hashes \
    --custom-compile-command "desktop/scripts/lock-runtime.sh" \
    ${UPGRADE:+--upgrade} \
    -o "${output}"
}

if [[ "${1:-}" == "--upgrade" ]]; then
  UPGRADE=1
fi

# Main environment: the editor, the pipeline, WhisperX and the cloud planners.
compile 3.12 desktop/runtime/requirements-main.txt \
  --extra audio --extra cloud --extra editor
# MLX environment: MLX Whisper and the local planners (Apple Silicon only).
compile 3.11 desktop/runtime/requirements-mlx.txt --extra mlx
