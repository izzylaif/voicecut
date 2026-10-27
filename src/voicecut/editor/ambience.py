"""Clean room tone for pauses the user inserts or resizes.

The pauses VoiceCut made keep the room tone VoiceCut chose.  Any other pause is
filled the way ``cut_render`` fills one: from an :class:`~voicecut.cut_render.
AmbienceBank` of verified-quiet stretches of the recording, found by
``cut_render.build_ambience_bank`` away from every word.  The signal levels are
the renderer's own (``frame_levels``' two-hop RMS, computed here in streaming
blocks so an hour of audio is never loaded at once) and the noise and sound
thresholds are the ones VoiceCut measured for this recording, kept in its plan.

Taking room tone is deterministic per pause, so a pause sounds the same in the
preview and in the export.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .. import cut_render
from ..common import read_json
from .timeline import Proposal

CACHE_VERSION = 2


def _levels(audio_path: Path) -> tuple[np.ndarray, int, int]:
    """``cut_render.frame_levels`` of the mono mix, block by block."""

    info = sf.info(str(audio_path))
    sample_rate, frames = int(info.samplerate), int(info.frames)
    hop = max(1, round(cut_render.HOP_SECONDS * sample_rate))
    full = frames // hop
    blocks = np.zeros(full + 2, np.float64)
    position = 0
    for block in sf.blocks(
        str(audio_path), blocksize=hop * 20_000, dtype="float32", always_2d=True
    ):
        mono = block[:, 0] if block.shape[1] == 1 else block.mean(axis=1)
        whole = min(len(mono) // hop, full - position)
        if whole > 0:
            chunk = mono[: whole * hop].reshape(-1, hop)
            blocks[position + 1 : position + 1 + whole] = np.einsum(
                "ij,ij->i", chunk, chunk
            )
        position += whole
        rest = mono[whole * hop :]
        if len(rest) and position >= full:
            blocks[full + 1] += float(np.dot(rest, rest))
    energy = (blocks[:-1] + blocks[1:]) / (2 * hop)
    return (10.0 * np.log10(energy + 1e-14)).astype(np.float32), sample_rate, frames


def _thresholds(
    plan: dict[str, Any] | None, db: np.ndarray
) -> tuple[float, float, float]:
    signal = plan.get("signal") if plan else None
    if isinstance(signal, dict) and all(
        isinstance(signal.get(key), (int, float))
        for key in ("noise_floor_db", "speech_db", "sound_threshold_db")
    ):
        return (
            float(signal["noise_floor_db"]),
            float(signal["speech_db"]),
            float(signal["sound_threshold_db"]),
        )
    # No Voice Cut run yet: cut_render.analyze_signal's rule without words.
    floor = float(np.percentile(db, 20))
    return floor, floor + 30.0, floor + max(6.0, min(15.0, 0.2 * 30.0))


def _words(plan: dict[str, Any] | None) -> list[cut_render.PlanWord]:
    words = []
    for index, word in enumerate((plan or {}).get("words") or []):
        if isinstance(word, dict) and isinstance(word.get("start"), (int, float)):
            words.append(
                cut_render.PlanWord(
                    id=index,
                    text=str(word.get("text", "")),
                    start=float(word["start"]),
                    end=float(word.get("end", word["start"])),
                )
            )
    return words


def ambience_runs(
    audio_path: Path, proposal: Proposal | None, cache_path: Path
) -> list[tuple[int, int]]:
    """Source ranges of clean room tone in the recording (cached)."""

    key = f"{CACHE_VERSION}:{proposal.plan_sha256 if proposal else 'none'}"
    if cache_path.is_file():
        try:
            cached = read_json(cache_path)
            if cached.get("key") == key:
                return [(int(start), int(end)) for start, end in cached["runs"]]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    plan = read_json(proposal.plan_path) if proposal else None
    db, sample_rate, frames = _levels(audio_path)
    floor, speech, sound = _thresholds(plan, db)
    signal = cut_render.SignalAnalysis(
        sample_rate=sample_rate,
        db=db,
        breath=None,
        breath_threshold=cut_render.BREATH_PROBABILITY,
        noise_floor_db=floor,
        speech_db=speech,
        sound_threshold_db=sound,
    )
    bank = cut_render.build_ambience_bank(signal, _words(plan), frames)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"key": key, "runs": bank.runs}), encoding="utf-8")
    return list(bank.runs)


class AmbienceRoomTone:
    """Room tone for one pause: deterministic in the pause's id."""

    def __init__(self, runs: list[tuple[int, int]], sample_rate: int) -> None:
        self.runs = runs
        self.sample_rate = sample_rate

    def __call__(
        self, length: int, near_sample: int, seed: str
    ) -> list[tuple[int, int]]:
        if not self.runs:
            return []  # nothing clean to use: the pause is silent
        bank = cut_render.AmbienceBank(
            runs=list(self.runs),
            sample_rate=self.sample_rate,
            status="complete",
            cursor=zlib.crc32(seed.encode("utf-8")) % 9973,
        )
        return bank.take(length, near_sample)
