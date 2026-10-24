#!/usr/bin/env python3
"""Primary local ASR for whole recordings and waveform-derived speech atoms.

This script intentionally runs each atom without previous-text conditioning.
That prevents a whole-file decoder from silently normalizing away restarts.

Three engines are available.  ``mlx-whisper`` runs Whisper on MLX (Apple
Silicon, the default there).  ``faster-whisper`` runs the same Whisper model
with CTranslate2 on an NVIDIA GPU or the CPU (the default on Linux, Windows and
Intel Macs), decoding exactly as ``mlx-whisper`` does: greedy, temperature 0,
no conditioning on previous text.  ``crisperwhisper`` runs CrisperWhisper 2.0,
a verbatim Whisper fine-tune that
keeps false starts, repetitions and fillers ("In a tr- in a transformer") as
separate timed words; Whisper tends to merge such restarts into one word, which
leaves the take planner nothing to cut.  CrisperWhisper's weights are released
for non-commercial research use only.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np


def _report_progress(done: int, total: int) -> None:
    # voicecut.progress's line, printed here so this module stays standalone.
    print(f"[progress] {done}/{total} atoms", flush=True)


ARTIFACT_ROLES = (
    "source_primary",
    "final_primary",
    "source_independent",
    "final_independent",
)
DEFAULT_MODELS = {
    "mlx-whisper": "mlx-community/whisper-large-v3-turbo",
    "faster-whisper": "large-v3-turbo",
    "crisperwhisper": "nyralabs/CrisperWhisper2.0_large",
}
SAMPLE_RATE = 16000
# float32 reproduces mlx-whisper's words and timings; int8 is faster on x86
# but invented a repeated phrase inside a cut-off take in testing.
FASTER_WHISPER_CPU_COMPUTE = "float32"
SOURCE_ATOM_STRATEGY = "analysis_acoustic_atoms_v1"
WHOLE_FILE_STRATEGY = "whole_file_v1"


def default_whisper_engine() -> str:
    """MLX Whisper on Apple Silicon, faster-whisper (CTranslate2) elsewhere."""

    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "mlx-whisper"
    return "faster-whisper"


def load_audio(path: Path) -> np.ndarray:
    """Mono 16 kHz float32 audio, decoded by FFmpeg as Whisper does it."""

    command = [
        "ffmpeg", "-nostdin", "-i", str(path),
        "-threads", "0", "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le",
        "-ar", str(SAMPLE_RATE), "-",
    ]  # fmt: skip
    try:
        output = subprocess.run(command, capture_output=True, check=True).stdout
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Failed to load audio: {error.stderr.decode()}") from error
    return np.frombuffer(output, np.int16).astype(np.float32) / 32768.0


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mlx_decode_config(
    *,
    mode: str,
    skip_whole: bool,
    max_atoms: int | None,
) -> dict[str, Any]:
    """Return every setting that materially controls this transcription run."""

    return {
        "sample_rate": SAMPLE_RATE,
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "word_timestamps": True,
        "hallucination_silence_threshold": 0.5,
        "mode": mode,
        "skip_whole": skip_whole,
        "max_atoms": max_atoms,
        "source_chunking": (
            SOURCE_ATOM_STRATEGY if mode == "source" else WHOLE_FILE_STRATEGY
        ),
    }


def build_provenance(
    audio: Path,
    *,
    artifact_role: str | None,
    engine: str,
    model: str,
    language: str,
    prompt: str | None,
    decode_config: dict[str, Any],
    analysis: Path | None = None,
) -> dict[str, Any]:
    return {
        "audio": str(audio.resolve()),
        "audio_sha256": sha256_file(audio),
        "artifact_role": artifact_role,
        "engine": engine,
        "model": model,
        "language": language,
        "decode_config": decode_config,
        "prompt_sha256": sha256_optional_text(prompt),
        "source_decode_strategy": (
            SOURCE_ATOM_STRATEGY if analysis is not None else WHOLE_FILE_STRATEGY
        ),
        "analysis": str(analysis.resolve()) if analysis is not None else None,
        "analysis_sha256": (sha256_file(analysis) if analysis is not None else None),
    }


def checkpoint_identity(
    provenance: dict[str, Any],
    *,
    mode: str,
    analysis_sha256: str | None,
) -> dict[str, Any]:
    """Select immutable run inputs that a partial checkpoint must match."""

    return {
        "audio": provenance["audio"],
        "audio_sha256": provenance["audio_sha256"],
        "artifact_role": provenance["artifact_role"],
        "engine": provenance["engine"],
        "model": provenance["model"],
        "language": provenance["language"],
        "decode_config": provenance["decode_config"],
        "prompt_sha256": provenance["prompt_sha256"],
        "mode": mode,
        "analysis_sha256": analysis_sha256,
    }


def validate_checkpoint_identity(
    checkpoint: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    mismatches = [
        field
        for field, expected_value in expected.items()
        if field not in checkpoint or checkpoint[field] != expected_value
    ]
    if mismatches:
        joined = ", ".join(mismatches)
        raise RuntimeError(f"ASR checkpoint does not match this run ({joined}).")


def clean_number(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def serialize_result(
    result: dict[str, Any],
    *,
    offset: float,
    clip_start: float,
    clip_end: float,
) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    all_words: list[dict[str, Any]] = []
    for raw_segment in result.get("segments", []):
        segment_start = max(
            clip_start,
            min(clip_end, offset + clean_number(raw_segment.get("start"))),
        )
        segment_end = max(
            segment_start,
            min(clip_end, offset + clean_number(raw_segment.get("end"))),
        )
        words: list[dict[str, Any]] = []
        for raw_word in raw_segment.get("words") or []:
            text = str(raw_word.get("word", "")).strip()
            if not text:
                continue
            start = max(
                clip_start,
                min(clip_end, offset + clean_number(raw_word.get("start"))),
            )
            end = max(
                start,
                min(clip_end, offset + clean_number(raw_word.get("end"))),
            )
            item = {
                "word": text,
                "start": start,
                "end": end,
                "probability": clean_number(raw_word.get("probability"), 0.5),
            }
            words.append(item)
            all_words.append(item)
        segments.append(
            {
                "start": segment_start,
                "end": segment_end,
                "text": str(raw_segment.get("text", "")).strip(),
                "avg_logprob": clean_number(raw_segment.get("avg_logprob"), -1.0),
                "no_speech_prob": clean_number(raw_segment.get("no_speech_prob"), 0.0),
                "words": words,
            }
        )

    # Whisper occasionally returns text without word timings for a tiny atom.
    # Keep it usable for source grounding by assigning conservative, uniformly
    # spaced pseudo-word anchors. The renderer protects the entire selected
    # word envelope and only moves boundaries outward from these anchors.
    text = str(result.get("text", "")).strip()
    if text and not all_words:
        raw_words = re.findall(r"\S+", text)
        usable_start = clip_start
        usable_end = max(usable_start + 0.001, clip_end)
        step = (usable_end - usable_start) / max(1, len(raw_words))
        for index, word in enumerate(raw_words):
            all_words.append(
                {
                    "word": word,
                    "start": usable_start + index * step,
                    "end": usable_start + (index + 1) * step,
                    "probability": 0.25,
                    "approximate": True,
                }
            )
    return {
        "text": text,
        "segments": segments,
        "words": all_words,
        "language": str(result.get("language", "en")),
    }


def run_asr(
    waveform: np.ndarray,
    *,
    model: str,
    language: str,
    word_timestamps: bool,
    prompt: str | None,
    engine: str = "mlx-whisper",
) -> dict[str, Any]:
    if engine == "crisperwhisper":
        if prompt:
            raise ValueError("CrisperWhisper does not take a decoding prompt")
        return run_crisperwhisper(waveform, model=model, language=language)
    if engine == "faster-whisper":
        return run_faster_whisper(
            waveform,
            model=model,
            language=language,
            word_timestamps=word_timestamps,
            prompt=prompt,
        )
    # Keep the optional MLX runtime out of module import so metadata and
    # checkpoint helpers can be tested in a regular Python environment.
    from mlx_whisper import transcribe as mlx_transcribe

    return mlx_transcribe(
        waveform,
        path_or_hf_repo=model,
        language=language,
        temperature=0.0,
        condition_on_previous_text=False,
        word_timestamps=word_timestamps,
        initial_prompt=prompt,
        # None disables mlx-whisper's per-call tqdm.  The pipeline emits one
        # useful aggregate progress line every N atoms instead.
        verbose=None,
        hallucination_silence_threshold=0.5,
    )


def _load_pip_cuda_libraries() -> None:
    """Make cuBLAS and cuDNN from the ``nvidia-*`` wheels findable, if installed.

    CTranslate2 opens them by name.  On Linux, loading them first lets the
    dynamic linker find them without ``LD_LIBRARY_PATH``; on Windows their
    folders join the DLL search path.
    """

    import ctypes
    import importlib

    for package in ("nvidia.cublas", "nvidia.cudnn"):
        with contextlib.suppress(ImportError, OSError):
            root = Path(importlib.import_module(package).__path__[0])
            if sys.platform == "win32":
                folder = root / "bin"
                os.environ["PATH"] = f"{folder}{os.pathsep}{os.environ.get('PATH', '')}"
                os.add_dll_directory(str(folder))
            else:
                for library in sorted((root / "lib").glob("lib*.so*")):
                    ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)


@functools.lru_cache(maxsize=1)
def _faster_whisper_model(model: str) -> Any:
    """The model on an NVIDIA GPU when CUDA really works there, else the CPU.

    The run's log says which, and why not the GPU.
    """

    from faster_whisper import WhisperModel

    reason = "no NVIDIA GPU with CUDA was found"
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            _load_pip_cuda_libraries()
            gpu = WhisperModel(model, device="cuda", compute_type="float16")
            # cuBLAS and cuDNN load on first use: decode a second of silence.
            segments, _ = gpu.transcribe(np.zeros(SAMPLE_RATE, np.float32))
            list(segments)
            print(
                "Whisper runs on the GPU (CUDA, float16)", file=sys.stderr, flush=True
            )
            return gpu
    except Exception as error:  # noqa: BLE001 - any CUDA failure means the CPU
        reason = f"CUDA failed: {type(error).__name__}: {error}"
    print(f"Whisper runs on the CPU: {reason}", file=sys.stderr, flush=True)
    return WhisperModel(model, device="cpu", compute_type=FASTER_WHISPER_CPU_COMPUTE)


def run_faster_whisper(
    waveform: np.ndarray,
    *,
    model: str,
    language: str,
    word_timestamps: bool,
    prompt: str | None,
) -> dict[str, Any]:
    """Whisper on CTranslate2, decoded like ``mlx_whisper.transcribe`` above."""

    segments, info = _faster_whisper_model(model).transcribe(
        waveform,
        language=language,
        beam_size=1,
        temperature=0.0,
        condition_on_previous_text=False,
        word_timestamps=word_timestamps,
        initial_prompt=prompt,
        hallucination_silence_threshold=0.5,
        vad_filter=False,
    )
    serialized = [
        {
            "start": segment.start,
            "end": segment.end,
            "text": segment.text,
            "avg_logprob": segment.avg_logprob,
            "no_speech_prob": segment.no_speech_prob,
            "words": [
                {
                    "word": word.word,
                    "start": word.start,
                    "end": word.end,
                    "probability": word.probability,
                }
                for word in segment.words or []
            ],
        }
        for segment in segments
    ]
    return {
        "text": "".join(segment["text"] for segment in serialized),
        "segments": serialized,
        "language": info.language,
    }


@functools.lru_cache(maxsize=1)
def _crisperwhisper_model(model: str) -> Any:
    try:
        import torch
        from crisperwhisper import CrisperWhisperModel
    except ImportError as error:
        raise RuntimeError(
            "the crisperwhisper engine needs the crisperwhisper extra in the "
            "ASR environment: .venv-mlx/bin/python -m pip install -e "
            "'.[mlx,crisperwhisper]'"
        ) from error
    if torch.backends.mps.is_available():
        device, compute_type = "mps", "float16"
    else:
        device, compute_type = "cpu", "float32"
    return CrisperWhisperModel(
        model, backend="transformers", device=device, compute_type=compute_type
    )


def run_crisperwhisper(
    waveform: np.ndarray,
    *,
    model: str,
    language: str,
) -> dict[str, Any]:
    """Verbatim transcription in the result shape ``serialize_result`` reads."""

    result = _crisperwhisper_model(model).transcribe(
        waveform,
        sr=SAMPLE_RATE,
        language=language,
        mode="verbatim",
        word_timestamps=True,
    )
    words = [
        {"word": word.word, "start": word.start, "end": word.end}
        for word in result.words or []
    ]
    return {
        "text": result.text,
        "language": result.language,
        "segments": [
            {
                "start": 0.0,
                "end": len(waveform) / SAMPLE_RATE,
                "text": result.text,
                "words": words,
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--analysis", type=Path)
    parser.add_argument(
        "--mode",
        choices=("source", "file"),
        default="source",
        help="'source' transcribes the whole recording and every analysis atom.",
    )
    parser.add_argument(
        "--engine", choices=tuple(DEFAULT_MODELS), default=default_whisper_engine()
    )
    parser.add_argument("--model", help="default: the engine's standard model")
    parser.add_argument("--language", default="en")
    parser.add_argument("--prompt")
    parser.add_argument("--artifact-role", choices=ARTIFACT_ROLES)
    parser.add_argument("--skip-whole", action="store_true")
    parser.add_argument("--max-atoms", type=int)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue from the atom checkpoint beside --output.",
    )
    args = parser.parse_args()
    args.model = args.model or DEFAULT_MODELS[args.engine]

    decode_config = mlx_decode_config(
        mode=args.mode,
        skip_whole=args.skip_whole,
        max_atoms=args.max_atoms,
    )
    provenance = build_provenance(
        args.audio,
        artifact_role=args.artifact_role,
        engine=args.engine,
        model=args.model,
        language=args.language,
        prompt=args.prompt,
        decode_config=decode_config,
        analysis=args.analysis,
    )
    analysis_sha256 = sha256_file(args.analysis) if args.analysis else None
    expected_checkpoint = checkpoint_identity(
        provenance,
        mode=args.mode,
        analysis_sha256=analysis_sha256,
    )
    analysis = read_json(args.analysis) if args.analysis else None
    waveform = load_audio(args.audio)
    duration = len(waveform) / float(SAMPLE_RATE)
    started = time.time()

    partial_path = args.output.with_name(args.output.name + ".partial")
    whole: dict[str, Any] | None = None
    atom_results: list[dict[str, Any]] = []
    if args.resume and partial_path.exists():
        checkpoint = read_json(partial_path)
        validate_checkpoint_identity(checkpoint, expected_checkpoint)
        whole = checkpoint.get("whole")
        atom_results = list(checkpoint.get("atoms", []))

    def save_checkpoint() -> None:
        write_json(
            partial_path,
            {
                "schema_version": 1,
                "complete": False,
                **expected_checkpoint,
                "duration": duration,
                "whole": whole,
                "atoms": atom_results,
            },
        )

    if not args.skip_whole and whole is None:
        raw_whole = run_asr(
            waveform,
            model=args.model,
            language=args.language,
            word_timestamps=True,
            prompt=args.prompt,
            engine=args.engine,
        )
        whole = serialize_result(
            raw_whole,
            offset=0.0,
            clip_start=0.0,
            clip_end=duration,
        )
        save_checkpoint()

    if args.mode == "source":
        if analysis is None:
            parser.error("--analysis is required in source mode")
        atoms = list(analysis.get("atoms", []))
        if args.max_atoms is not None:
            atoms = atoms[: args.max_atoms]
        completed_atoms = {int(item["atom_index"]): item for item in atom_results}
        atom_results = []
        for position, atom in enumerate(atoms, 1):
            atom_index = int(atom["atom_index"])
            if atom_index in completed_atoms:
                atom_results.append(completed_atoms[atom_index])
                _report_progress(position, len(atoms))
                continue
            start = float(atom["start"])
            end = float(atom["end"])
            first = max(0, round(start * SAMPLE_RATE))
            last = min(len(waveform), round(end * SAMPLE_RATE))
            clip = waveform[first:last]
            if len(clip) < 800:
                serialized = {
                    "text": "",
                    "segments": [],
                    "words": [],
                    "language": args.language,
                }
            else:
                raw = run_asr(
                    clip,
                    model=args.model,
                    language=args.language,
                    word_timestamps=True,
                    prompt=args.prompt,
                    engine=args.engine,
                )
                serialized = serialize_result(
                    raw,
                    offset=start,
                    clip_start=start,
                    clip_end=end,
                )
            segment_scores = [
                float(segment["avg_logprob"])
                for segment in serialized["segments"]
                if math.isfinite(float(segment["avg_logprob"]))
            ]
            atom_results.append(
                {
                    "atom_index": int(atom["atom_index"]),
                    "start_sample": int(atom["start_sample"]),
                    "end_sample": int(atom["end_sample"]),
                    "start": start,
                    "end": end,
                    "duration": end - start,
                    "decode_strategy": SOURCE_ATOM_STRATEGY,
                    "text": serialized["text"],
                    "segments": serialized["segments"],
                    "words": serialized["words"],
                    "mean_logprob": (
                        sum(segment_scores) / len(segment_scores)
                        if segment_scores
                        else -10.0
                    ),
                }
            )
            _report_progress(position, len(atoms))
            if args.progress_every and (
                position % args.progress_every == 0 or position == len(atoms)
            ):
                save_checkpoint()
                elapsed = time.time() - started
                print(
                    f"transcribed {position}/{len(atoms)} atoms in {elapsed:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )

    result = {
        "schema_version": 2,
        **provenance,
        "duration": duration,
        "condition_on_previous_text": False,
        "temperature": 0.0,
        "preferred_evidence": "atoms" if args.mode == "source" else "whole",
        "analysis_atom_count": (
            len(list(analysis.get("atoms", [])))
            if args.mode == "source" and isinstance(analysis, dict)
            else 0
        ),
        "decoded_atom_count": len(atom_results),
        "whole": whole,
        "atoms": atom_results,
        "elapsed_seconds": time.time() - started,
    }
    write_json(args.output, result)
    if partial_path.exists():
        partial_path.unlink()
    print(
        json.dumps(
            {
                "duration": duration,
                "model": args.model,
                "whole_words": len(whole["words"]) if whole else 0,
                "atoms": len(atom_results),
                "atom_words": sum(len(atom["words"]) for atom in atom_results),
                "elapsed_seconds": result["elapsed_seconds"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
