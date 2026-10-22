from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from voicecut.common import read_json, sha256_file, write_json
from voicecut.full_pipeline import (
    FullPipelineError,
    _implementation_fingerprint,
    _run,
    _subprocess_environment,
    _WorkDirectoryLock,
    build_parser,
    run_full_pipeline,
)
from voicecut.planner_backends import PlannerRuntimeConfiguration


def _args(tmp_path: Path, source: Path):
    mfa_prefix = tmp_path / ".mfa-env"
    mfa_executable = (
        mfa_prefix / "Scripts" / "mfa.exe"
        if sys.platform == "win32"
        else mfa_prefix / "bin" / "mfa"
    )
    mfa_executable.parent.mkdir(parents=True, exist_ok=True)
    mfa_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    mfa_executable.chmod(0o755)
    micromamba = tmp_path / "micromamba"
    micromamba.write_text("#!/bin/sh\n", encoding="utf-8")
    micromamba.chmod(0o755)
    return build_parser().parse_args(
        [
            str(source),
            "--output",
            str(tmp_path / "edited.mp3"),
            "--work-dir",
            str(tmp_path / "work"),
            "--asr-python",
            sys.executable,
            "--alignment-backend",
            "mfa",
            "--mfa-prefix",
            str(mfa_prefix),
            "--mfa-cache-root",
            str(tmp_path / "mfa-cache"),
            "--mfa-micromamba",
            str(micromamba),
            "--mfa-num-jobs",
            "2",
            "--planner-backend",
            "gemini",
            "--planner-model",
            "gemini-3.6-flash",
        ]
    )


def _fake_gemini_preflight(**_: object) -> PlannerRuntimeConfiguration:
    return PlannerRuntimeConfiguration(
        provider="gemini",
        base_url=None,
        api_key_env="GEMINI_API_KEY",
    )


def _write_fake_ctc_enrichment(output_dir: Path, *, audio_sha256: str) -> None:
    report = {
        "schema_version": 1,
        "status": "complete",
        "atoms_examined": 0,
        "atoms_expanded": 0,
        "atoms_skipped_no_lexical_content": 0,
        "atoms_with_decode_failures": 0,
        "hallucinated_suffixes_pruned": 0,
        "hidden_retries_recovered": 0,
        "recovered": [],
        "pruned_suffixes": [],
        "skipped_atoms": [],
        "decode_failures": [],
    }
    write_json(
        output_dir / "source_transcript_ctc_enriched.json",
        {
            "audio_sha256": audio_sha256,
            "source_decode_strategy": (
                "whisper_primary_plus_gated_raw_ctc_insertions_v2"
            ),
            "ctc_enrichment": report,
        },
    )
    write_json(output_dir / "ctc_enrichment_report.json", report)


def _fake_gap_recovery(command: list[str]) -> None:
    transcript_path = Path(command[command.index("--transcript") + 1])
    output = Path(command[command.index("--output") + 1])
    transcript = read_json(transcript_path)
    transcript["gap_recovery"] = {
        "status": "complete",
        "strategy": "uncovered_speech_retranscription_v1",
        "model": command[command.index("--model") + 1],
        "input_transcript_sha256": sha256_file(transcript_path),
        "regions": [],
        "recovered_words": 0,
    }
    write_json(output, transcript)


