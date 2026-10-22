"""Pinned Respiro-en frame probabilities mapped to canonical source samples."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np

from .common import sha256_file

RESPIRO_UPSTREAM_COMMIT = "70e01c60c2f582c41092730680f2894ab24d6467"
RESPIRO_MODULES_SHA256 = (
    "f789e0986e3090d7df5f9f0f596d9e3601c6da514c3ac01a65920a493b840e46"
)
RESPIRO_CHECKPOINT_SHA256 = (
    "1f4a9b96f96645c480bf0e07b1e18cd68878ac0b4bb5dc920ad93f9b17df858a"
)
RESPIRO_LICENSE_SHA256 = (
    "a34ad1af58dc7c02f867f620f7ddc952029b383c9b0dce349d54f6b875e079cd"
)
RESPIRO_SAMPLE_RATE = 16_000
RESPIRO_FRAME_HOP_SAMPLES = 160
RESPIRO_FRAME_HOP_MS = 10
DEFAULT_BREATH_THRESHOLD = 0.5

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESPIRO_CACHE_ROOT = (
    PROJECT_ROOT
    / ".voicecut-cache"
    / "runtime"
    / "respiro-en"
    / RESPIRO_UPSTREAM_COMMIT
)


class BreathDetectionError(RuntimeError):
    """Pinned Respiro-en evidence could not be produced safely."""


@dataclass(frozen=True)
class RespiroRuntime:
    """Verified upstream model plus its unchanged feature extractor."""

    model: Any
    feature_extractor: Callable[..., tuple[Any, Any]]
    device: Any
    device_name: str
    cache_root: Path

    def infer(self, waveform_16khz: np.ndarray) -> np.ndarray:
        import torch

        waveform = np.asarray(waveform_16khz, dtype=np.float32)
        if waveform.ndim != 1 or not len(waveform):
            raise BreathDetectionError("Respiro-en requires a non-empty mono crop")
        feature, length = self.feature_extractor(
            waveform,
            sr=RESPIRO_SAMPLE_RATE,
        )
        feature = feature.to(self.device)
        length = length.to(self.device)
        with torch.inference_mode():
            output = self.model(feature, length)
        probabilities = output[0].detach().to("cpu").numpy().astype(np.float32)
        return _validate_probabilities(probabilities)


_RUNTIME_CACHE: dict[tuple[Path, str], RespiroRuntime] = {}


def _verified_runtime_file(
    cache_root: Path,
    filename: str,
    expected_sha256: str,
) -> Path:
    path = (cache_root / filename).absolute()
    if not path.is_file() or path.is_symlink():
        raise BreathDetectionError(
            f"pinned Respiro-en file is missing or unsafe: {path}; "
            "run scripts/install.sh"
        )
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise BreathDetectionError(
            f"pinned Respiro-en hash mismatch for {path}: "
            f"expected {expected_sha256}, got {actual}"
        )
    return path


def verify_respiro_runtime(
    cache_root: Path = DEFAULT_RESPIRO_CACHE_ROOT,
) -> dict[str, str]:
    """Verify every pinned upstream file before importing executable code."""

    root = cache_root.expanduser().absolute()
    modules_path = _verified_runtime_file(
        root,
        "modules.py",
        RESPIRO_MODULES_SHA256,
    )
    checkpoint_path = _verified_runtime_file(
        root,
        "respiro-en.pt",
        RESPIRO_CHECKPOINT_SHA256,
    )
    license_path = _verified_runtime_file(
        root,
        "LICENSE",
        RESPIRO_LICENSE_SHA256,
    )
    return {
        "cache_root": str(root),
        "modules_path": str(modules_path),
        "checkpoint_path": str(checkpoint_path),
        "license_path": str(license_path),
    }


def _import_upstream_modules(path: Path) -> ModuleType:
    module_name = f"voicecut_respiro_en_{RESPIRO_UPSTREAM_COMMIT}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise BreathDetectionError(f"cannot import pinned Respiro-en module: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise BreathDetectionError(
            f"pinned Respiro-en module import failed: {type(error).__name__}: {error}"
        ) from error
    return module


def load_respiro_runtime(
    *,
    cache_root: Path = DEFAULT_RESPIRO_CACHE_ROOT,
    device: str | None = None,
) -> RespiroRuntime:
    """Load the verified official DetectionNet without modifying its source."""

    import torch

    paths = verify_respiro_runtime(cache_root)
    root = Path(paths["cache_root"])
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    key = (root, device)
    cached = _RUNTIME_CACHE.get(key)
    if cached is not None:
        return cached

    upstream = _import_upstream_modules(Path(paths["modules_path"]))
    detection_net = getattr(upstream, "DetectionNet", None)
    feature_extractor = getattr(upstream, "feature_extractor", None)
    if detection_net is None or not callable(feature_extractor):
        raise BreathDetectionError(
            "pinned Respiro-en module has no DetectionNet/feature_extractor"
        )
    torch_device = torch.device(device)
    try:
        model = detection_net().to(torch_device)
        checkpoint = torch.load(
            paths["checkpoint_path"],
            map_location=torch_device,
            weights_only=True,
        )
        state_dict = checkpoint["model"]
        load_result = model.load_state_dict(state_dict)
        if load_result.missing_keys or load_result.unexpected_keys:
            raise BreathDetectionError(
                "Respiro-en checkpoint has incompatible state-dict keys"
            )
        model.eval()
    except BreathDetectionError:
        raise
    except Exception as error:
        raise BreathDetectionError(
            f"Respiro-en model load failed: {type(error).__name__}: {error}"
        ) from error

    runtime = RespiroRuntime(
        model=model,
        feature_extractor=feature_extractor,
        device=torch_device,
        device_name=str(torch_device),
        cache_root=root,
    )
    _RUNTIME_CACHE[key] = runtime
    return runtime


def _validate_probabilities(probabilities: np.ndarray) -> np.ndarray:
    values = np.asarray(probabilities, dtype=np.float32)
    if values.ndim != 1 or not len(values):
        raise BreathDetectionError("Respiro-en returned no frame probabilities")
    if not np.all(np.isfinite(values)):
        raise BreathDetectionError("Respiro-en returned non-finite probabilities")
    if np.any(values < 0.0) or np.any(values > 1.0):
        raise BreathDetectionError("Respiro-en probabilities leave [0, 1]")
    return values


def breath_probabilities(
    mono: np.ndarray,
    sample_rate: int,
    *,
    cache_root: Path = DEFAULT_RESPIRO_CACHE_ROOT,
    runtime: RespiroRuntime | None = None,
) -> np.ndarray:
    """Respiro-en breath probabilities for a whole recording on a 10 ms grid.

    The recording is analysed in 30 s chunks overlapping by 2 s; each chunk
    contributes only away from its edges.  Frame ``k`` describes the
    ``RESPIRO_FRAME_HOP_MS`` frame starting at ``k * 10 ms``.
    """

    from math import gcd

    from scipy.signal import resample_poly

    runtime = runtime or load_respiro_runtime(cache_root=cache_root)
    divisor = gcd(sample_rate, RESPIRO_SAMPLE_RATE)
    audio = resample_poly(
        mono, RESPIRO_SAMPLE_RATE // divisor, sample_rate // divisor
    ).astype(np.float32)
    hop = RESPIRO_FRAME_HOP_SAMPLES
    chunk = 30 * RESPIRO_SAMPLE_RATE
    overlap = 2 * RESPIRO_SAMPLE_RATE
    total = len(audio) // hop + 1
    result = np.zeros(total, np.float32)
    start = 0
    while start < len(audio):
        end = min(len(audio), start + chunk)
        piece = audio[start:end]
        if len(piece) < hop * 4:
            break
        probabilities = runtime.infer(piece)
        first = start // hop
        keep_from = 0 if start == 0 else (overlap // 2) // hop
        values = probabilities[keep_from:]
        result[first + keep_from : first + keep_from + len(values)] = values[
            : max(0, total - first - keep_from)
        ]
        if end >= len(audio):
            break
        start = end - overlap
    return result


__all__ = [
    "DEFAULT_BREATH_THRESHOLD",
    "DEFAULT_RESPIRO_CACHE_ROOT",
    "RESPIRO_CHECKPOINT_SHA256",
    "RESPIRO_FRAME_HOP_MS",
    "RESPIRO_LICENSE_SHA256",
    "RESPIRO_MODULES_SHA256",
    "RESPIRO_SAMPLE_RATE",
    "RESPIRO_UPSTREAM_COMMIT",
    "BreathDetectionError",
    "RespiroRuntime",
    "breath_probabilities",
    "load_respiro_runtime",
    "verify_respiro_runtime",
]
