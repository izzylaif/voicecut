#!/usr/bin/env python3
"""Recover speech that the primary transcript skipped.

Whisper's decoder tends to drop text that repeats what it has just written, so
a narrator's third attempt at a phrase can be audible in the recording but
absent from the transcript.  The take planner can only choose between words it
sees; an untranscribed retake that sits between two kept words is therefore
published as part of the edit.

This stage finds speech-level stretches of the waveform that no transcript
word covers, transcribes each stretch on its own, and inserts the recovered
words chronologically.  It runs with the same Whisper engine as transcription
(mlx-whisper in the MLX environment, or faster-whisper).
"""

from __future__ import annotations

import argparse
import copy
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from .transcribe_mlx import (
    DEFAULT_MODELS,
    SAMPLE_RATE,
    default_whisper_engine,
    load_audio,
    read_json,
    run_asr,
    serialize_result,
    sha256_file,
    write_json,
)

STRATEGY = "uncovered_speech_retranscription_v1"
HOP_SECONDS = 0.005
LOUD_MARGIN_DB = 15.0
MERGE_SECONDS = 0.15
COVER_MARGIN_SECONDS = 0.05
MIN_REGION_SECONDS = 0.30
CROP_PAD_SECONDS = 0.30
KEEP_MARGIN_SECONDS = 0.10
MAX_NO_SPEECH_PROBABILITY = 0.6
MIN_AVG_LOGPROB = -1.2
# On noise, breaths and mumbling Whisper confidently hallucinates short
# phrases ("Thank you."), but with one very uncertain word.  A skipped retake
# is several confidently recognized words.
MIN_RECOVERED_WORDS = 2
MIN_MEAN_WORD_PROBABILITY = 0.6
MIN_WORD_PROBABILITY = 0.3