def test_full_pipeline_runs_every_stage_and_then_uses_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source media")
    args = _args(tmp_path, source)
    calls: list[list[str]] = []
    publication_calls = 0
    transcription_checkpoint = (
        args.work_dir / "02_transcription/source_transcript.json.partial"
    )
    transcription_checkpoint.parent.mkdir(parents=True, exist_ok=True)
    transcription_checkpoint_value = {
        "schema_version": 1,
        "checkpoint_kind": "transcription",
        "completed_atoms": [0],
    }
    write_json(transcription_checkpoint, transcription_checkpoint_value)
    (args.work_dir / "02_transcription/source_transcript.json").write_text(
        '{"audio_sha256":',
        encoding="utf-8",
    )
    ctc_checkpoint = args.work_dir / "03_ctc_enrichment/ctc_alignment.json.partial"
    ctc_checkpoint.parent.mkdir(parents=True, exist_ok=True)
    ctc_checkpoint_value = {
        "schema_version": 1,
        "checkpoint_kind": "ctc_alignment",
        "completed_atoms": [0],
    }
    write_json(ctc_checkpoint, ctc_checkpoint_value)
    (
        args.work_dir / "03_ctc_enrichment/source_transcript_ctc_enriched.json"
    ).write_text('{"audio_sha256":', encoding="utf-8")
    (args.work_dir / "03_ctc_enrichment/ctc_enrichment_report.json").write_text(
        '{"status":',
        encoding="utf-8",
    )
    monkeypatch.setenv("GEMINI_API_KEY", "selected-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unrelated-secret")

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
        final_wav: Path,
        output_path: Path,
        *,
        manifest_path: Path,
        overwrite: bool,
    ):
        nonlocal publication_calls
        publication_calls += 1
        assert overwrite is False
        assert manifest_path.name == "audio_publish_manifest.json"
        output_path.write_bytes(b"published audio")
        return {
            "status": "complete",
            "source_wav": str(final_wav),
            "output_audio": str(output_path),
        }

    def fake_runner(command: list[str], **kwargs: object) -> None:
        calls.append(command)
        environment = kwargs["env"]  # type: ignore[assignment]
        assert str(Path(__file__).resolve().parents[1] / "src") in str(
            environment["PYTHONPATH"]  # type: ignore[index]
        )
        module = command[command.index("-m") + 1]
        if module == "voicecut.take_planner":
            assert environment["GEMINI_API_KEY"] == "selected-secret"  # type: ignore[index]
        else:
            assert "GEMINI_API_KEY" not in environment
        assert "OPENAI_API_KEY" not in environment
        assert "DEEPSEEK_API_KEY" not in environment
        audio = args.work_dir / "00_media/source_audio.wav"
        audio_sha = sha256_file(audio)
        if module == "voicecut.analyze":
            output_dir = Path(command[command.index("--output-dir") + 1])
            write_json(output_dir / "analysis.json", {"audio_sha256": audio_sha})
        elif module == "voicecut.transcribe_mlx":
            assert "--resume" in command
            assert read_json(transcription_checkpoint) == transcription_checkpoint_value
            write_json(
                Path(command[command.index("--output") + 1]),
                {"audio_sha256": audio_sha},
            )
        elif module == "voicecut.ctc_enrich":
            assert "--resume" in command
            assert read_json(ctc_checkpoint) == ctc_checkpoint_value
            output_dir = Path(command[command.index("--output-dir") + 1])
            _write_fake_ctc_enrichment(output_dir, audio_sha256=audio_sha)
        elif module == "voicecut.gap_recovery":
            _fake_gap_recovery(command)
        elif module == "voicecut.take_planner":
            output_dir = Path(command[command.index("--output-dir") + 1])
            transcript = Path(command[command.index("--transcript") + 1])
            write_json(
                output_dir / "take_plan.json",
                {
                    "status": "complete",
                    "planner": "take_planner_v1",
                    "listened_to_audio": True,
                    "backend": "gemini",
                    "model": "gemini-3.6-flash",
                    "transcript": str(transcript.resolve()),
                    "transcript_sha256": sha256_file(transcript),
                },
            )
        elif module == "voicecut.cut_render":
            assert command[command.index("--mfa-prefix") + 1] == str(
                args.mfa_prefix.absolute()
            )
            assert command[command.index("--mfa-cache-root") + 1] == str(
                args.mfa_cache_root.absolute()
            )
            assert command[command.index("--mfa-micromamba") + 1] == str(
                args.mfa_micromamba.absolute()
            )
            assert command[command.index("--mfa-num-jobs") + 1] == "2"
            assert command[command.index("--breath-cleanup") + 1] == "replace"
            assert command[command.index("--breath-threshold") + 1] == "0.5"
            assert command[command.index("--pause-policy") + 1] == "semantic"
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

    created = run_full_pipeline(
        args,
        runner=fake_runner,
        media_preparer=fake_media_preparer,
        audio_publisher=fake_audio_publisher,
        planner_preflight=_fake_gemini_preflight,
    )
    assert len(calls) == 6
    assert publication_calls == 1
    assert read_json(transcription_checkpoint) == transcription_checkpoint_value
    assert read_json(ctc_checkpoint) == ctc_checkpoint_value
    assert created["stages"] == {
        "media_input": "created",
        "analysis": "created",
        "transcription": "created",
        "hidden_retry_recovery": "created",
        "gap_recovery": "created",
        "semantic_plan": "created",
        "final_render": "created",
        "publication": "created",
    }
    transcription_command = next(
        command for command in calls if "voicecut.transcribe_mlx" in command
    )
    assert transcription_command[0] == str(Path(sys.executable).absolute())
    configuration = read_json(args.work_dir / "pipeline_config.json")
    assert configuration["alignment_backend"] == "mfa"
    assert configuration["mfa_prefix"] == str(args.mfa_prefix.absolute())
    assert configuration["mfa_cache_root"] == str(args.mfa_cache_root.absolute())
    assert configuration["mfa_micromamba"] == str(args.mfa_micromamba.absolute())
    assert configuration["mfa_num_jobs"] == 2
    assert configuration["breath_cleanup"] == "replace"
    assert configuration["breath_threshold"] == 0.5
    assert configuration["planner"] == "take_planner_v1"
    assert configuration["planner_listens_to_audio"] is True
    assert configuration["renderer"] == "cut_render_v1"
    assert created["output"] == str((tmp_path / "edited.mp3").absolute())

    def unexpected_runner(*_: object, **__: object) -> None:
        raise AssertionError("a cached rerun must not launch any stage")

    def unexpected_media(*_: object, **__: object):
        raise AssertionError("cached media must not be decoded")

    # A matching enriched transcript without its report is not a complete CTC
    # cache entry: the absent report could conceal degraded passthrough status.
    # Rerun only CTC, retain its validated checkpoint, then reuse downstream
    # stages because the regenerated enriched transcript is byte-identical.
    calls.clear()
    (args.work_dir / "03_ctc_enrichment/ctc_enrichment_report.json").unlink()
    resumed = run_full_pipeline(
        args,
        runner=fake_runner,
        media_preparer=unexpected_media,
        audio_publisher=unexpected_media,
        planner_preflight=_fake_gemini_preflight,
    )
    assert [command[command.index("-m") + 1] for command in calls] == [
        "voicecut.ctc_enrich"
    ]
    assert resumed["stages"]["hidden_retry_recovery"] == "created"
    assert all(
        status == "cached"
        for stage, status in resumed["stages"].items()
        if stage != "hidden_retry_recovery"
    )
    assert read_json(ctc_checkpoint) == ctc_checkpoint_value

    report_path = args.work_dir / "03_ctc_enrichment/ctc_enrichment_report.json"
    mismatched_report = read_json(report_path)
    mismatched_report["status"] = "complete_with_degraded_evidence"
    write_json(report_path, mismatched_report)
    calls.clear()
    cross_checked = run_full_pipeline(
        args,
        runner=fake_runner,
        media_preparer=unexpected_media,
        audio_publisher=unexpected_media,
        planner_preflight=_fake_gemini_preflight,
    )
    assert [command[command.index("-m") + 1] for command in calls] == [
        "voicecut.ctc_enrich"
    ]
    assert cross_checked["ctc_enrichment_status"] == "complete"
    assert read_json(ctc_checkpoint) == ctc_checkpoint_value

    cached = run_full_pipeline(
        args,
        runner=unexpected_runner,
        media_preparer=unexpected_media,
        audio_publisher=unexpected_media,
        planner_preflight=_fake_gemini_preflight,
    )
    assert set(cached["stages"].values()) == {"cached"}
    assert cached["output_sha256"] == created["output_sha256"]


