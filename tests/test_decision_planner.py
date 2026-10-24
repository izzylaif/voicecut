from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from voicecut import decision_models, decision_planner
from voicecut.common import read_json, sha256_file, write_json
from voicecut.decision_models import Protocol
from voicecut.full_pipeline import run_full_pipeline
from voicecut.planner_backends import (
    OPENROUTER_BASE_URL,
    PlannerRuntimeConfiguration,
    preflight_planner_backend,
)
from voicecut.take_planner import build_lines, load_transcript_words

from .test_full_pipeline import _args, _fake_gap_recovery, _write_fake_ctc_enrichment


def _transcript(tmp_path: Path) -> Path:
    """Three attempts at one sentence, a stray remark, then the next sentence."""

    words = []
    clock = 0.0
    for line in (
        "Here is the paradox.",
        "Here is a paradox.",
        "Here is the paradox.",
        "okay",
        "A training pipeline can keep every GPU busy.",
    ):
        for text in line.split():
            words.append({"word": text, "start": clock, "end": clock + 0.3})
            clock += 0.35
        clock += 1.0
    path = tmp_path / "transcript.json"
    write_json(path, {"whole": {"words": words}})
    return path


class FakeEngine:
    """Says the first two lines are retaken by the third, the fourth is junk."""

    cost = 0.0

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    def answer(self, state: str, questions: dict) -> dict:
        self.requests.append((state, questions))
        answers = {}
        for qid, question in questions.items():
            line, kind = qid.split(":")
            if kind == "retake":
                choice = "L3" if line in ("0", "1") else "none"
                answers[qid] = {
                    name: float(name == choice) for name in question["criteria"]
                }
            elif kind == "junk":
                answers[qid] = (
                    {"false": 0.1, "true": 0.9}
                    if line == "3"
                    else {"false": 0.9, "true": 0.1}
                )
            else:
                answers[qid] = (
                    {"false": 0.9, "true": 0.1}
                    if line == "3"
                    else {"false": 0.1, "true": 0.9}
                )
        return answers


def test_requests_mark_the_block_and_offer_later_lines_by_label(tmp_path: Path) -> None:
    words = load_transcript_words(read_json(_transcript(tmp_path)))
    lines = build_lines(words)
    requests = decision_planner.build_requests(
        words,
        lines,
        Protocol(block_lines=2, ahead_seconds=30.0, options=2, side_question="junk"),
    )
    assert len(requests) == 3
    first = requests[0]
    state = first["state"]
    assert ">> L1: Here is the paradox." in state
    assert re.search(r">> L2 \(\+1\.\ds\): Here is a paradox\.", state)
    assert state.index("--- LATER") < state.index("\nL3 (+")
    retake = first["questions"]["0:retake"]
    assert retake["type"] == "choice"
    # "none" first, then the next lines by label only (their text is in the state)
    assert list(retake["criteria"]) == ["none", "L2", "L3"]
    assert retake["criteria"]["L2"] is None
    assert first["questions"]["0:junk"]["type"] == "noul"
    assert "narration" not in json.dumps(first["questions"])


