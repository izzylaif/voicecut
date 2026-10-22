"""Run VoiceCut's pipeline inside the runtime the app installed.

    <runtime python> desktop/tests/pipeline_smoke.py INPUT WORK_DIR [--full]

Without ``--full`` it needs no API key: it runs every stage before the take
planner (media input, analysis, Whisper, CTC alignment, gap recovery), then
aligns the first transcribed words with MFA the way the cut renderer does.
With ``--full`` (``GEMINI_API_KEY`` set) the whole pipeline runs and
publishes the edit.  Prints a JSON summary; exits 1 when anything failed.
Run it with the working directory and PATH of the app's engine.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from voicecut import full_pipeline
from voicecut.mfa_alignment import (
    MFAContextSpec,
    MFASourceWord,
    align_mfa_contexts,
    source_word_alignment,
)
from voicecut.planner_backends import PlannerRuntimeConfiguration

MFA_WORDS = 12


class PlannerReached(Exception):
    """The pipeline got as far as the take planner."""


def run_pipeline(input_path: Path, work_dir: Path, *, full: bool) -> dict[str, Any]:
    stages: dict[str, float] = {}
    started: dict[str, float] = {}

    def progress(event: dict[str, Any]) -> None:
        stage, status = event.get("stage"), event.get("status")
        if status == "started":
            started[stage] = time.monotonic()
        elif status in {"completed", "skipped", "cached"}:
            elapsed = time.monotonic() - started.get(stage, time.monotonic())
            stages[stage] = round(max(0.0, elapsed), 1)

    def runner(command: list[str], *args: Any, **kwargs: Any) -> Any:
        if not full and "voicecut.take_planner" in command:
            raise PlannerReached
        return subprocess.run(command, *args, **kwargs)  # noqa: PLW1510 - check= in kwargs

    def no_planner(**options: Any) -> PlannerRuntimeConfiguration:
        return PlannerRuntimeConfiguration(
            provider=options["provider"], base_url=options["base_url"], api_key_env=None
        )

    output = work_dir.parent / f"{input_path.stem}_edited{input_path.suffix}"
    args = full_pipeline.build_parser().parse_args(
        [str(input_path), "--work-dir", str(work_dir), "--output", str(output)]
    )
    options: dict[str, Any] = {"runner": runner, "progress_callback": progress}
    if not full:
        options["planner_preflight"] = no_planner
    try:
        full_pipeline.run_full_pipeline(args, **options)
    except PlannerReached:
        pass
    summary: dict[str, Any] = {
        "whisper_engine": args.whisper_engine,
        "whisper_model": args.whisper_model,
        "stage_seconds": stages,
    }
    if full:
        summary["output"] = str(output)
        summary["output_bytes"] = output.stat().st_size
    return summary


def transcript_words(work_dir: Path) -> list[dict[str, Any]]:
    path = work_dir / "02_transcription" / "source_transcript.json"
    transcript = json.loads(path.read_text(encoding="utf-8"))
    return [
        word
        for atom in transcript["atoms"]
        for segment in atom.get("segments", [])
        for word in segment.get("words", [])
        if str(word.get("word", "")).strip()
    ]


def align_with_mfa(audio: Path, words: list[dict[str, Any]], work_dir: Path) -> dict:
    words = words[:MFA_WORDS]
    source = tuple(
        MFASourceWord(
            word_id=index,
            text=str(word["word"]).strip(),
            start_seconds=float(word["start"]),
            end_seconds=float(word["end"]),
            selected=True,
        )
        for index, word in enumerate(words)
    )
    context = MFAContextSpec(
        context_id="context_000",
        crop_source_start_seconds=max(0.0, source[0].start_seconds - 0.3),
        crop_source_end_seconds=source[-1].end_seconds + 0.3,
        words=source,
        boundary_ids=("smoke",),
    )
    started = time.monotonic()
    result = align_mfa_contexts(
        audio_path=audio,
        contexts=(context,),
        work_dir=work_dir / "mfa_smoke",
        micromamba=shutil.which("micromamba") or "micromamba",
    )
    aligned = []
    for word in source:
        alignment = source_word_alignment(result["contexts"][0], word.word_id)
        phones = [phone for phone in alignment["phones"] if not phone["is_silence"]]
        if not phones:
            raise RuntimeError(f"MFA aligned no phones to {word.text!r}")
        aligned.append(word.text)
    return {
        "seconds": round(time.monotonic() - started, 1),
        "model": result["model_id"],
        "aligned_words": len(aligned),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("input", type=Path)
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    summary: dict[str, Any] = {"python": sys.version.split()[0], "ok": False}
    try:
        work_dir = args.work_dir.absolute()
        summary.update(run_pipeline(args.input.absolute(), work_dir, full=args.full))
        words = transcript_words(work_dir)
        summary["words"] = len(words)
        summary["text"] = " ".join(str(word["word"]).strip() for word in words[:40])
        if len(words) < MFA_WORDS:
            raise RuntimeError(f"Whisper found only {len(words)} words")
        if not args.full:
            manifest = work_dir / "00_media" / "media_input.json"
            audio = Path(json.loads(manifest.read_text())["canonical_audio"])
            summary["mfa"] = align_with_mfa(audio, words, work_dir)
        summary["ok"] = True
    except Exception as error:  # noqa: BLE001 - reported to the test driver
        summary["error"] = f"{type(error).__name__}: {error}"
        summary["traceback"] = traceback.format_exc()[-4000:]
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
