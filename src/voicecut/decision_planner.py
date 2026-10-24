"""Take selection with a decision model (see :mod:`voicecut.decision_models`).

The transcript is split into the take planner's lines (speech between pauses)
and decided ten lines at a time.  Each request shows a model the lines to
decide (marked ``>>``) and the lines that follow them, and asks two typed
questions per marked line:

* Choice: which of the next lines is a retake of it (the speaker saying it
  again), or none;
* yes/no: whether it is narration at all (or, per model, whether it is a stray
  word, filler or a remark to self).

A line is cut when a later line retakes it or when it is not narration: the
last complete take of a sentence survives, as the take planner's editing rules
say.  The model never sees the script and the rules are the same for every
recording.  Lines are kept or cut whole; kept runs of lines become the plan's
items, in the take planner's ``take_plan.json`` format.

Kev runs on Apple Silicon through MLX (``python -m voicecut.decision_planner``
in the MLX environment); Jev through OpenRouter's Decisions API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import decision_models, progress
from .common import read_json, sha256_file, write_json
from .decision_models import DecisionModel, Protocol
from .take_planner import (
    LINE_GAP_SECONDS,
    PLAN_FILENAME,
    KeptItem,
    Line,
    TranscriptWord,
    _items_text,
    build_lines,
    find_duplicate_suspects,
    load_transcript_words,
    merged_source_ranges,
)

DECISION_PLANNER_NAME = "decision_planner_v1"
OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
# OpenRouter switches Jev to its long-context mode (slower and less accurate)
# above about 32,000 bytes of text: requests stay under this.
MAX_REQUEST_BYTES = 30_000
MAX_TRANSPORT_RETRIES = 6

PREAMBLE = (
    "Raw machine (ASR) transcript of a voice-over recording; a new line starts after every pause "
    "(the pause before a line is shown in brackets). "
    "The speaker reads prepared narration and often restarts: false starts, stumbles, repeated words, "
    "re-reads of sentences or whole paragraphs, abandoned fragments, filler, and remarks to themself. "
    "In the final edit every sentence is heard exactly once, normally from its LAST complete and fluent "
    "attempt; earlier attempts of the same words are cut, and so are fragments, filler and remarks."
)


class DecisionPlanError(RuntimeError):
    """The decision planner could not produce a plan."""


# -- requests ------------------------------------------------------------------


def line_text(words: Sequence[TranscriptWord], line: Line) -> str:
    return " ".join(word.text for word in words[line.first_id : line.last_id + 1])


def build_requests(
    words: Sequence[TranscriptWord], lines: Sequence[Line], protocol: Protocol
) -> list[dict[str, Any]]:
    """One request per block of lines: a text state and typed questions."""

    requests = []
    for start in range(0, len(lines), protocol.block_lines):
        block = lines[start : start + protocol.block_lines]
        before = [
            line
            for line in lines[:start]
            if line.start >= block[0].start - protocol.back_seconds
        ]
        after = [
            line
            for line in lines[start + protocol.block_lines :]
            if line.start <= block[-1].end + protocol.ahead_seconds
        ]
        marked = {line.index for line in block}
        rendered = []
        for line in [*before, *block, *after]:
            if after and line.index == after[0].index:
                rendered.append("--- LATER (look-ahead: shows what is said next) ---")
            pause = (
                f" (+{line.pause_before:.1f}s)" if line.pause_before is not None else ""
            )
            arrow = ">> " if line.index in marked else ""
            rendered.append(
                f"{arrow}L{line.index + 1}{pause}: {line_text(words, line)}"
            )
        state = (
            PREAMBLE
            + "\n\nThe lines marked >> are the ones to decide now.\n\n"
            + "\n".join(rendered)
        )
        questions: dict[str, dict[str, Any]] = {}
        for line in block:
            text = line_text(words, line)
            later = [other for other in [*block, *after] if other.index > line.index][
                : protocol.options
            ]
            criteria: dict[str, Any] = {"none": "no later line says these words again"}
            criteria.update({f"L{other.index + 1}": None for other in later})
            retake = (
                "the speaker saying the same thing again, in the same words or reworded, alone or as part "
                "of a longer re-read, so that this line is no longer needed?"
                if protocol.reworded
                else "the speaker saying the same words again (completely or as the start of a new attempt)?"
            )
            questions[f"{line.index}:retake"] = {
                "type": "choice",
                "instructions": f'Line L{line.index + 1}: "{text}". Which later line is a retake of it: {retake}',
                "criteria": criteria,
            }
            if protocol.side_question == "junk":
                questions[f"{line.index}:junk"] = {
                    "type": "noul",
                    "instructions": (
                        f'Line L{line.index + 1}: "{text}". Is this line a stray word, filler, or a remark the '
                        "speaker makes to themself, rather than part of the text being read?"
                    ),
                }
            else:
                questions[f"{line.index}:narration"] = {
                    "type": "noul",
                    "instructions": (
                        f'Line L{line.index + 1}: "{text}". Is this line narration meant for the audience '
                        "(as opposed to an abandoned fragment, filler, or a remark to themself)?"
                    ),
                }
        requests.append(
            {"block": len(requests) + 1, "state": state, "questions": questions}
        )
    return requests


def decide(answers: dict[str, dict[str, float]]) -> dict[int, bool]:
    """Keep or cut each line from its answers' probabilities."""

    keep: dict[int, bool] = {}
    for qid in answers:
        keep.setdefault(int(qid.split(":")[0]), True)
    for qid, probabilities in answers.items():
        line, kind = qid.split(":")
        index = int(line)
        if kind == "retake":
            retaken = max(probabilities, key=probabilities.__getitem__) != "none"
            keep[index] = keep[index] and not retaken
        elif kind == "junk":
            keep[index] = keep[index] and probabilities["true"] <= 0.5
        else:
            keep[index] = keep[index] and probabilities["true"] > 0.5
    return keep