def test_public_parser_is_one_input_to_one_output(tmp_path: Path) -> None:
    source = tmp_path / "voice.m4a"
    parsed = build_parser().parse_args(
        [
            str(source),
            "-o",
            "edited.flac",
            "--planner-backend",
            "ollama",
            "--planner-model",
            "qwen3.6:27b",
            "--planner-think",
            "off",
        ]
    )
    assert parsed.input == source
    assert parsed.output == Path("edited.flac")
    assert parsed.planner_backend == "ollama"
    assert parsed.planner_model == "qwen3.6:27b"
    assert parsed.planner_think == "off"
    assert parsed.planner_audio == "auto"
    assert parsed.alignment_backend == "mfa"
    assert parsed.debug_artifacts is False


def test_russian_profile_bypasses_english_ctc_and_propagates_language(
    tmp_path: Path,
) -> None:
    source = tmp_path / "russian.wav"
    source.write_bytes(b"russian source media")
    args = _args(tmp_path, source)
    args.language = "ru"
    calls: list[list[str]] = []
    russian_mfa_model = (
        "MontrealCorpusTools/russian_mfa@88b81ae3eaf3bd8163bb3f7c43e1ae61478595af"
    )

    def fake_media_preparer(input_path: Path, output_dir: Path):
        output_dir.mkdir(parents=True)
        canonical = output_dir / "source_audio.wav"
        canonical.write_bytes(b"canonical russian audio")
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

    def fake_runner(command: list[str], **_: object) -> None:
        calls.append(command)
        module = command[command.index("-m") + 1]
        audio = args.work_dir / "00_media/source_audio.wav"
        audio_sha = sha256_file(audio)
        if module == "voicecut.analyze":
            output_dir = Path(command[command.index("--output-dir") + 1])
            write_json(output_dir / "analysis.json", {"audio_sha256": audio_sha})
        elif module == "voicecut.transcribe_mlx":
            assert command[command.index("--language") + 1] == "ru"
            write_json(
                Path(command[command.index("--output") + 1]),
                {"audio_sha256": audio_sha, "language": "ru", "atoms": []},
            )
        elif module == "voicecut.gap_recovery":
            _fake_gap_recovery(command)
        elif module == "voicecut.take_planner":
            assert command[command.index("--language") + 1] == "ru"
            transcript = Path(command[command.index("--transcript") + 1])
            assert transcript == (
                args.work_dir
                / "03_gap_recovery/source_transcript_with_recovered_gaps.json"
            )
            output_dir = Path(command[command.index("--output-dir") + 1])
            write_json(
                output_dir / "take_plan.json",
                {
                    "status": "complete",
                    "planner": "take_planner_v1",
                    "listened_to_audio": True,
                    "backend": "gemini",
                    "model": "gemini-3.6-flash",
                    "transcript": str(transcript.resolve()),
                    "transcript_sha256": sha256_file(transcript),
                },
            )
        elif module == "voicecut.cut_render":
            assert command[command.index("--language") + 1] == "ru"
            assert command[command.index("--breath-cleanup") + 1] == "off"
            output_dir = Path(command[command.index("--output-dir") + 1])
            output_dir.mkdir(parents=True)
            final_cut = output_dir / "final_cut.wav"
            final_cut.write_bytes(b"rendered russian audio")
            boundary_plan = output_dir / "final_boundary_plan.json"
            write_json(boundary_plan, {"status": "safe", "language": "ru"})
            plan = Path(command[command.index("--plan") + 1])
            write_json(
                output_dir / "final_render_manifest.json",
                {
                    "status": "complete",
                    "renderer": "cut_render_v1",
                    "alignment_backend": "mfa",
                    "mfa_version": "3.4.1",
                    "mfa_model": russian_mfa_model,
                    "pause_policy": "semantic",
                    "breath_cleanup_mode": "off",
                    "breath_threshold": 0.5,
                    "source_audio_sha256": audio_sha,
                    "take_plan": str(plan.resolve()),
                    "take_plan_sha256": sha256_file(plan),
                    "final_cut_wav": str(final_cut.resolve()),
                    "final_cut_wav_sha256": sha256_file(final_cut),
                    "final_boundary_plan": str(boundary_plan.resolve()),
                    "final_boundary_plan_sha256": sha256_file(boundary_plan),
                    "duration_seconds": 1.0,
                },
            )
        else:
            raise AssertionError(command)

    def fake_audio_publisher(
        final_wav: Path,
        output_path: Path,
        *,
        manifest_path: Path,
        overwrite: bool,
    ) -> dict[str, object]:
        assert final_wav.read_bytes() == b"rendered russian audio"
        assert overwrite is False
        output_path.write_bytes(b"published russian audio")
        write_json(manifest_path, {"status": "complete"})
        return {"status": "complete", "output_audio": str(output_path)}

    result = run_full_pipeline(
        args,
        runner=fake_runner,
        media_preparer=fake_media_preparer,
        audio_publisher=fake_audio_publisher,
        planner_preflight=_fake_gemini_preflight,
    )

    modules = [command[command.index("-m") + 1] for command in calls]
    assert "voicecut.ctc_enrich" not in modules
    assert args.breath_cleanup == "off"
    assert result["language"] == "ru"
    assert result["mfa_model"] == russian_mfa_model
    assert result["semantic_transcript"] == str(
        args.work_dir / "03_gap_recovery/source_transcript_with_recovered_gaps.json"
    )
    assert result["enriched_transcript"] is None
    assert result["ctc_enrichment_status"] == "skipped_unsupported_language"
    assert result["stages"]["hidden_retry_recovery"] == ("skipped_language_policy")
    report = read_json(args.work_dir / "03_ctc_enrichment/ctc_enrichment_report.json")
    assert report["status"] == "skipped_unsupported_language"
    assert report["language"] == "ru"
    assert report["ctc_enrichment_supported"] is False
    assert not (
        args.work_dir / "03_ctc_enrichment/source_transcript_ctc_enriched.json"
    ).exists()
    configuration = read_json(args.work_dir / "pipeline_config.json")
    assert configuration["language"] == "ru"
    assert configuration["language_profile"]["mfa_model"] == russian_mfa_model
    assert configuration["language_profile"]["whisper_language"] == "ru"
    assert configuration["breath_cleanup"] == "off"


