from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from voicecut.common import read_json, write_json
from voicecut.take_planner import (
    KeptItem,
    TranscriptWord,
    WindowResponseError,
    build_lines,
    find_duplicate_suspects,
    merged_source_ranges,
    parse_window_response,
    run_take_planner,
)


def _words(spec: list[tuple[str, float]]) -> list[TranscriptWord]:
    """Words from (text, start) pairs; every word lasts 0.2 s."""

    return [
        TranscriptWord(id=index, text=text, start=start, end=start + 0.2)
        for index, (text, start) in enumerate(spec)
    ]


def _sentence(text: str, start: float) -> list[tuple[str, float]]:
    return [(word, start + 0.25 * index) for index, word in enumerate(text.split())]


def test_build_lines_splits_only_at_pauses() -> None:
    words = _words([*_sentence("Here is the paradox.", 0.0), *_sentence("Again.", 2.0)])
    lines = build_lines(words)
    assert [(line.first_id, line.last_id) for line in lines] == [(0, 3), (4, 4)]
    assert lines[0].pause_before is None
    assert lines[1].pause_before == pytest.approx(2.0 - 0.95)


def _response(keep: list[dict[str, Any]], **extra: Any) -> str:
    return json.dumps(
        {
            "notes": "",
            "keep": keep,
            "drop_previous": [],
            "resume_from_id": None,
            **extra,
        }
    )


def _item(first: int, last: int, text: str = "x") -> dict[str, Any]:
    return {"first_id": first, "last_id": last, "text": text, "break_after": "none"}


def test_parse_repairs_geometry_that_loses_no_narration() -> None:
    words = _words(_sentence("zero one two three four five six", 0.0))
    lines = build_lines(words)
    common = {
        "previous_count": 0,
        "final": True,
        "zone_lines": lines,
        "origin": "w",
        "words": words,
    }
    # Out of order, overlapping, starting before and reaching past the zone.
    decision = parse_window_response(
        _response([_item(4, 9), _item(0, 2), _item(2, 3, "two three")]),
        zone_first_id=1,
        zone_last_id=5,
        **common,
    )
    assert [(item.first_id, item.last_id) for item in decision.items] == [
        (1, 2),
        (3, 3),
        (4, 5),
    ]
    # A changed item says what its words say.
    assert [item.text for item in decision.items] == ["one two", "three", "four five"]
    assert decision.warnings
    # An item wholly inside one kept before it adds nothing.
    decision = parse_window_response(
        _response([_item(0, 4), _item(1, 2)]),
        zone_first_id=0,
        zone_last_id=6,
        **common,
    )
    assert [(item.first_id, item.last_id) for item in decision.items] == [(0, 4)]


def test_parse_leaves_a_sentence_past_the_zone_to_the_next_window() -> None:
    words = _words(
        [*_sentence("First sentence here.", 0.0), *_sentence("Second one two", 3.0)]
    )
    lines = build_lines(words)
    decision = parse_window_response(
        _response([_item(0, 2), _item(3, 6)]),
        zone_first_id=0,
        zone_last_id=4,
        previous_count=0,
        final=False,
        zone_lines=lines,
        origin="w",
        words=words,
    )
    assert [(item.first_id, item.last_id) for item in decision.items] == [(0, 2)]
    assert decision.resume_from_id == 3


def test_parse_rejects_lossy_repairs_until_the_last_attempt() -> None:
    lines = build_lines(_words(_sentence("one two three four", 0.0)))
    common = {
        "zone_first_id": 2,
        "zone_last_id": 3,
        "previous_count": 0,
        "final": True,
        "zone_lines": lines,
        "origin": "w",
    }
    reversed_range = _response([_item(3, 2)])
    before_zone = _response([_item(0, 1), _item(2, 3)])
    with pytest.raises(WindowResponseError, match="after last_id"):
        parse_window_response(reversed_range, **common)
    with pytest.raises(WindowResponseError, match="precedes the editable"):
        parse_window_response(before_zone, **common)
    swapped = parse_window_response(reversed_range, strict=False, **common).items
    assert [(item.first_id, item.last_id) for item in swapped] == [(2, 3)]
    kept = parse_window_response(before_zone, strict=False, **common).items
    assert [(item.first_id, item.last_id) for item in kept] == [(2, 3)]


