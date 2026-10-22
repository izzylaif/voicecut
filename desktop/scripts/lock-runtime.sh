#!/usr/bin/env bash
# Re-lock the runtime the desktop app installs on first launch.
#
#   desktop/scripts/lock-runtime.sh            keep every pinned version
#   desktop/scripts/lock-runtime.sh --upgrade  move to the newest versions
#
# Python: hashed locks from pyproject.toml's extras (the ones
# scripts/install.sh installs), so the app installs exactly what was tested.
#   macOS (Apple Silicon)  requirements-main.txt, requirements-mlx.txt
#   Linux x86_64           requirements-linux-x86_64.txt (PyTorch for the CPU)
#   Windows x64            requirements-windows-x86_64.txt
# Linux and Windows take the macOS versions wherever they exist; wheels must
# exist for every package (macOS 14 on Apple Silicon, glibc 2.28, Windows),
# because users have no compiler.
#
# MFA: explicit conda locks.  Linux and Windows are solved here; the macOS one
# is exported from a working environment:
#   micromamba env export -p "$PWD/.mfa-env" --explicit --md5 \
#     > desktop/runtime/mfa-osx-arm64.txt
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
UV="${UV:-uv}"
MICROMAMBA="${MICROMAMBA:-micromamba}"
RUNTIME=desktop/runtime
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export MACOSX_DEPLOYMENT_TARGET=14.0

if [[ "${1:-}" == "--upgrade" ]]; then
  UPGRADE=1
fi

compile() {
  local python="$1" platform="$2" output="$3"
  shift 3
  "${UV}" pip compile pyproject.toml "$@" \
    --python-version "${python}" \
    --python-platform "${platform}" \
    --override "${RUNTIME}/overrides.txt" \
    --generate-hashes \
    --custom-compile-command "desktop/scripts/lock-runtime.sh" \
    ${UPGRADE:+--upgrade} \
    -o "${RUNTIME}/${output}"
}

# macOS: the editor, pipeline, WhisperX and planners; MLX Whisper separately.
compile 3.12 aarch64-apple-darwin requirements-main.txt \
  --extra audio --extra cloud --extra editor
compile 3.11 aarch64-apple-darwin requirements-mlx.txt --extra mlx

# Linux and Windows: one environment, Whisper on faster-whisper (CTranslate2).
grep -o '^[A-Za-z0-9_.-]*==[^ ;]*' "${RUNTIME}/requirements-main.txt" >"${WORK}/pins.txt"
compile 3.12 x86_64-manylinux_2_28 requirements-linux-x86_64.txt \
  --extra audio --extra cloud --extra editor -c "${WORK}/pins.txt" \
  --index-url https://pypi.org/simple \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match --emit-index-url --no-annotate
compile 3.12 x86_64-pc-windows-msvc requirements-windows-x86_64.txt \
  --extra audio --extra cloud --extra editor -c "${WORK}/pins.txt" --no-annotate

# CUDA libraries for faster-whisper, installed only next to an NVIDIA GPU
# (CTranslate2 4.x needs CUDA 12 and cuDNN 9; its wheels do not bundle them).
printf 'nvidia-cublas-cu12==12.8.4.1\nnvidia-cudnn-cu12==9.10.2.21\n' >"${WORK}/cuda.in"
for target in x86_64-manylinux_2_28:linux-x86_64 x86_64-pc-windows-msvc:windows-x86_64; do
  "${UV}" pip compile "${WORK}/cuda.in" --python-version 3.12 \
    --python-platform "${target%%:*}" --generate-hashes --no-annotate --no-header \
    -o "${RUNTIME}/requirements-cuda-${target##*:}.txt"
done

# MFA 3.4.1 (with Kaldi and FFmpeg) for Linux and Windows.
solve_mfa() {
  local platform="$1"
  CONDA_OVERRIDE_GLIBC=2.28 CONDA_OVERRIDE_LINUX=5.15 \
    MAMBA_ROOT_PREFIX="${WORK}/mamba" \
    "${MICROMAMBA}" create --no-rc --dry-run --json -n solve --platform "${platform}" \
    -c conda-forge --override-channels "python=3.11" "montreal-forced-aligner=3.4.1" \
    >"${WORK}/${platform}.json"
  python3 - "${WORK}/${platform}.json" "${RUNTIME}/mfa-${platform}.txt" "${platform}" <<'EOF'
import json
import sys

links = json.load(open(sys.argv[1]))["actions"]["LINK"]
with open(sys.argv[2], "w") as lock:
    lock.write(f"# Explicit conda lock of VoiceCut's MFA environment for {sys.argv[3]}.\n")
    lock.write("# Regenerate with desktop/scripts/lock-runtime.sh.\n")
    lock.write(f"# platform: {sys.argv[3]}\n@EXPLICIT\n")
    for link in links:
        lock.write(f"{link['url']}#{link['md5']}\n")
EOF
}
solve_mfa linux-64
solve_mfa win-64
