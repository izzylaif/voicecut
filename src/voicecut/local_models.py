"""The planner models VoiceCut recommends for running on this computer.

Ollama runs two kinds of model: GGUF through llama.cpp on every system, and
safetensors through MLX on Apple Silicon (a preview, which Ollama recommends
for Macs with more than 32 GB of memory).  A recommendation names the tag of
each kind where both exist; this computer is offered the MLX one when it can
run it.  Any other model of the Ollama library (or a GGUF on Hugging Face,
``hf.co/<user>/<repo>:<quant>``) works too.

Whether a model reasons before it answers is a setting of the model: off, on,
or a level (``low``, ``medium``, ``high``) for models that take one.  Each
recommendation comes with the setting that planned best.
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys
from dataclasses import dataclass

MLX_MIN_MEMORY_GB = 32
# What a model good enough for the take planner needs (about 30B parameters
# at 4 bits, its context, and the rest of the computer).
MEMORY_NEEDED_GB = 32
# What planning on this computer costs in time (measured below: on an M1 Max
# longer than the recording); how long depends on the computer.
SPEED_NOTE = "Running locally may take longer."
THINK_LEVELS = ("low", "medium", "high")
# False, True, or one of THINK_LEVELS.
Think = bool | str


@dataclass(frozen=True)
class Recommendation:
    label: str
    gguf: str
    gguf_gb: float
    mlx: str | None = None
    mlx_gb: float | None = None
    # How the model reasons before answering, unless the user says otherwise
    # for this model.
    think: Think = True


# The first one is the default.  Compared on a 64-minute narration with
# Gemini 3.1 Pro listening to the audio (agreement on the words removed, F1),
# Gemma 4 31B planned best (0.92 without reasoning; reasoning did not help)
# and Qwen 3.8 27B next (0.90 reasoning at "low"; without reasoning its
# reviews broke the answer's format).  Every model of 14B parameters or less
# scored 0.57-0.65, Qwen 3.6 35B-A3B 0.76 and Qwen 3.5 122B-A10B 0.78.
# Quantized, Gemma 4 31B kept its edit: on 12 minutes of that narration the
# MLX tag (NVFP4) differed from the full model in 10 of 1668 words and the
# QAT GGUF (Q4_0) in 65, while two runs of the full model differed in 60.
# Both read the prompt at about 50 and answered at about 30 tokens/s on an
# M1 Max: planning took longer than the recording (SPEED_NOTE).
RECOMMENDED: tuple[Recommendation, ...] = (
    Recommendation(
        label="Gemma 4 31B",
        gguf="gemma4:31b-it-qat",
        gguf_gb=18.9,
        mlx="gemma4:31b-mlx",
        mlx_gb=19.4,
        think=False,
    ),
    Recommendation(
        label="Qwen 3.8 27B",
        gguf="qwen3.8:27b",
        gguf_gb=17.7,
        mlx="qwen3.8:27b-mlx",
        mlx_gb=18.2,
        think="low",
    ),
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


def recommended_think(model: str) -> Think | None:
    """How a recommended model reasons by default (None: not recommended)."""

    for item in RECOMMENDED:
        if model in (item.gguf, item.mlx):
            return item.think
    return None


def is_think(value: object) -> bool:
    return isinstance(value, bool) or value in THINK_LEVELS


def fit_think(value: Think, choices: list[Think] | None) -> Think | None:
    """``value`` as the model takes it, given the values it accepts.

    A level becomes plain reasoning for a model without levels, plain
    reasoning becomes the middle level for one with only levels, and "off"
    becomes the model's own default (None) for one that always reasons.
    """

    if not choices or value in choices:
        return value
    if isinstance(value, str):
        return True if True in choices else None
    if value is True:
        return "medium" if "medium" in choices else None
    return None