def test_explicit_russian_breath_cleanup_override_is_preserved(tmp_path: Path) -> None:
    parsed = build_parser().parse_args(
        [
            str(tmp_path / "russian.wav"),
            "--language",
            "ru",
            "--breath-cleanup",
            "replace",
        ]
    )
    assert parsed.language == "ru"
    assert parsed.breath_cleanup == "replace"


def test_optional_ctc_process_failure_completes_with_whisper_passthrough(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source media")
    args = _args(tmp_path, source)

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

    def fake_runner(command: list[str], **_: object) -> None:
        module = command[command.index("-m") + 1]
        audio = args.work_dir / "00_media/source_audio.wav"
        audio_sha = sha256_file(audio)
        if module == "voicecut.analyze":
            output_dir = Path(command[command.index("--output-dir") + 1])
            write_json(output_dir / "analysis.json", {"audio_sha256": audio_sha})
            return
        if module == "voicecut.transcribe_mlx":
            write_json(
                Path(command[command.index("--output") + 1]),
                {
                    "audio_sha256": audio_sha,
                    "engine": "mlx_whisper",
                    "atoms": [
                        {
                            "atom_index": 0,
                            "start": 0.0,
                            "end": 1.0,
                            "text": "usable words",
                            "words": [
                                {
                                    "word": "usable",
                                    "start": 0.1,
                                    "end": 0.4,
                                },
                                {
                                    "word": "words",
                                    "start": 0.5,
                                    "end": 0.8,
                                },
                            ],
                        }
                    ],
                },
            )
            return
        if module == "voicecut.ctc_enrich":
            raise subprocess.CalledProcessError(
                71,
                command,
                stderr="RuntimeError: optional CTC runtime unavailable\n",
            )
        if module == "voicecut.gap_recovery":
            _fake_gap_recovery(command)
            return
        if module == "voicecut.take_planner":
            transcript_path = Path(command[command.index("--transcript") + 1])
            transcript = read_json(transcript_path)
            report = read_json(
                args.work_dir / "03_ctc_enrichment/ctc_enrichment_report.json"
            )
            assert transcript["engine"] == ("whisper_primary_ctc_degraded_passthrough")
            assert report["status"] == "degraded_whisper_primary_passthrough"
            assert transcript["atoms"][0]["words"][1]["word"] == "words"
            output_dir = Path(command[command.index("--output-dir") + 1])
            write_json(
                output_dir / "take_plan.json",
                {
                    "status": "complete",
                    "planner": "take_planner_v1",
                    "listened_to_audio": True,
                    "backend": "gemini",
                    "model": "gemini-3.6-flash",
                    "transcript": str(transcript_path.resolve()),
                    "transcript_sha256": sha256_file(transcript_path),
                    "fallbacks": [
                        {
                            "iteration": 3,
                            "status": "source_passthrough",
                            "source_ranges": [{"start_word_id": 0, "end_word_id": 2}],
                            "rejected_model_output_accepted": False,
                        }
                    ],
                },
            )
            return
        if module == "voicecut.cut_render":
            output_dir = Path(command[command.index("--output-dir") + 1])
            output_dir.mkdir(parents=True)
            final_cut = output_dir / "final_cut.wav"
            final_cut.write_bytes(b"rendered degraded audio")
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
                    "delivery_status": "complete_with_preserved_source_context",
                    "breath_cleanup_status": "detector_failed_energy_only",
                    "final_cut_wav": str(final_cut.resolve()),
                    "final_cut_wav_sha256": sha256_file(final_cut),
                    "final_boundary_plan": str(boundary_plan.resolve()),
                    "final_boundary_plan_sha256": sha256_file(boundary_plan),
                    "duration_seconds": 1.0,
                },
            )
            return
        raise AssertionError(command)

    def fake_audio_publisher(
        final_wav: Path,
        output_path: Path,
        *,
        manifest_path: Path,
        overwrite: bool,
    ) -> dict[str, object]:
        assert final_wav.read_bytes() == b"rendered degraded audio"
        assert overwrite is False
        output_path.write_bytes(b"published degraded audio")
        write_json(manifest_path, {"status": "complete"})
        return {"status": "complete", "output_audio": str(output_path)}

    result = run_full_pipeline(
        args,
        runner=fake_runner,
        media_preparer=fake_media_preparer,
        audio_publisher=fake_audio_publisher,
        planner_preflight=_fake_gemini_preflight,
    )

    assert Path(result["output"]).read_bytes() == b"published degraded audio"
    assert result["ctc_enrichment_status"] == ("degraded_whisper_primary_passthrough")
    assert result["stages"]["hidden_retry_recovery"] == "created_degraded"
    assert result["delivery_status"] == "complete_with_preserved_source_context"
    assert result["pipeline_warnings"] == [
        {
            "stage": "hidden_retry_recovery",
            "status": "degraded_whisper_primary_passthrough",
            "message": (
                "Optional CTC retry evidence was unavailable for part or all "
                "of the recording; VoiceCut continued with the primary "
                "Whisper occurrences."
            ),
            "report": str(
                (
                    args.work_dir / "03_ctc_enrichment/ctc_enrichment_report.json"
                ).resolve()
            ),
        },
        {
            "stage": "final_render",
            "status": "whisper_timing_fallback",
            "message": (
                "MFA word alignment was unavailable; cuts used Whisper word "
                "timing refined by the waveform."
            ),
        },
        {
            "stage": "final_render",
            "status": "breath_detector_failed",
            "message": (
                "Respiro-en breath detection failed, so breaths were judged "
                "by level only and some may remain; run scripts/install.sh."
            ),
        },
    ]