def _break_after(text: str) -> str:
    """The pause after a kept run, from its last word (the renderer's own rule
    for words inside an item); a run that stops mid-sentence still ends at a
    pause in the recording, so it gets a phrase pause."""

    stripped = text.rstrip("\"'”’»)]")
    if stripped and stripped[-1] in ".?!" and not stripped.endswith(("...", "…")):
        return "sentence"
    return "comma"


def kept_items(
    words: Sequence[TranscriptWord], lines: Sequence[Line], keep: dict[int, bool]
) -> list[KeptItem]:
    items: list[KeptItem] = []
    for line in lines:
        if not keep.get(line.index, True):
            continue
        if items and items[-1].last_id + 1 == line.first_id:
            items[-1].last_id = line.last_id
        else:
            items.append(
                KeptItem(
                    first_id=line.first_id,
                    last_id=line.last_id,
                    text="",
                    break_after="",
                    origin="decision",
                )
            )
    for item in items:
        item.text = " ".join(
            word.text for word in words[item.first_id : item.last_id + 1]
        )
        item.break_after = _break_after(words[item.last_id].text)
    return items


# -- engines ---------------------------------------------------------------------


def _options(question: dict[str, Any]) -> tuple[list[str], list[Any]]:
    if question["type"] == "noul":
        return ["false", "true"], [None, None]
    return list(question["criteria"]), list(question["criteria"].values())


def _softmax(values: Sequence[float]) -> list[float]:
    top = max(values)
    exps = [math.exp(value - top) for value in values]
    total = sum(exps)
    return [value / total for value in exps]


def _memory_gb() -> float:
    from .local_models import memory_gb

    return memory_gb() or 0.0