def test_parse_defers_items_after_resume_to_the_next_window() -> None:
    words = _words(
        [*_sentence("First sentence here.", 0.0), *_sentence("Second one", 3.0)]
    )
    lines = build_lines(words)
    decision = parse_window_response(
        _response(
            [
                {
                    "first_id": 0,
                    "last_id": 2,
                    "text": "First sentence here.",
                    "break_after": "sentence",
                },
                {
                    "first_id": 3,
                    "last_id": 4,
                    "text": "Second one",
                    "break_after": "none",
                },
            ],
            resume_from_id=4,
        ),
        zone_first_id=0,
        zone_last_id=4,
        previous_count=0,
        final=False,
        zone_lines=lines,
        origin="w",
    )
    # The resume ID snaps back to its line start and the straddling item waits.
    assert decision.resume_from_id == 3
    assert [(item.first_id, item.last_id) for item in decision.items] == [(0, 2)]


def test_parse_repairs_invalid_break_and_ignores_bad_drop() -> None:
    lines = build_lines(_words(_sentence("one two", 0.0)))
    decision = parse_window_response(
        _response(
            [{"first_id": 0, "last_id": 1, "text": "one two", "break_after": "long"}],
            drop_previous=[1, 9],
        ),
        zone_first_id=0,
        zone_last_id=1,
        previous_count=2,
        final=True,
        zone_lines=lines,
        origin="w",
    )
    assert decision.items[0].break_after == "sentence"
    assert decision.drop_previous == [1]
    assert decision.resume_from_id is None


def test_duplicate_suspects_find_retakes_but_not_distinct_sentences() -> None:
    words = _words(
        [
            *_sentence("This randomized method might seem too abstract.", 0.0),
            *_sentence("No method would have uniform stages.", 3.0),
            *_sentence("This randomized method might look too abstract.", 6.0),
            *_sentence("Gradients flow backward through every stage.", 9.0),
        ]
    )
    items = [
        KeptItem(0, 6, "", "sentence", "w"),
        KeptItem(7, 12, "", "sentence", "w"),
        KeptItem(13, 19, "", "sentence", "w"),
        KeptItem(20, 25, "", "sentence", "w"),
    ]
    pairs = {(left, right) for left, right, _ in find_duplicate_suspects(items, words)}
    assert (0, 2) in pairs
    assert not any(3 in pair for pair in pairs)


def test_duplicate_suspects_find_a_repeat_inside_one_item() -> None:
    words = _words(
        _sentence("and that might be the reason and that might be the reason why", 0.0)
    )
    items = [KeptItem(0, len(words) - 1, "", "sentence", "w")]
    assert find_duplicate_suspects(items, words)[0][:2] == (0, 0)


def test_merged_source_ranges_join_contiguous_items() -> None:
    items = [
        KeptItem(0, 3, "", "none", "w"),
        KeptItem(4, 6, "", "sentence", "w"),
        KeptItem(9, 9, "", "sentence", "w"),
    ]
    assert merged_source_ranges(items) == [
        {"start_word_id": 0, "end_word_id": 7},
        {"start_word_id": 9, "end_word_id": 10},
    ]


class ScriptedBackend:
    """Answers each request with the next scripted response."""

    backend_name = "fake"
    model = "fake-model"

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate(
        self, prompt: str, *, response_schema: dict[str, Any], request_id: str
    ) -> str:
        del response_schema, request_id
        self.prompts.append(prompt)
        return json.dumps(
            {
                "notes": "",
                "drop_previous": [],
                "resume_from_id": None,
                **self.responses.pop(0),
            }
        )

    def close(self) -> None:
        pass


def _transcript(path: Path, words: list[TranscriptWord]) -> Path:
    write_json(
        path,
        {
            "atoms": [
                {
                    "atom_index": 0,
                    "words": [
                        {"word": word.text, "start": word.start, "end": word.end}
                        for word in words
                    ],
                }
            ]
        },
    )
    return path


def test_planner_revokes_an_earlier_take_and_reviews_the_result(tmp_path: Path) -> None:
    words = _words(
        [
            *_sentence("Pipeline stages run in order.", 0.0),
            *_sentence("Each stage owns its weights.", 5.0),
            *_sentence("Pipeline stages run in order.", 10.0),
        ]
    )
    transcript = _transcript(tmp_path / "transcript.json", words)
    backend = ScriptedBackend(
        [
            # Window 1 decides the first eight seconds and keeps both sentences.
            {
                "keep": [
                    {
                        "first_id": 0,
                        "last_id": 4,
                        "text": "Pipeline stages run in order.",
                        "break_after": "sentence",
                    },
                    {
                        "first_id": 5,
                        "last_id": 9,
                        "text": "Each stage owns its weights.",
                        "break_after": "sentence",
                    },
                ],
                "resume_from_id": None,
            },
            # Window 2 sees the retake of K1, revokes it and keeps the new take.
            {
                "keep": [
                    {
                        "first_id": 10,
                        "last_id": 14,
                        "text": "Pipeline stages run in order.",
                        "break_after": "paragraph",
                    },
                ],
                "drop_previous": [1],
            },
        ]
    )
    plan = run_take_planner(
        transcript_path=transcript,
        output_dir=tmp_path / "plan",
        backend=backend,
        audio_path=None,
        window_seconds=8.0,
        lookahead_seconds=3.0,
    )
    assert [(item["first_id"], item["last_id"]) for item in plan["items"]] == [
        (5, 9),
        (10, 14),
    ]
    assert plan["selected_source_ranges"] == [{"start_word_id": 5, "end_word_id": 15}]
    assert "ALREADY IN THE EDIT" in backend.prompts[1]
    assert "LOOK-AHEAD" in backend.prompts[0]
    assert plan["remaining_duplicate_suspects"] == []
    assert read_json(tmp_path / "plan" / "take_plan.json")["status"] == "complete"