def test_public_parser_can_enable_renderer_debug_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "voice.wav"
    parsed = build_parser().parse_args(
        [
            str(source),
            "--debug-artifacts",
        ]
    )
    assert parsed.debug_artifacts is True


def test_full_pipeline_rejects_symlink_output_before_any_stage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source media")
    output_link = tmp_path / "edited.wav"
    output_link.symlink_to(source)
    args = _args(tmp_path, source)
    args.output = output_link
    args.overwrite = True

    def unexpected_media(*_: object, **__: object):
        raise AssertionError("unsafe output must fail before media preparation")

    with pytest.raises(FullPipelineError, match="symbolic link"):
        run_full_pipeline(args, media_preparer=unexpected_media)


def test_full_pipeline_rejects_output_resolving_to_input_through_symlinked_parent(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "recording.wav"
    source.write_bytes(b"source media")
    alias_dir = tmp_path / "alias"
    alias_dir.symlink_to(source_dir, target_is_directory=True)
    output = alias_dir / source.name
    assert not output.is_symlink()
    args = _args(tmp_path, source)
    args.output = output
    args.overwrite = True

    def unexpected_media(*_: object, **__: object):
        raise AssertionError("resolved input/output collision must fail immediately")

    with pytest.raises(FullPipelineError, match="must be different"):
        run_full_pipeline(args, media_preparer=unexpected_media)


def test_full_pipeline_never_resets_a_stage_containing_the_input(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "work"
    media_stage = work_dir / "00_media"
    media_stage.mkdir(parents=True)
    source = media_stage / "recording.wav"
    source.write_bytes(b"must survive")
    args = _args(tmp_path, source)

    with pytest.raises(FullPipelineError, match="input path.*disjoint"):
        run_full_pipeline(args)

    assert source.read_bytes() == b"must survive"


def test_full_pipeline_never_resets_a_stage_containing_the_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "recording.wav"
    source.write_bytes(b"source")
    work_dir = tmp_path / "work"
    publication_stage = work_dir / "06_publication"
    publication_stage.mkdir(parents=True)
    output = publication_stage / "edited.mp3"
    output.write_bytes(b"must survive")
    args = _args(tmp_path, source)
    args.output = output
    args.overwrite = True

    with pytest.raises(FullPipelineError, match="output path.*disjoint"):
        run_full_pipeline(args)

    assert output.read_bytes() == b"must survive"


def test_full_pipeline_rejects_work_dir_nested_under_future_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "recording.wav"
    source.write_bytes(b"source")
    output = tmp_path / "edited.mp3"
    args = _args(tmp_path, source)
    args.output = output
    args.work_dir = output / "cache"

    with pytest.raises(FullPipelineError, match="output path.*disjoint"):
        run_full_pipeline(args)

    assert not output.exists()


def test_full_pipeline_routes_video_input_to_video_publication(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source video")
    args = _args(tmp_path, source)
    args.output = tmp_path / "edited.mp4"
    calls: list[list[str]] = []
    video_calls = 0

    def fake_media_preparer(input_path: Path, output_dir: Path):
        output_dir.mkdir(parents=True)
        canonical = output_dir / "source_audio.wav"
        canonical.write_bytes(b"canonical audio")
        manifest = {
            "status": "complete",
            "source_media": str(input_path.resolve()),
            "source_media_sha256": sha256_file(input_path),
            "source_kind": "video",
            "canonical_audio": str(canonical.resolve()),
            "canonical_audio_sha256": sha256_file(canonical),
        }
        write_json(output_dir / "media_input.json", manifest)
        return manifest

    def fake_runner(command: list[str], **_: object) -> None:
        calls.append(command)
        module = command[command.index("-m") + 1]
        audio = args.work_dir / "00_media/source_audio.wav"
        audio_sha = sha256_file(audio)
        if module == "voicecut.analyze":
            output_dir = Path(command[command.index("--output-dir") + 1])
            write_json(output_dir / "analysis.json", {"audio_sha256": audio_sha})
        elif module == "voicecut.transcribe_mlx":
            write_json(
                Path(command[command.index("--output") + 1]),
                {"audio_sha256": audio_sha},
            )
        elif module == "voicecut.ctc_enrich":
            output_dir = Path(command[command.index("--output-dir") + 1])
            _write_fake_ctc_enrichment(output_dir, audio_sha256=audio_sha)
        elif module == "voicecut.gap_recovery":
            _fake_gap_recovery(command)
        elif module == "voicecut.take_planner":
            output_dir = Path(command[command.index("--output-dir") + 1])
            transcript = Path(command[command.index("--transcript") + 1])
            write_json(
                output_dir / "take_plan.json",
                {
                    "status": "complete",
                    "planner": "take_planner_v1",
                    "listened_to_audio": True,
                    "backend": "gemini",
                    "model": "gemini-3.6-flash",
                    "transcript": str(transcript.resolve()),
                    "transcript_sha256": sha256_file(transcript),
                },
            )
        elif module == "voicecut.cut_render":
            assert command[command.index("--pause-policy") + 1] == "cuts"
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
                    "pause_policy": "cuts",
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

    def unexpected_audio(*_: object, **__: object):
        raise AssertionError("video input must not use audio-only publication")

    def fake_video_publisher(
        *,
        source_video: Path,
        media_input_manifest_path: Path,
        final_render_manifest_path: Path,
        output_path: Path,
        manifest_path: Path,
        overwrite: bool,
    ):
        nonlocal video_calls
        video_calls += 1
        assert source_video == source.resolve()
        assert media_input_manifest_path.name == "media_input.json"
        assert final_render_manifest_path.name == "final_render_manifest.json"
        assert manifest_path.name == "video_render_manifest.json"
        assert overwrite is False
        output_path.write_bytes(b"published video")
        write_json(manifest_path, {"status": "complete"})
        return {
            "status": "complete",
            "source_video": str(source_video),
            "output_video": str(output_path),
        }

    result = run_full_pipeline(
        args,
        runner=fake_runner,
        media_preparer=fake_media_preparer,
        audio_publisher=unexpected_audio,
        video_publisher=fake_video_publisher,
        planner_preflight=_fake_gemini_preflight,
    )

    assert len(calls) == 6
    assert video_calls == 1
    assert result["input_kind"] == "video"
    assert result["output_kind"] == "video"
    assert result["pause_policy"] == "cuts"
    assert Path(result["output"]).read_bytes() == b"published video"


def test_planner_preflight_fails_before_media_work(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source media")
    media_called = False

    def failed_preflight(**_: object) -> PlannerRuntimeConfiguration:
        raise RuntimeError("GEMINI_API_KEY is missing")

    def unexpected_media(*_: object, **__: object) -> dict[str, object]:
        nonlocal media_called
        media_called = True
        return {}

    with pytest.raises(
        FullPipelineError,
        match="planner preflight failed: GEMINI_API_KEY is missing",
    ):
        run_full_pipeline(
            _args(tmp_path, source),
            media_preparer=unexpected_media,
            planner_preflight=failed_preflight,
        )
    assert media_called is False


def test_effective_endpoint_is_canonicalized_in_cache_provenance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source media")
    args = _args(tmp_path, source)
    args.planner_backend = "openai"
    args.planner_model = "gpt-5-mini"
    args.planner_base_url = "HTTPS://Planner.Example.TEST:443/v1/"

    class StopBeforeMedia(RuntimeError):
        pass

    def fake_preflight(**values: object) -> PlannerRuntimeConfiguration:
        assert values["base_url"] == "https://planner.example.test/v1"
        return PlannerRuntimeConfiguration(
            provider="openai",
            base_url="https://planner.example.test/v1",
            api_key_env="OPENAI_API_KEY",
        )

    def stop_media(*_: object, **__: object) -> dict[str, object]:
        raise StopBeforeMedia

    with pytest.raises(StopBeforeMedia):
        run_full_pipeline(
            args,
            media_preparer=stop_media,
            planner_preflight=fake_preflight,
        )
    configuration = read_json(args.work_dir / "pipeline_config.json")
    assert configuration["planner_base_url"] == "https://planner.example.test/v1"


def test_stage_failure_is_concise_and_endpoint_is_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    endpoint = "https://private.example.test/v1"

    def failed_runner(
        command: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[object]:
        raise subprocess.CalledProcessError(
            23,
            command,
            stderr=(
                "Traceback (most recent call last):\n"
                '  File "worker.py", line 10, in <module>\n'
                f"RuntimeError: endpoint {endpoint} refused the request\n"
            ),
        )

    with pytest.raises(
        FullPipelineError,
        match="TEST STAGE failed with exit status 23",
    ) as raised:
        _run(
            "TEST STAGE",
            [
                sys.executable,
                "-m",
                "voicecut.take_planner",
                "--planner-base-url",
                endpoint,
            ],
            runner=failed_runner,
        )
    assert endpoint not in str(raised.value)
    assert "RuntimeError: endpoint <redacted-endpoint> refused the request" in str(
        raised.value
    )
    captured = capsys.readouterr()
    assert endpoint not in captured.out
    assert "<redacted-endpoint>" in captured.out
    assert "Traceback" not in captured.out + captured.err


def test_planner_environment_retains_only_selected_standard_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "selected")
    monkeypatch.setenv("GEMINI_API_KEY", "unrelated")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unrelated")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://endpoint.example.test/v1")

    planner_environment = _subprocess_environment(planner_backend="openai")
    assert planner_environment["OPENAI_API_KEY"] == "selected"
    assert "GEMINI_API_KEY" not in planner_environment
    assert "DEEPSEEK_API_KEY" not in planner_environment
    assert "OPENAI_BASE_URL" not in planner_environment

    analysis_environment = _subprocess_environment()
    assert "OPENAI_API_KEY" not in analysis_environment


def test_implementation_fingerprint_is_content_addressed() -> None:
    fingerprint = _implementation_fingerprint()
    assert len(fingerprint) == 64
    assert int(fingerprint, 16) >= 0


def test_work_directory_lock_rejects_a_concurrent_owner(tmp_path: Path) -> None:
    work_dir = tmp_path / "shared-cache"
    first = _WorkDirectoryLock(work_dir)
    try:
        with pytest.raises(FullPipelineError, match="another VoiceCut run owns"):
            _WorkDirectoryLock(work_dir)
    finally:
        first.close()

    second = _WorkDirectoryLock(work_dir)
    second.close()


def test_full_pipeline_reports_progress_callback_stage_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source media")
    args = _args(tmp_path, source)
    monkeypatch.setenv("GEMINI_API_KEY", "selected-secret")

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
        final_wav: Path,
        output_path: Path,
        *,
        manifest_path: Path,
        overwrite: bool,
    ):
        output_path.write_bytes(b"published audio")
        return {"status": "complete", "output_audio": str(output_path)}

    def fake_runner(command: list[str], **kwargs: object) -> None:
        module = command[command.index("-m") + 1]
        audio = args.work_dir / "00_media/source_audio.wav"
        audio_sha = sha256_file(audio)
        if module == "voicecut.analyze":
            output_dir = Path(command[command.index("--output-dir") + 1])
            write_json(output_dir / "analysis.json", {"audio_sha256": audio_sha})
        elif module == "voicecut.transcribe_mlx":
            write_json(
                Path(command[command.index("--output") + 1]),
                {"audio_sha256": audio_sha},
            )
        elif module == "voicecut.ctc_enrich":
            output_dir = Path(command[command.index("--output-dir") + 1])
            _write_fake_ctc_enrichment(output_dir, audio_sha256=audio_sha)
        elif module == "voicecut.gap_recovery":
            _fake_gap_recovery(command)
        elif module == "voicecut.take_planner":
            output_dir = Path(command[command.index("--output-dir") + 1])
            transcript = Path(command[command.index("--transcript") + 1])
            write_json(
                output_dir / "take_plan.json",
                {
                    "status": "complete",
                    "planner": "take_planner_v1",
                    "listened_to_audio": True,
                    "backend": "gemini",
                    "model": "gemini-3.6-flash",
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

    events: list[dict[str, object]] = []
    run_full_pipeline(
        args,
        runner=fake_runner,
        media_preparer=fake_media_preparer,
        audio_publisher=fake_audio_publisher,
        planner_preflight=_fake_gemini_preflight,
        progress_callback=events.append,
    )
    stage_order = [
        "preflight",
        "media_input",
        "analysis",
        "transcription",
        "hidden_retry_recovery",
        "gap_recovery",
        "semantic_plan",
        "final_render",
        "publication",
    ]
    assert [(event["stage"], event["status"]) for event in events] == [
        pair
        for stage in stage_order
        for pair in ((stage, "started"), (stage, "completed"))
    ]

    events.clear()
    run_full_pipeline(
        args,
        runner=fake_runner,
        media_preparer=fake_media_preparer,
        audio_publisher=fake_audio_publisher,
        planner_preflight=_fake_gemini_preflight,
        progress_callback=events.append,
    )
    assert [(event["stage"], event["status"]) for event in events] == [
        ("preflight", "started"),
        ("preflight", "completed"),
        *((stage, "cached") for stage in stage_order[1:]),
    ]

    # Russian has no validated CTC enrichment: the stage is reported as skipped.
    args.language = "ru"
    args.work_dir = tmp_path / "work-ru"
    args.breath_cleanup = None
    events.clear()

    def russian_runner(command: list[str], **kwargs: object) -> None:
        if "voicecut.cut_render" in command:
            assert command[command.index("--breath-cleanup") + 1] == "off"
        fake_runner(command, **kwargs)

    def russian_publisher(final_wav, output_path, *, manifest_path, overwrite):
        return fake_audio_publisher(
            final_wav,
            output_path,
            manifest_path=manifest_path,
            overwrite=True,
        )

    args.overwrite = True
    monkeypatch.setattr(
        "voicecut.full_pipeline._render_is_current",
        lambda *a, **k: a[0].is_file(),
    )
    run_full_pipeline(
        args,
        runner=russian_runner,
        media_preparer=fake_media_preparer,
        audio_publisher=russian_publisher,
        planner_preflight=_fake_gemini_preflight,
        progress_callback=events.append,
    )
    statuses = {event["stage"]: event["status"] for event in events}
    assert statuses["hidden_retry_recovery"] == "skipped"
    assert [e for e in events if e["stage"] == "hidden_retry_recovery"] == [
        {"stage": "hidden_retry_recovery", "status": "skipped"}
    ]

    # Verbatim ASR already keeps hidden retries: CTC enrichment is skipped.
    args.language = "en"
    args.asr = "crisperwhisper"
    args.work_dir = tmp_path / "work-verbatim"
    args.breath_cleanup = None
    events.clear()
    modules: list[str] = []

    def verbatim_runner(command: list[str], **kwargs: object) -> None:
        module = command[command.index("-m") + 1]
        modules.append(module)
        if module == "voicecut.transcribe_mlx":
            assert command[command.index("--engine") + 1] == "crisperwhisper"
            assert command[command.index("--model") + 1] == (
                "nyralabs/CrisperWhisper2.0_large"
            )
        fake_runner(command, **kwargs)

    result = run_full_pipeline(
        args,
        runner=verbatim_runner,
        media_preparer=fake_media_preparer,
        audio_publisher=russian_publisher,
        planner_preflight=_fake_gemini_preflight,
        progress_callback=events.append,
    )
    assert "voicecut.ctc_enrich" not in modules
    assert result["stages"]["hidden_retry_recovery"] == "skipped_verbatim_asr"
    assert [e for e in events if e["stage"] == "hidden_retry_recovery"] == [
        {"stage": "hidden_retry_recovery", "status": "skipped"}
    ]
    assert read_json(args.work_dir / "pipeline_config.json")["asr"] == "crisperwhisper"
