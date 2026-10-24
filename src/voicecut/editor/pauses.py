"""The pause widths the editor gives VoiceCut's pauses, for every project.

VoiceCut's renderer aims each pause at a target by its class: inside a
sentence, after a comma, after a sentence, after a paragraph
(``cut_render.PAUSE_CLASSES``).  The user can change those widths once, in
Settings: every VoiceCut pause on a timeline then moves by the difference
between the chosen width and the renderer's target for its class (the
silence already around the cut stays, so the pause heard is about the chosen
width).  Pauses the user resized are manual pauses and keep their length.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .. import cut_render

FILE_NAME = "pause_widths.json"
CLASSES = ("none", "comma", "sentence", "paragraph")
DEFAULTS: dict[str, float] = {name: cut_render.PAUSE_CLASSES[name][1] for name in CLASSES}
LONGEST_SECONDS = 5.0


class PauseWidthError(ValueError):
    """Pause widths that cannot be used."""


def targets() -> dict[str, float]:
    """The renderer's target per pause class, the ends included."""

    return {
        **{name: values[1] for name, values in cut_render.PAUSE_CLASSES.items()},
        "lead": cut_render.LEAD_IN_SECONDS,
        "tail": cut_render.LEAD_OUT_SECONDS,
    }


class PauseWidths:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self) -> dict[str, float]:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        widths = dict(DEFAULTS)
        if isinstance(saved, dict):
            for name in CLASSES:
                value = saved.get(name)
                if isinstance(value, (int, float)) and 0 <= value <= LONGEST_SECONDS:
                    widths[name] = float(value)
        return widths

    def set(self, values: Any) -> dict[str, float]:
        """Save ``values`` ({class: seconds}, the rest unchanged); None resets."""

        from .projects import write_json_atomic

        if values is None:
            self.path.unlink(missing_ok=True)
            return self.get()
        if not isinstance(values, dict):
            raise PauseWidthError("pause widths must be an object of seconds by pause class")
        widths = self.get()
        for name, value in values.items():
            if name not in CLASSES:
                raise PauseWidthError(f"unknown pause class {name!r}")
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0 <= value <= LONGEST_SECONDS
            ):
                raise PauseWidthError(f"the {name} pause must be 0 to {LONGEST_SECONDS:g} seconds")
            widths[name] = round(float(value), 3)
        write_json_atomic(self.path, widths)
        return widths
