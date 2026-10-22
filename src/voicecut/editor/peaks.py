"""Waveform peaks of the canonical WAV.

A peak bin covers ``spp`` (samples per peak) consecutive source samples and
stores the minimum and maximum over all channels as int16 (full scale
±32767), interleaved ``[min0, max0, min1, max1, ...]``.  Bin ``i`` covers
samples ``[i * spp, (i + 1) * spp)``.

The editor precomputes one base level (:data:`BASE_SPP`) for the whole
recording; the browser derives coarser levels from it and asks for finer
levels (:data:`FINE_SPPS`) only for the stretch it is zoomed into.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

BASE_SPP = 256
FINE_SPPS = (16, 32, 64, 128)
MAX_FINE_BINS = 1 << 16
BLOCK_BINS = 4096


def _quantise(values: np.ndarray) -> np.ndarray:
    return np.round(np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2")


def _bins(block: np.ndarray, spp: int) -> np.ndarray:
    """Interleaved min/max int16 bins of a ``(frames, channels)`` block."""

    count = -(-len(block) // spp)
    padded = np.zeros((count * spp, block.shape[1]), np.float32)
    padded[: len(block)] = block
    # Pad a partial last bin with its own last sample so the padding cannot
    # widen the bin's min/max envelope.
    if len(block) % spp:
        padded[len(block) :] = block[-1]
    frames = padded.reshape(count, spp * block.shape[1])
    result = np.empty(count * 2, "<i2")
    result[0::2] = _quantise(frames.min(axis=1))
    result[1::2] = _quantise(frames.max(axis=1))
    return result


def compute_base_peaks(audio_path: Path, output_path: Path) -> int:
    """Write the base peak level of ``audio_path``; return the bin count."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    count = 0
    with temporary.open("wb") as handle:
        for block in sf.blocks(
            str(audio_path),
            blocksize=BASE_SPP * BLOCK_BINS,
            dtype="float32",
            always_2d=True,
        ):
            bins = _bins(block, BASE_SPP)
            handle.write(bins.tobytes())
            count += len(bins) // 2
    temporary.replace(output_path)
    return count


def fine_peaks(audio_path: Path, *, spp: int, first_bin: int, count: int) -> bytes:
    """Peaks at a fine level for bins ``[first_bin, first_bin + count)``."""

    if spp not in FINE_SPPS:
        raise ValueError(f"spp must be one of {FINE_SPPS}")
    if first_bin < 0 or not 0 < count <= MAX_FINE_BINS:
        raise ValueError("invalid peak range")
    with sf.SoundFile(str(audio_path)) as handle:
        start = first_bin * spp
        if start >= handle.frames:
            return b""
        handle.seek(start)
        block = handle.read(
            min(count * spp, handle.frames - start), dtype="float32", always_2d=True
        )
    return _bins(block, spp).tobytes()
