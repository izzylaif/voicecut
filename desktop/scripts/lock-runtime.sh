#!/usr/bin/env bash
# Re-lock the runtime the desktop app installs on first launch.
#
#   desktop/scripts/lock-runtime.sh            keep every pinned version
#   desktop/scripts/lock-runtime.sh --upgrade  move to the newest versions
#
# Python: hashed locks from pyproject.toml's extras (the ones
# scripts/install.sh installs), so the app installs exactly what was tested.
#   macOS (Apple Silicon)  requirements-main.txt, requirements-mlx.txt
#   macOS (Intel)          requirements-macos-x86_64.txt over main-osx-64.txt
#   Linux x86_64           requirements-linux-x86_64.txt (PyTorch for the CPU)
#   Windows x64            requirements-windows-x86_64.txt
# The others take the Apple Silicon versions wherever they exist; wheels must
# exist for every package (macOS 14, glibc 2.28, Windows), because users have
# no compiler.
#
# Intel Macs: PyPI has no wheels for them of PyTorch and torchaudio after
# 2.2.2 or of numba and llvmlite after 0.62 and 0.45, so these come from
# conda-forge in the same versions: main-osx-64.txt is an explicit conda lock
# of Python, PyTorch (its OpenBLAS build, a third smaller than the MKL one),
# torchaudio, numba and llvmlite.  The app creates the main environment from
# it and installs the rest from requirements-macos-x86_64.txt.  onnxruntime
# and cryptography take their newest versions with Intel wheels; numpy comes
# from PyPI, as everywhere (see desktop/README.md, "Intel Macs").
#
# MFA: explicit conda locks.  Linux, Windows and Intel Macs are solved here;
# the Apple Silicon one is exported from a working environment:
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

# Intel Macs: everything but what conda-forge brings (see above).
INTEL_CONDA=(torch torchaudio numba llvmlite)
grep -vE '^(onnxruntime|cryptography)==' "${WORK}/pins.txt" >"${WORK}/pins-intel.txt"
for package in "${INTEL_CONDA[@]}" torchvision torchcodec; do
  echo "${package}; sys_platform == \"never\""
done >"${WORK}/intel-overrides.txt"
# intervaltree and antlr4-python3-runtime are pure Python and only sdists.
compile 3.12 x86_64-apple-darwin requirements-macos-x86_64.txt \
  --extra audio --extra cloud --extra editor -c "${WORK}/pins-intel.txt" \
  --override "${WORK}/intel-overrides.txt" --only-binary :all: \
  --no-binary intervaltree --no-binary antlr4-python3-runtime --no-annotate

# Its conda base: the pinned versions of INTEL_CONDA and of every package the
# Python locks share with it (but torchcodec: conda-forge's torchaudio needs a
# newer one than WhisperX's pin, and VoiceCut does not use it), solved for the
# locked numpy, which the lock then leaves to PyPI.
CONDA_OVERRIDE_OSX=14.0 MAMBA_ROOT_PREFIX="${WORK}/mamba" python3 - "${MICROMAMBA}" \
  "${RUNTIME}" "${INTEL_CONDA[@]}" <<'EOF'
import json
import re
import subprocess
import sys
from pathlib import Path

micromamba, runtime, *packages = sys.argv[1:]
runtime = Path(runtime)


def pins(name: str) -> dict[str, str]:
    text = (runtime / name).read_text()
    return {
        match[1].lower().replace("_", "-"): match[2]
        for match in re.finditer(r"^([A-Za-z0-9_.-]+)==([^ ;\\\s]+)", text, re.M)
    }


main, intel = pins("requirements-main.txt"), pins("requirements-macos-x86_64.txt")
pinned = {**main, **intel}
conda_name = {"torch": "pytorch"}
python = json.loads((runtime / "runtime.json").read_text())["python"]["main"]
specs = {"python": python}
for package in packages:
    specs[conda_name.get(package, package)] = main[package]
specs["pytorch"] += "=cpu_generic*"
for _ in range(5):
    solve = subprocess.run(
        [micromamba, "create", "--no-rc", "--dry-run", "--json", "-n", "solve",
         "--platform", "osx-64", "-c", "conda-forge", "--override-channels",
         *(f"{name}={version}" for name, version in specs.items())],
        capture_output=True, text=True,
    )
    links = json.loads(solve.stdout).get("actions", {}).get("LINK")
    if links is None:
        sys.exit(f"cannot solve the Intel Mac conda base {specs}:\n{solve.stdout[-3000:]}")
    differing = {
        link["name"]: pinned[name]
        for link in links
        if (name := link["name"].lower().replace("_", "-")) in pinned
        and name != "torchcodec"
        and link["version"] != pinned[name]
    }
    if not differing:
        break
    specs.update(differing)
else:
    sys.exit(f"conda-forge has no versions matching the locks: {differing}")
with open(runtime / "main-osx-64.txt", "w") as lock:
    lock.write("# Explicit conda lock of the base of VoiceCut's main environment on Intel Macs.\n")
    lock.write("# Regenerate with desktop/scripts/lock-runtime.sh.\n")
    lock.write("# platform: osx-64\n@EXPLICIT\n")
    for link in links:
        if link["name"] != "numpy":
            lock.write(f"{link['url']}#{link['md5']}\n")
EOF

# CUDA libraries for faster-whisper, installed only next to an NVIDIA GPU
# (CTranslate2 4.x needs CUDA 12 and cuDNN 9; its wheels do not bundle them).
printf 'nvidia-cublas-cu12==12.8.4.1\nnvidia-cudnn-cu12==9.10.2.21\n' >"${WORK}/cuda.in"
for target in x86_64-manylinux_2_28:linux-x86_64 x86_64-pc-windows-msvc:windows-x86_64; do
  "${UV}" pip compile "${WORK}/cuda.in" --python-version 3.12 \
    --python-platform "${target%%:*}" --generate-hashes --no-annotate --no-header \
    -o "${RUNTIME}/requirements-cuda-${target##*:}.txt"
done

# MFA 3.4.1 (with Kaldi and FFmpeg) for Linux, Windows and Intel Macs.
solve_mfa() {
  local platform="$1"
  CONDA_OVERRIDE_GLIBC=2.28 CONDA_OVERRIDE_LINUX=5.15 CONDA_OVERRIDE_OSX=14.0 \
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
solve_mfa osx-64