class KevEngine:
    """Kev (jaredpalmer/kev, Apache-2.0) on MLX: a Qwen3.5 base with Kev's LoRA
    folded in and its pointer head, as ``kev.mlx_model`` runs it.  The state
    of a request is computed once and every question continues from a copy of
    it (exact: the questions are independent rows after the state)."""

    SPECIAL = (
        "<|fim_prefix|>",
        "<|fim_middle|>",
        "<|box_start|>",
        "<|box_end|>",
        "<|fim_suffix|>",
    )
    PREFILL_CHUNK = 1024
    ROW_BUDGET = 16384

    def __init__(self, model: DecisionModel) -> None:
        import mlx.core as mx
        import numpy as np
        import torch
        from huggingface_hub import snapshot_download
        from transformers import AutoTokenizer

        self.mx, self.np = mx, np
        mx.set_cache_limit(1 << 30)
        patterns = list(decision_models.WEIGHT_PATTERNS)
        adapter = Path(
            snapshot_download(
                model.adapter_repo,
                revision=model.adapter_revision,
                allow_patterns=patterns,
                local_files_only=True,
            )
        )
        base = Path(
            snapshot_download(
                model.base_repo,
                revision=model.base_revision,
                allow_patterns=patterns,
                local_files_only=True,
            )
        )
        meta = torch.load(adapter / "head.pt", map_location="cpu", weights_only=True)
        self.tok = AutoTokenizer.from_pretrained(base)
        self.bits = None if _memory_gb() >= (model.full_memory_gb or 0) else model.bits
        self.model = self._load(base, adapter, self.bits)
        head = {key: value.float().numpy() for key, value in meta["head"].items()}
        self.wq, self.bq = head["q.weight"], head["q.bias"]
        self.wk, self.bk = head["k.weight"], head["k.bias"]
        self.scale = 1 / math.sqrt(int(meta["head_dim"]))
        self.special = [self.tok.convert_tokens_to_ids(token) for token in self.SPECIAL]
        self.pad = self.tok.pad_token_id or 0

    def _load(self, base: Path, adapter: Path, bits: int | None):
        """mlx-lm's Qwen3.5 with the LoRA folded in (W + B @ A * alpha / r, in
        float32 on the CPU), quantized to ``bits`` when given.  Everything stays
        lazy until the parameters are evaluated one by one, so a quantized model
        never holds all of its float weights in memory at once."""

        mx = self.mx
        import mlx.nn as nn
        from mlx.utils import tree_flatten
        from mlx_lm.models import qwen3_5

        config = json.loads((base / "config.json").read_text())
        model = qwen3_5.Model(qwen3_5.ModelArgs.from_dict(config))
        weights: dict[str, Any] = {}
        for shard in sorted(base.glob("*.safetensors")):
            weights.update(mx.load(str(shard)))
        weights = {
            k: v for k, v in model.sanitize(weights).items() if not k.startswith("mtp.")
        }
        model.load_weights(list(weights.items()), strict=False)
        del weights
        cfg = json.loads((adapter / "adapter_config.json").read_text())
        alpha = cfg["lora_alpha"] / (
            math.sqrt(cfg["r"]) if cfg.get("use_rslora") else cfg["r"]
        )
        lora = mx.load(str(adapter / "adapter_model.safetensors"))
        params = dict(tree_flatten(model.parameters()))
        merged = {}
        with mx.stream(mx.cpu):
            for name, a in lora.items():
                if not name.endswith(".lora_A.weight"):
                    continue
                stem = name[: -len(".lora_A.weight")]
                inner = re.sub(r"^base_model\.model\.(model\.)?", "", stem)
                target = (
                    "language_model.model."
                    + inner.removeprefix("language_model.").removeprefix("model.")
                    + ".weight"
                )
                if target not in params:
                    raise DecisionPlanError(
                        f"the Kev adapter does not fit {base.name}: no {target}"
                    )
                delta = (
                    lora[stem + ".lora_B.weight"].astype(mx.float32)
                    @ a.astype(mx.float32)
                ) * alpha
                merged[target] = (params[target].astype(mx.float32) + delta).astype(
                    params[target].dtype
                )
        model.load_weights(list(merged.items()), strict=False)
        del params, merged, lora
        if bits:
            nn.quantize(model, group_size=64, bits=bits)
        for _, value in tree_flatten(model.parameters()):
            mx.eval(value)
        model.eval()
        mx.clear_cache()
        return model

    def _tokens(self, text: str) -> list[int]:
        # Caller text can never produce Kev's delimiter tokens.
        return self.tok(
            re.sub(r"<\|([A-Za-z0-9_]+)\|>", r"<¦\1¦>", text), add_special_tokens=False
        ).input_ids

    def _row(
        self, state_ids: list[int], question: dict[str, Any]
    ) -> tuple[list[int], list[int]]:
        keys, descriptions = _options(question)
        names = ["no", "yes"] if question["type"] == "noul" else keys
        texts = [
            name if not description else f"{name}: {description}"
            for name, description in zip(names, descriptions)
        ]
        _, q, o, c, d = self.special
        ids = [q] + self._tokens(str(question.get("instructions") or ""))
        ends = []
        for text in texts:
            ids += [o] + self._tokens(text) + [c]
            ends.append(len(ids) - 1)
        ids.append(d)
        return ids, [len(ids) - 1, *ends]

    def answer(
        self, state: str, questions: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, float]]:
        mx, np = self.mx, self.np
        from mlx_lm.models.cache import make_prompt_cache

        text_model = self.model.language_model.model
        state_ids = [self.special[0]] + self._tokens(state)
        cache = make_prompt_cache(self.model)
        for start in range(0, len(state_ids), self.PREFILL_CHUNK):
            text_model(
                mx.array([state_ids[start : start + self.PREFILL_CHUNK]]), cache=cache
            )
            mx.eval([entry.state for entry in cache])
        qids = list(questions)
        rows = [self._row(state_ids, questions[qid]) for qid in qids]
        longest = max(len(ids) for ids, _ in rows)
        per_pass = max(1, self.ROW_BUDGET // (len(state_ids) + longest))
        answers: dict[str, dict[str, float]] = {}
        for start in range(0, len(rows), per_pass):
            part = list(range(start, min(len(rows), start + per_pass)))
            length = max(len(rows[i][0]) for i in part)
            batch = [type(entry).merge([entry] * len(part)) for entry in cache]
            ids = mx.array(
                [rows[i][0] + [self.pad] * (length - len(rows[i][0])) for i in part],
                dtype=mx.int32,
            )
            hidden = text_model(ids, cache=batch)
            mx.eval(hidden)
            for row, i in enumerate(part):
                picked = np.asarray(
                    hidden[row][mx.array(rows[i][1])].astype(mx.float32)
                )
                query = self.wq @ picked[0] + self.bq
                keys = picked[1:] @ self.wk.T + self.bk
                logits = (keys @ query) * self.scale
                names = _options(questions[qids[i]])[0]
                answers[qids[i]] = dict(zip(names, _softmax(logits.tolist())))
            del batch, hidden
        return answers


class JevEngine:
    """Hosted Jev (TypeSafe) through OpenRouter's Decisions API."""

    def __init__(self, model: DecisionModel, api_key: str) -> None:
        self.model = model
        self.api_key = api_key
        self.cost = 0.0

    @staticmethod
    def _question(question: dict[str, Any]) -> dict[str, Any]:
        if question["type"] == "noul":
            return {"type": "noul", "instructions": question["instructions"]}
        return {
            "type": "choice",
            "instructions": question["instructions"],
            "criteria": question["criteria"],
        }

    def _call(self, state: str, questions: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(
            {"model": self.model.id, "state": state, "questions": questions}
        ).encode()
        for attempt in range(MAX_TRANSPORT_RETRIES):
            request = urllib.request.Request(
                OPENROUTER_DECISIONS_URL,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")[:300]
                if (
                    error.code in (429, 500, 502, 503, 504)
                    and attempt < MAX_TRANSPORT_RETRIES - 1
                ):
                    time.sleep(5 * (attempt + 1))
                    continue
                raise DecisionPlanError(
                    f"OpenRouter answered HTTP {error.code}: {detail}"
                ) from None
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < MAX_TRANSPORT_RETRIES - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise DecisionPlanError(f"cannot reach OpenRouter: {error}") from None
        raise DecisionPlanError("OpenRouter did not answer")

    def answer(
        self, state: str, questions: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, float]]:
        parts: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        size = len(state.encode())
        for qid, question in questions.items():
            converted = self._question(question)
            extra = len(json.dumps(converted, ensure_ascii=False).encode())
            if current and size + extra > MAX_REQUEST_BYTES:
                parts.append(current)
                current, size = {}, len(state.encode())
            current[qid] = converted
            size += extra
        if current:
            parts.append(current)
        answers: dict[str, dict[str, float]] = {}
        for part in parts:
            response = self._call(state, part)
            self.cost += float((response.get("usage") or {}).get("cost") or 0.0)
            for qid, answer in (response.get("answers") or {}).items():
                if answer.get("type") == "noul":
                    p = float(answer["noul"])
                    answers[qid] = {"false": 1 - p, "true": p}
                else:
                    answers[qid] = {
                        k: float(v) for k, v in answer["probabilities"].items()
                    }
        missing = set(questions) - set(answers)
        if missing:
            raise DecisionPlanError(f"Jev left {len(missing)} questions unanswered")
        return answers


def _api_key(env_file: Path | None) -> str:
    value = os.environ.get("OPENROUTER_API_KEY")
    if not value and env_file is not None and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, raw = line.partition("=")
            if name.strip().removeprefix("export ").strip() == "OPENROUTER_API_KEY":
                value = raw.strip().strip('"').strip("'")
    if not value:
        raise DecisionPlanError(
            "Jev runs through OpenRouter: add your OpenRouter API key"
        )
    return value


def create_engine(
    model: DecisionModel, env_file: Path | None = None
) -> KevEngine | JevEngine:
    if model.backend == "openrouter":
        return JevEngine(model, _api_key(env_file))
    if not decision_models.weights_ready(model):
        raise DecisionPlanError(
            f"{model.label} is not downloaded: download it from the planner's model list in the editor"
        )
    try:
        return KevEngine(model)
    except ImportError as error:
        raise DecisionPlanError(
            f"{model.label} runs on MLX (Apple Silicon) and needs mlx-lm in the MLX environment: {error}"
        ) from None


# -- the plan --------------------------------------------------------------------


def run_decision_planner(
    *,
    transcript_path: Path,
    output_dir: Path,
    backend: str,
    model: str,
    language: str = "en",
    env_file: Path | None = None,
    engine: Any = None,
) -> dict[str, Any]:
    """Plan the edit with a decision model and write ``take_plan.json``."""

    chosen = decision_models.find(backend, model)
    if chosen is None:
        raise DecisionPlanError(f"{backend} / {model} is not a decision model")
    transcript_path = transcript_path.resolve()
    output_dir = output_dir.resolve()
    calls_dir = output_dir / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)
    transcript = read_json(transcript_path)
    words = load_transcript_words(transcript)
    lines = build_lines(words)
    requests = build_requests(words, lines, chosen.protocol)
    started = time.monotonic()
    engine = engine or create_engine(chosen, env_file)
    print(f"{chosen.label}: {len(lines)} lines in {len(requests)} requests", flush=True)
    answers: dict[str, dict[str, float]] = {}
    ledger = []
    for request in requests:
        name = f"block_{request['block']:04d}"
        key = hashlib.sha256(
            json.dumps(
                [chosen.id, request["state"], request["questions"]], sort_keys=True
            ).encode()
        ).hexdigest()
        path = calls_dir / f"{name}.json"
        cached = read_json(path) if path.is_file() else None
        if isinstance(cached, dict) and cached.get("request_sha256") == key:
            got = cached["answers"]
        else:
            block_started = time.monotonic()
            got = engine.answer(request["state"], request["questions"])
            write_json(
                path,
                {
                    "request_sha256": key,
                    "elapsed_seconds": round(time.monotonic() - block_started, 2),
                    "answers": got,
                },
            )
        answers.update(got)
        ledger.append({"name": name, "questions": len(request["questions"])})
        progress.report(request["block"], len(requests), "requests")
        if request["block"] % 10 == 0 or request["block"] == len(requests):
            print(f"{name}: {request['block']}/{len(requests)} requests", flush=True)
    keep = decide(answers)
    items = kept_items(words, lines, keep)
    if not items:
        raise DecisionPlanError("the decision model kept no narration")
    plan = {
        "schema_version": 1,
        "planner": DECISION_PLANNER_NAME,
        "status": "complete",
        "backend": backend,
        "model": model,
        "language": language,
        "transcript": str(transcript_path),
        "transcript_sha256": sha256_file(transcript_path),
        "audio": None,
        "listened_to_audio": False,
        "protocol": asdict(chosen.protocol),
        "line_gap_seconds": LINE_GAP_SECONDS,
        "word_count": len(words),
        "words": [asdict(word) for word in words],
        "items": [asdict(item) for item in items],
        "selected_source_ranges": merged_source_ranges(items),
        "reconstructed_narration": _items_text(items),
        "decisions": [
            {
                "line": line.index,
                "first_id": line.first_id,
                "last_id": line.last_id,
                "keep": keep.get(line.index, True),
            }
            for line in lines
        ],
        "windows": ledger,
        "review": [],
        "remaining_duplicate_suspects": [
            {"items": [left, right], "reason": reason}
            for left, right, reason in find_duplicate_suspects(items, words)
        ],
        "seconds": round(time.monotonic() - started, 1),
        "cost_usd": round(getattr(engine, "cost", 0.0), 6) or None,
    }
    write_json(output_dir / PLAN_FILENAME, plan)
    kept_lines = sum(1 for value in keep.values() if value)
    print(f"kept {kept_lines} of {len(lines)} lines in {len(items)} items", flush=True)
    return plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m voicecut.decision_planner",
        description="Choose the final takes of a narration transcript with a decision model.",
    )
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--planner-backend", required=True)
    parser.add_argument("--planner-model", required=True)
    parser.add_argument("--language", default="en")
    parser.add_argument("--env-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        run_decision_planner(
            transcript_path=args.transcript,
            output_dir=args.output_dir,
            backend=args.planner_backend,
            model=args.planner_model,
            language=args.language,
            env_file=args.env_file,
        )
    except DecisionPlanError as error:
        print(f"decision planner: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