def frame_levels(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """RMS level (dB) of consecutive ``HOP_SECONDS`` frames."""

    hop = max(1, round(HOP_SECONDS * sample_rate))
    frames = len(audio) // hop
    blocks = audio[: frames * hop].reshape(frames, hop)
    energy = np.einsum("ij,ij->i", blocks, blocks) / hop
    return (10.0 * np.log10(energy + 1e-14)).astype(np.float32)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    edges = np.flatnonzero(np.diff(np.concatenate([[0], mask.astype(np.int8), [0]])))
    return list(zip(edges[::2].tolist(), edges[1::2].tolist(), strict=True))


def transcript_words(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        word
        for atom in transcript.get("atoms", [])
        if isinstance(atom, dict)
        for word in atom.get("words", [])
        if isinstance(word, dict) and str(word.get("word", "")).strip()
    ]


def uncovered_speech(
    levels: np.ndarray,
    words: list[dict[str, Any]],
) -> list[tuple[float, float]]:
    """Speech-level stretches of at least ``MIN_REGION_SECONDS`` with no word."""

    covered = np.zeros(len(levels), bool)
    in_word = np.zeros(len(levels), bool)
    for word in words:
        first = int(float(word["start"]) / HOP_SECONDS)
        last = int(float(word["end"]) / HOP_SECONDS) + 1
        in_word[max(0, first) : last] = True
        margin = int(COVER_MARGIN_SECONDS / HOP_SECONDS)
        covered[max(0, first - margin) : last + margin] = True
    if not in_word.any():
        return []
    speech_db = float(np.median(levels[in_word]))
    loud = levels >= speech_db - LOUD_MARGIN_DB
    merge = int(MERGE_SECONDS / HOP_SECONDS)
    regions: list[list[int]] = []
    for start, stop in _runs(loud):
        if regions and start - regions[-1][1] <= merge:
            regions[-1][1] = stop
        else:
            regions.append([start, stop])
    result: list[tuple[float, float]] = []
    minimum = int(MIN_REGION_SECONDS / HOP_SECONDS)
    for start, stop in regions:
        for first, last in _runs(~covered[start:stop]):
            if last - first >= minimum:
                result.append(
                    ((start + first) * HOP_SECONDS, (start + last) * HOP_SECONDS)
                )
    return result


def _overlaps_existing(word: dict[str, Any], words: list[dict[str, Any]]) -> bool:
    start, end = float(word["start"]), float(word["end"])
    length = max(1e-3, end - start)
    for other in words:
        overlap = min(end, float(other["end"])) - max(start, float(other["start"]))
        if overlap > 0.5 * length:
            return True
    return False


def insert_words(
    transcript: dict[str, Any],
    recovered: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a copy with ``recovered`` words placed chronologically.

    A recovered word goes into the analysis atom whose time span contains it;
    otherwise it starts a new atom.  Atoms are then re-indexed by start time so
    every consumer reads the words in recording order.
    """

    result = copy.deepcopy(transcript)
    atoms = [atom for atom in result.get("atoms", []) if isinstance(atom, dict)]
    for word in recovered:
        middle = (float(word["start"]) + float(word["end"])) / 2
        owner = next(
            (
                atom
                for atom in atoms
                if float(atom.get("start", 0.0))
                <= middle
                <= float(atom.get("end", 0.0))
            ),
            None,
        )
        if owner is None:
            owner = {
                "start": float(word["start"]),
                "end": float(word["end"]),
                "decode_strategy": STRATEGY,
                "text": "",
                "segments": [],
                "words": [],
            }
            atoms.append(owner)
        owner["words"] = sorted(
            [*owner.get("words", []), word], key=lambda item: float(item["start"])
        )
        owner["start"] = min(float(owner["start"]), float(word["start"]))
        owner["end"] = max(float(owner["end"]), float(word["end"]))
    atoms.sort(key=lambda atom: float(atom.get("start", 0.0)))
    for index, atom in enumerate(atoms):
        atom.setdefault("source_atom_index", atom.get("atom_index"))
        atom["atom_index"] = index
    result["atoms"] = atoms
    return result


def recover_gaps(
    *,
    audio_path: Path,
    transcript_path: Path,
    output_path: Path,
    model: str,
    language: str,
    engine: str = "mlx-whisper",
) -> dict[str, Any]:
    started = time.time()
    transcript = read_json(transcript_path)
    if not isinstance(transcript, dict):
        raise TypeError("transcript root must be an object")
    waveform = load_audio(audio_path)
    words = transcript_words(transcript)
    regions = uncovered_speech(frame_levels(waveform, SAMPLE_RATE), words)
    recovered: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for region_start, region_end in regions:
        crop_start = max(0.0, region_start - CROP_PAD_SECONDS)
        crop_end = min(len(waveform) / SAMPLE_RATE, region_end + CROP_PAD_SECONDS)
        clip = waveform[round(crop_start * SAMPLE_RATE) : round(crop_end * SAMPLE_RATE)]
        result = serialize_result(
            run_asr(
                clip,
                model=model,
                language=language,
                word_timestamps=True,
                prompt=None,
                engine=engine,
            ),
            offset=crop_start,
            clip_start=crop_start,
            clip_end=crop_end,
        )
        accepted: list[dict[str, Any]] = []
        for segment in result["segments"]:
            if (
                segment["no_speech_prob"] > MAX_NO_SPEECH_PROBABILITY
                or segment["avg_logprob"] < MIN_AVG_LOGPROB
            ):
                continue
            inside = [
                word
                for word in segment["words"]
                if region_start - KEEP_MARGIN_SECONDS
                <= (word["start"] + word["end"]) / 2
                <= region_end + KEEP_MARGIN_SECONDS
                and not _overlaps_existing(word, words)
            ]
            probabilities = [float(word["probability"]) for word in inside]
            if (
                len(inside) < MIN_RECOVERED_WORDS
                or float(np.mean(probabilities)) < MIN_MEAN_WORD_PROBABILITY
                or min(probabilities) < MIN_WORD_PROBABILITY
            ):
                continue
            accepted.extend({**word, "recovered_from_gap": True} for word in inside)
        recovered.extend(accepted)
        reports.append(
            {
                "start": region_start,
                "end": region_end,
                "text": " ".join(word["word"] for word in accepted),
                "recovered_words": len(accepted),
            }
        )
        print(
            f"gap {region_start:8.2f}-{region_end:8.2f}: "
            f"{' '.join(word['word'] for word in accepted) or '(nothing)'}",
            flush=True,
        )
    output = insert_words(transcript, recovered)
    output["gap_recovery"] = {
        "schema_version": 1,
        "status": "complete",
        "strategy": STRATEGY,
        "input_transcript": str(transcript_path.resolve()),
        "input_transcript_sha256": sha256_file(transcript_path),
        "model": model,
        "language": language,
        "regions": reports,
        "recovered_words": len(recovered),
        "elapsed_seconds": round(time.time() - started, 2),
    }
    write_json(output_path, output)
    return output["gap_recovery"]


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m voicecut.gap_recovery")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--engine",
        choices=("mlx-whisper", "faster-whisper"),
        default=default_whisper_engine(),
    )
    parser.add_argument("--model", help="default: the engine's standard model")
    parser.add_argument("--language", default="en")
    args = parser.parse_args()
    report = recover_gaps(
        audio_path=args.audio,
        transcript_path=args.transcript,
        output_path=args.output,
        model=args.model or DEFAULT_MODELS[args.engine],
        language=args.language,
        engine=args.engine,
    )
    summary = {key: report[key] for key in ("status", "recovered_words")}
    summary["regions"] = len(report["regions"])
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
