"""How far a pipeline stage has got, and how long the stages should take.

A stage process prints ``[progress] done/total`` lines on its standard output
(readable in a terminal too); the editor's worker turns them into progress
events of the running stage.  Before the first stage the pipeline estimates
how long each stage takes for the recording and the chosen planner, from the
speeds measured on an M1 Max; the editor scales the estimates by how fast
this computer turned out to be on the stages already done.
"""

from __future__ import annotations

import re

PATTERN = re.compile(r"^\[progress\] (\d+)/(\d+)")

# Seconds of work per second of recording, and fixed seconds (model loading),
# per stage, measured on an M1 Max (64 GB) with a 10-minute narration.  They
# only set how big the stages are relative to each other until this computer
# has measured its own (see voicecut.editor.speeds).
STAGE_RATES: dict[str, tuple[float, float]] = {
    "preflight": (0.0, 2.0),
    "media_input": (0.002, 1.0),
    "analysis": (0.007, 1.0),
    "transcription": (0.2, 8.0),
    "hidden_retry_recovery": (0.03, 5.0),
    "gap_recovery": (0.012, 2.0),
    "final_render": (0.1, 8.0),
    "publication": (0.002, 0.5),
}
# The take selection, per planner: seconds per second of recording.
CLOUD_PLAN_RATE = 0.35  # Gemini, OpenRouter and the other cloud LLMs
LOCAL_LLM_PLAN_RATE = 1.5  # Gemma 4 31B through Ollama on an M1 Max
PLAN_RATE_BY_MODEL = {
    "jaredpalmer/kev-9b": 0.3,
    "jaredpalmer/kev-4b": 0.15,
    "typesafe/jev-1.14": 0.01,
}


def report(done: int, total: int, what: str = "") -> None:
    """Print one progress line of the running stage."""

    print(f"[progress] {done}/{total}{' ' + what if what else ''}", flush=True)


def stage_seconds(
    audio_seconds: float, *, planner_backend: str, planner_model: str
) -> dict[str, float]:
    """How long each stage should take on the reference computer."""

    estimates = {
        stage: rate * audio_seconds + fixed for stage, (rate, fixed) in STAGE_RATES.items()
    }
    rate = PLAN_RATE_BY_MODEL.get(planner_model)
    if rate is None:
        rate = LOCAL_LLM_PLAN_RATE if planner_backend == "ollama" else CLOUD_PLAN_RATE
    estimates["semantic_plan"] = rate * audio_seconds + 5.0
    return {stage: round(value, 1) for stage, value in estimates.items()}