def test_lines_said_again_or_not_narration_are_cut(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    engine = FakeEngine()
    plan = decision_planner.run_decision_planner(
        transcript_path=transcript,
        output_dir=tmp_path / "plan",
        backend="ollama",
        model="jaredpalmer/kev-4b",
        engine=engine,
    )
    assert plan["planner"] == decision_planner.DECISION_PLANNER_NAME
    assert plan["status"] == "complete"
    assert plan["backend"] == "ollama" and plan["model"] == "jaredpalmer/kev-4b"
    assert plan["listened_to_audio"] is False
    assert plan["transcript_sha256"] == sha256_file(transcript)
    kept = [item["text"] for item in plan["items"]]
    # lines 3 and 5 survive; they are not contiguous (line 4 is cut), so two items
    assert kept == [
        "Here is the paradox.",
        "A training pipeline can keep every GPU busy.",
    ]
    assert [item["break_after"] for item in plan["items"]] == ["sentence", "sentence"]
    assert [d["keep"] for d in plan["decisions"]] == [False, False, True, False, True]
    assert read_json(tmp_path / "plan" / "take_plan.json") == json.loads(
        json.dumps(plan)
    )
    # a rerun reads every block from the call cache
    again = FakeEngine()
    decision_planner.run_decision_planner(
        transcript_path=transcript,
        output_dir=tmp_path / "plan",
        backend="ollama",
        model="jaredpalmer/kev-4b",
        engine=again,
    )
    assert again.requests == []


def test_each_model_has_its_protocol() -> None:
    assert (
        decision_models.find("ollama", "jaredpalmer/kev-9b") is decision_models.KEV_9B
    )
    assert (
        decision_models.find("openrouter", "typesafe/jev-1.14") is decision_models.JEV
    )
    assert decision_models.find("ollama", "gemma4:31b-it-qat") is None
    assert decision_models.find("openrouter", "jaredpalmer/kev-9b") is None
    assert decision_models.KEV_9B.protocol.side_question == "narration"
    assert decision_models.KEV_4B.protocol.side_question == "junk"
    assert (
        decision_models.JEV.protocol.reworded
        and decision_models.JEV.protocol.options == 40
    )


def test_jev_requests_stay_under_the_long_context_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = decision_planner.JevEngine(decision_models.JEV, "key")
    calls = []

    def fake_call(state: str, questions: dict) -> dict:
        size = len(state.encode()) + sum(
            len(json.dumps(q).encode()) for q in questions.values()
        )
        calls.append(size)
        return {
            "usage": {"cost": 0.001},
            "answers": {
                qid: {"type": "noul", "noul": 0.25}
                if q["type"] == "noul"
                else {
                    "type": "choice",
                    "probabilities": {
                        name: 1 / len(q["criteria"]) for name in q["criteria"]
                    },
                }
                for qid, q in questions.items()
            },
        }

    monkeypatch.setattr(engine, "_call", fake_call)
    questions = {
        f"{index}:retake": {
            "type": "choice",
            "instructions": "x" * 400,
            "criteria": {"none": "no", "L2": None},
        }
        for index in range(200)
    }
    questions.update(
        {
            f"{index}:junk": {"type": "noul", "instructions": "y" * 300}
            for index in range(200)
        }
    )
    answers = engine.answer("state " * 1000, questions)
    assert len(answers) == 400
    assert len(calls) > 1 and max(calls) <= decision_planner.MAX_REQUEST_BYTES
    assert answers["0:junk"] == {"false": 0.75, "true": 0.25}
    assert engine.cost == pytest.approx(0.001 * len(calls))


def test_preflight_wants_a_local_decision_model_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(decision_models, "runs_here", lambda model: True)
    monkeypatch.setattr(decision_models, "weights_ready", lambda model: False)
    with pytest.raises(RuntimeError, match="Kev 4B is not downloaded"):
        preflight_planner_backend(
            provider="ollama",
            env_file=tmp_path / ".env",
            base_url=None,
            api_key_env=None,
            model="jaredpalmer/kev-4b",
        )
    monkeypatch.setattr(decision_models, "weights_ready", lambda model: True)
    configuration = preflight_planner_backend(
        provider="ollama",
        env_file=tmp_path / ".env",
        base_url=None,
        api_key_env=None,
        model="jaredpalmer/kev-4b",
    )
    assert configuration.provider == "ollama"
    monkeypatch.setattr(decision_models, "runs_here", lambda model: False)
    with pytest.raises(RuntimeError, match="Apple Silicon"):
        preflight_planner_backend(
            provider="ollama",
            env_file=tmp_path / ".env",
            base_url=None,
            api_key_env=None,
            model="jaredpalmer/kev-4b",
        )


@pytest.mark.parametrize(
    ("backend", "model", "local"),
    [
        ("ollama", "jaredpalmer/kev-9b", True),
        ("openrouter", "typesafe/jev-1.14", False),
    ],
)
def test_the_pipeline_plans_a_decision_model_with_the_decision_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    model: str,
    local: bool,
) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source media")
    args = _args(tmp_path, source)
    args.planner_backend, args.planner_model = backend, model
    monkeypatch.setenv("OPENROUTER_API_KEY", "selected-secret")
    asr_python = tmp_path / "mlx-python"
    asr_python.write_text("#!/bin/sh\n", encoding="utf-8")
    args.asr_python = asr_python
    plan_commands: list[list[str]] = []

    def fake_media_preparer(input_path: Path, output_dir: Path):
        output_dir.mkdir(parents=True)
        canonical = output_dir / "source_audio.wav"
        canonical.write_bytes(b"canonical audio")
        manifest = {
            "status": "complete",
            "source_media": str(input_path.resolve()),
            "source_media_sha256": sha256_file(input_path),
            "source_kind": "audio",
            "canonical_audio": str(canonical.resolve()),
            "canonical_audio_sha256": sha256_file(canonical),
        }
        write_json(output_dir / "media_input.json", manifest)
        return manifest

    def fake_audio_publisher(
        final_wav: Path, output_path: Path, *, manifest_path: Path, overwrite: bool
    ):
        output_path.write_bytes(b"published audio")
        return {"status": "complete", "output_audio": str(output_path)}

    def fake_runner(command: list[str], **kwargs: object) -> None:
        module = command[command.index("-m") + 1]
        audio = args.work_dir / "00_media/source_audio.wav"
        audio_sha = sha256_file(audio)
        if module == "voicecut.analyze":
            write_json(
                Path(command[command.index("--output-dir") + 1]) / "analysis.json",
                {"audio_sha256": audio_sha},
            )
        elif module == "voicecut.transcribe_mlx":
            write_json(
                Path(command[command.index("--output") + 1]),
                {"audio_sha256": audio_sha},
            )
        elif module == "voicecut.ctc_enrich":
            _write_fake_ctc_enrichment(
                Path(command[command.index("--output-dir") + 1]), audio_sha256=audio_sha
            )
        elif module == "voicecut.gap_recovery":
            _fake_gap_recovery(command)
        elif module == "voicecut.decision_planner":
            plan_commands.append(command)
            environment = kwargs["env"]  # type: ignore[assignment]
            assert ("OPENROUTER_API_KEY" in environment) is (not local)  # type: ignore[operator]
            transcript = Path(command[command.index("--transcript") + 1])
            write_json(
                Path(command[command.index("--output-dir") + 1]) / "take_plan.json",
                {
                    "status": "complete",
                    "planner": decision_planner.DECISION_PLANNER_NAME,
                    "listened_to_audio": False,
                    "backend": backend,
                    "model": model,
                    "transcript": str(transcript.resolve()),
                    "transcript_sha256": sha256_file(transcript),
                },
            )
        elif module == "voicecut.cut_render":
            output_dir = Path(command[command.index("--output-dir") + 1])
            output_dir.mkdir(parents=True)
            final_cut = output_dir / "final_cut.wav"
            final_cut.write_bytes(b"rendered audio")
            boundary_plan = output_dir / "final_boundary_plan.json"
            write_json(boundary_plan, {"status": "safe"})
            plan = Path(command[command.index("--plan") + 1])
            write_json(
                output_dir / "final_render_manifest.json",
                {
                    "status": "complete",
                    "renderer": "cut_render_v1",
                    "alignment_backend": "mfa",
                    "mfa_version": "3.4.1",
                    "mfa_model": "english_us_arpa",
                    "pause_policy": "semantic",
                    "breath_cleanup_mode": "replace",
                    "breath_threshold": 0.5,
                    "source_audio_sha256": audio_sha,
                    "take_plan": str(plan.resolve()),
                    "take_plan_sha256": sha256_file(plan),
                    "final_cut_wav": str(final_cut.resolve()),
                    "final_cut_wav_sha256": sha256_file(final_cut),
                    "final_boundary_plan": str(boundary_plan.resolve()),
                    "final_boundary_plan_sha256": sha256_file(boundary_plan),
                    "duration_seconds": 1.25,
                },
            )
        else:
            raise AssertionError(command)

    def fake_preflight(**_: object) -> PlannerRuntimeConfiguration:
        return PlannerRuntimeConfiguration(
            provider=backend,
            base_url=None if local else OPENROUTER_BASE_URL,
            api_key_env=None if local else "OPENROUTER_API_KEY",
        )

    created = run_full_pipeline(
        args,
        runner=fake_runner,
        media_preparer=fake_media_preparer,
        audio_publisher=fake_audio_publisher,
        planner_preflight=fake_preflight,
    )
    assert created["stages"]["semantic_plan"] == "created"
    (command,) = plan_commands
    # Kev runs in the MLX environment, Jev in the pipeline's own Python.
    assert command[0] == (str(asr_python.absolute()) if local else sys.executable)
    assert command[command.index("--planner-model") + 1] == model
    assert "--planner-think" not in command
