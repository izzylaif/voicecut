"""How fast Voice Cut's stages run on this computer, learned from its own runs.

Every finished Voice Cut records, per stage, its seconds of work per second of
recording (beyond the stage's fixed start-up time, see
:data:`voicecut.progress.STAGE_RATES`); the take selection is recorded per
planner model.  The next run's time estimate uses these instead of the
reference computer's speeds, so it fits this computer, its system and the
models it uses.  A new measurement is averaged with the earlier ones.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .. import progress

FILE_NAME = "run_speeds.json"
# Weight of the newest run in the running average.
NEW_WEIGHT = 0.5
PLAN_FIXED_SECONDS = 5.0


def _fixed(stage: str) -> float:
    return PLAN_FIXED_SECONDS if stage == "semantic_plan" else progress.STAGE_RATES.get(stage, (0.0, 0.0))[1]


def stage_durations(events: list[dict[str, Any]]) -> dict[str, float]:
    """Seconds each stage ran in a job (stages that completed, not cached)."""

    started: dict[str, float] = {}
    durations: dict[str, float] = {}
    for event in events:
        if event.get("type") != "stage":
            continue
        stage, status, at = str(event.get("stage")), event.get("status"), event.get("at")
        if not isinstance(at, (int, float)):
            continue
        if status == "started":
            started[stage] = float(at)
        elif status == "completed" and stage in started:
            durations[stage] = max(0.0, float(at) - started.pop(stage))
    return durations


class RunSpeeds:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"stages": {}, "plan": {}}
        if not isinstance(value, dict):
            return {"stages": {}, "plan": {}}
        return {
            "stages": dict(value.get("stages") or {}),
            "plan": dict(value.get("plan") or {}),
        }

    def estimate(
        self, audio_seconds: float, planner_model: str, reference: dict[str, float]
    ) -> tuple[dict[str, float], bool]:
        """Expected seconds per stage, learned where this computer has run the
        stage (and the planner model) before; whether any of it was learned."""

        learned = self._read()
        stages = dict(reference)
        measured = False
        for stage, rate in learned["stages"].items():
            if isinstance(rate, (int, float)) and stage in stages:
                stages[stage] = round(float(rate) * audio_seconds + _fixed(stage), 1)
                measured = True
        rate = learned["plan"].get(planner_model)
        if isinstance(rate, (int, float)):
            stages["semantic_plan"] = round(
                float(rate) * audio_seconds + PLAN_FIXED_SECONDS, 1
            )
            measured = True
        return stages, measured

    def record(
        self, events: list[dict[str, Any]], audio_seconds: float, planner_model: str
    ) -> None:
        """Remember how fast the stages of a finished run went."""

        if audio_seconds <= 0:
            return
        durations = stage_durations(events)
        with self._lock:
            learned = self._read()
            for stage, seconds in durations.items():
                rate = max(0.0, seconds - _fixed(stage)) / audio_seconds
                if stage == "semantic_plan":
                    table, key = learned["plan"], planner_model
                elif stage in progress.STAGE_RATES:
                    table, key = learned["stages"], stage
                else:
                    continue
                previous = table.get(key)
                table[key] = round(
                    rate
                    if not isinstance(previous, (int, float))
                    else NEW_WEIGHT * rate + (1 - NEW_WEIGHT) * float(previous),
                    5,
                )
            from .projects import write_json_atomic

            write_json_atomic(self.path, learned)
