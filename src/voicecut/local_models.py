"""The planner models VoiceCut recommends for running on this computer.

Ollama runs two kinds of model: GGUF through llama.cpp on every system, and
safetensors through MLX on Apple Silicon (a preview, which Ollama recommends
for Macs with more than 32 GB of memory).  A recommendation names the tag of
each kind where both exist; this computer is offered the MLX one when it can
run it.  Any other model of the Ollama library (or a GGUF on Hugging Face,
``hf.co/<user>/<repo>:<quant>``) works too; these are the ones that kept the
take planner's contract best.
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys
from dataclasses import dataclass

MLX_MIN_MEMORY_GB = 32


@dataclass(frozen=True)
class Recommendation:
    label: str
    gguf: str
    gguf_gb: float
    mlx: str | None = None
    mlx_gb: float | None = None
    # Whether the model reasons before answering, unless the user says
    # otherwise for this model.
    think: bool = True


# The first one is the default.
RECOMMENDED: tuple[Recommendation, ...] = (
    Recommendation(
        label="Gemma 4 31B",
        gguf="gemma4:31b",
        gguf_gb=20.4,
        mlx="gemma4:31b-mlx",
        mlx_gb=19.4,
    ),
    Recommendation(
        label="Gemma 4 26B-A4B",
        gguf="gemma4:26b",
        gguf_gb=18.7,
        mlx="gemma4:26b-mlx",
        mlx_gb=18.3,
    ),
    Recommendation(
        label="Qwen 3.6 27B",
        gguf="qwen3.6:27b",
        gguf_gb=17.8,
        mlx="qwen3.6:27b-mlx",
        mlx_gb=18.8,
    ),
    Recommendation(
        label="Qwen 3.6 35B-A3B",
        gguf="qwen3.6:35b-a3b",
        gguf_gb=22.6,
        mlx="qwen3.6:35b-mlx",
        mlx_gb=23.6,
    ),
    Recommendation(label="gpt-oss 20B", gguf="gpt-oss:20b", gguf_gb=13.8),
)


def memory_gb() -> float | None:
    """This computer's physical memory."""

    if sys.platform == "win32":

        class Status(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("load", ctypes.c_ulong),
                ("total", ctypes.c_ulonglong),
                ("available", ctypes.c_ulonglong),
                ("page_total", ctypes.c_ulonglong),
                ("page_available", ctypes.c_ulonglong),
                ("virtual_total", ctypes.c_ulonglong),
                ("virtual_available", ctypes.c_ulonglong),
                ("extended", ctypes.c_ulonglong),
            ]

        status = Status()
        status.length = ctypes.sizeof(Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return status.total / 1e9
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9
    except (OSError, ValueError, AttributeError):
        return None


def uses_mlx() -> bool:
    """Whether Ollama should run the MLX version of a model here."""

    memory = memory_gb()
    return (
        sys.platform == "darwin"
        and platform.machine() == "arm64"
        and memory is not None
        and memory > MLX_MIN_MEMORY_GB
    )


def recommended() -> list[dict[str, object]]:
    """The recommendations as this computer would download them."""

    mlx = uses_mlx()
    entries = []
    for index, item in enumerate(RECOMMENDED):
        use_mlx = mlx and item.mlx is not None
        entries.append(
            {
                "id": item.mlx if use_mlx else item.gguf,
                "label": item.label,
                "size_gb": item.mlx_gb if use_mlx else item.gguf_gb,
                "engine": "MLX" if use_mlx else "GGUF",
                "think": item.think,
                "default": index == 0,
            }
        )
    return entries


def default_model() -> str:
    return str(recommended()[0]["id"])


def recommended_think(model: str) -> bool | None:
    """Whether a recommended model reasons by default (None: not recommended)."""

    for item in RECOMMENDED:
        if model in (item.gguf, item.mlx):
            return item.think
    return None