def test_planner_reuses_cached_responses_on_rerun(tmp_path: Path) -> None:
    words = _words(_sentence("Only one sentence here.", 0.0))
    transcript = _transcript(tmp_path / "transcript.json", words)
    answer = {
        "keep": [
            {
                "first_id": 0,
                "last_id": 3,
                "text": "Only one sentence here.",
                "break_after": "sentence",
            }
        ]
    }
    run_take_planner(
        transcript_path=transcript,
        output_dir=tmp_path / "plan",
        backend=ScriptedBackend([answer]),
        audio_path=None,
    )
    rerun = run_take_planner(
        transcript_path=transcript,
        output_dir=tmp_path / "plan",
        backend=ScriptedBackend([]),  # any request would fail
        audio_path=None,
    )
    assert rerun["reconstructed_narration"] == "Only one sentence here."


def test_duplicate_suspects_ignore_sentences_that_only_share_an_opening() -> None:
    words = _words(
        [
            *_sentence("For local SGD, this is not a stale error.", 0.0),
            *_sentence("For local SGD, the drift term grows as H squared.", 4.0),
        ]
    )
    items = [
        KeptItem(0, 8, "", "sentence", "w"),
        KeptItem(9, 18, "", "sentence", "w"),
    ]
    assert find_duplicate_suspects(items, words) == []


def test_duplicate_suspects_catch_a_paragraph_reread_minutes_later() -> None:
    passage = "One could notice, but this is not the exact rate of the method."
    words = _words(
        [
            *_sentence(passage, 0.0),
            *_sentence("Remember the simplifications we made.", 10.0),
            *_sentence(passage, 130.0),
        ]
    )
    items = [
        KeptItem(0, 12, "", "sentence", "w"),
        KeptItem(13, 17, "", "sentence", "w"),
        KeptItem(18, 30, "", "sentence", "w"),
    ]
    assert (0, 2) in {(a, b) for a, b, _ in find_duplicate_suspects(items, words)}


def test_dropped_connections_are_retried_but_bad_requests_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voicecut import take_planner

    monkeypatch.setattr(take_planner.time, "sleep", lambda _: None)

    class FlakyBackend(ScriptedBackend):
        def __init__(self, failures: list[Exception]) -> None:
            super().__init__([{"keep": []}])
            self.failures = failures

        def generate(self, prompt: str, **kwargs: Any) -> str:
            if self.failures:
                raise self.failures.pop(0)
            return super().generate(prompt, **kwargs)

    class RemoteProtocolError(Exception):
        pass

    flaky = FlakyBackend([RemoteProtocolError("Server disconnected")] * 2)
    raw = take_planner._generate(flaky, "p", schema={}, request_id="r", media=())
    assert json.loads(raw)["keep"] == []

    broken = FlakyBackend([RuntimeError("400 INVALID_ARGUMENT: bad schema")])
    with pytest.raises(RuntimeError, match="INVALID_ARGUMENT"):
        take_planner._generate(broken, "p", schema={}, request_id="r", media=())


def test_a_review_without_a_usable_answer_leaves_its_region(tmp_path: Path) -> None:
    words = _words(
        [
            *_sentence("Pipeline stages run in order.", 0.0),
            *_sentence("Pipeline stages run in order.", 5.0),
        ]
    )
    transcript = _transcript(tmp_path / "transcript.json", words)
    kept = [_item(0, 4, "Pipeline stages run in order."), _item(5, 9, "Again.")]
    backend = ScriptedBackend([{"keep": kept}, *({"keep": []} for _ in range(3))])
    plan = run_take_planner(
        transcript_path=transcript,
        output_dir=tmp_path / "plan",
        backend=backend,
        audio_path=None,
    )
    # The window kept both takes; the review (empty answers) could not decide.
    assert [(item["first_id"], item["last_id"]) for item in plan["items"]] == [
        (0, 4),
        (5, 9),
    ]
    assert "region unchanged" in plan["review"][0]["warnings"][0]
    assert plan["status"] == "complete"
