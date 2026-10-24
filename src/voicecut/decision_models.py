"""Decision models VoiceCut can plan with: which they are and how to reach them.

A decision model (TypeSafe's Jev and its open reproductions) answers typed
questions about a text with probabilities and never writes text, so it cannot
break the planner's answer format.  It cannot pick word ranges either: the
decision planner (:mod:`voicecut.decision_planner`) asks it, for every
transcript line, which later line says it again and whether the line is
narration at all, and cuts lines that are said again or are not narration.

Compared on a 64-minute narration against the user's own edit (mean of F1 on
the words cut and on the words kept, on minutes 0-10 and 30-40): Gemma 4 31B
0.90-0.93, hosted Jev 0.89, Kev 9B 0.85, Kev 4B 0.82, every LLM of 14B
parameters or less 0.62 or worse.  Kev plans an hour of recording in about 19
(9B) or 9 (4B) minutes on an M1 Max, where Gemma 4 31B takes longer than the
recording.

This module is only the registry (no model code), so the editor can list the
models without loading anything.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field

from . import local_models


@dataclass(frozen=True)
class Protocol:
    """How the planner questions a model (see :mod:`voicecut.decision_planner`)."""

    # Lines decided per request, and the transcript shown before and after them.
    block_lines: int = 10
    back_seconds: float = 0.0
    ahead_seconds: float = 30.0
    # Later lines offered as the retake of a line (besides "none").
    options: int = 10
    # The second question: "narration" (cut when the line is not narration) or
    # "junk" (cut when the line is a stray word, filler or a remark to self).
    side_question: str = "narration"
    # Whether a retake may be a reworded or longer re-read of the line.
    reworded: bool = False


@dataclass(frozen=True)
class DecisionModel:
    id: str
    label: str
    # The planner backend whose model list shows it: "ollama" (models on this
    # computer) or "openrouter".
    backend: str
    protocol: Protocol = field(default_factory=Protocol)
    # Local models: the Kev checkpoint (a LoRA adapter and its pointer head)
    # and the base model it adapts, as pinned Hugging Face revisions.
    adapter_repo: str | None = None
    adapter_revision: str | None = None
    base_repo: str | None = None
    base_revision: str | None = None
    size_gb: float | None = None
    # Memory a local model needs: with float (bf16) weights from
    # ``full_memory_gb`` up, below that quantized to ``bits`` (the same edit
    # on a 10-minute test, about 1.5 times slower on an M1 Max), from
    # ``memory_gb`` up.
    memory_gb: float | None = None
    full_memory_gb: float | None = None
    bits: int | None = None


KEV_9B = DecisionModel(
    id="jaredpalmer/kev-9b",
    label="Kev 9B",
    backend="ollama",
    protocol=Protocol(side_question="narration"),
    adapter_repo="jaredpalmer/kev-9b",
    adapter_revision="v1.0",
    base_repo="Qwen/Qwen3.5-9B-Base",
    base_revision="68c46c4b3498877f3ef123c856ecfde50c39f404",
    size_gb=19.4,
    # Peak 11.8 GB at 8 bits, 26 GB in bf16 (measured on an M1 Max).
    memory_gb=18,
    full_memory_gb=32,
    bits=8,
)
KEV_4B = DecisionModel(
    id="jaredpalmer/kev-4b",
    label="Kev 4B",
    backend="ollama",
    protocol=Protocol(side_question="junk"),
    adapter_repo="jaredpalmer/kev-4b",
    adapter_revision="v1.0",
    base_repo="Qwen/Qwen3.5-4B-Base",
    base_revision="1001bb4d826a52d1f399e183466143f4da7b741b",
    size_gb=9.4,
    # Peak 7.1 GB at 8 bits, 14 GB in bf16.
    memory_gb=12,
    full_memory_gb=24,
    bits=8,
)
JEV = DecisionModel(
    id="typesafe/jev-1.14",
    label="TypeSafe Jev 1.14",
    backend="openrouter",
    protocol=Protocol(
        back_seconds=0.0,
        ahead_seconds=120.0,
        options=40,
        side_question="junk",
        reworded=True,
    ),
)
# In the order the model lists show them (after the recommended LLMs).
LOCAL: tuple[DecisionModel, ...] = (KEV_9B, KEV_4B)
HOSTED: tuple[DecisionModel, ...] = (JEV,)


def find(backend: str | None, model: str | None) -> DecisionModel | None:
    """The decision model ``backend`` / ``model`` name, if it is one."""

    if backend is None or model is None:
        return None
    for item in (*LOCAL, *HOSTED):
        if item.backend == backend and item.id == model.strip():
            return item
    return None


def runs_here(model: DecisionModel) -> bool:
    """Whether this computer can run a local decision model: the planner runs
    Kev on MLX (Apple Silicon) and needs the memory for its weights."""

    if model.backend != "ollama":
        return True
    memory = local_models.memory_gb()
    return (
        sys.platform == "darwin"
        and platform.machine() == "arm64"
        and memory is not None
        and memory >= (model.memory_gb or 0)
    )


def local_models_here() -> list[DecisionModel]:
    return [model for model in LOCAL if runs_here(model)]


def weight_repos(model: DecisionModel) -> list[tuple[str, str | None]]:
    """The Hugging Face repositories (and revisions) a local model needs."""

    return [
        (repo, revision)
        for repo, revision in (
            (model.adapter_repo, model.adapter_revision),
            (model.base_repo, model.base_revision),
        )
        if repo
    ]


# Files of a checkpoint the planner reads (weights, configs, tokenizer, head).
WEIGHT_PATTERNS = ("*.json", "*.safetensors", "*.pt", "*.txt", "*.jinja")


def weights_ready(model: DecisionModel) -> bool:
    """Whether every file a local model needs is in the Hugging Face cache."""

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return False
    for repo, revision in weight_repos(model):
        try:
            snapshot_download(
                repo,
                revision=revision,
                allow_patterns=list(WEIGHT_PATTERNS),
                local_files_only=True,
            )
        except Exception:  # noqa: BLE001 - any miss means "not downloaded"
            return False
    return True
