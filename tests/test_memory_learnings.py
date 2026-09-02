"""The learnings summarizer's pure halves: prompt assembly and reply
parsing. No techne, no model, no sqlite."""

import json

from daedalus.memory.learnings import (
    CONTENT_MAX_CHARS,
    MAX_CANDIDATES,
    TRANSCRIPT_BUDGET_CHARS,
    _render_transcript,
    build_messages,
    parse_candidates,
)

CONVS = [
    {
        "context_id": "slack:C1:171",
        "history": [
            {"role": "user", "text": "the canary gate is manual now"},
            {"role": "agent", "text": "noted"},
        ],
        "created_at": "2026-09-01T08:00:00+00:00",
    }
]


def test_build_messages_persona_is_system_and_transcript_in_user():
    messages = build_messages("You are wren.", CONVS, [], on_date="2026-09-01")

    assert messages[0] == {"role": "system", "content": "You are wren."}
    assert messages[1]["role"] == "user"
    assert "the canary gate is manual now" in messages[1]["content"]
    assert "2026-09-01" in messages[1]["content"]


def test_build_messages_includes_known_facts_block_only_when_present():
    without = build_messages("p", CONVS, [], on_date="2026-09-01")
    with_known = build_messages("p", CONVS, ["water is wet"], on_date="2026-09-01")

    assert "already know" not in without[1]["content"]
    assert "water is wet" in with_known[1]["content"]


def test_build_messages_budget_drops_oldest_first():
    big = "x" * 40_000
    convs = [
        {"context_id": f"c{i}", "history": [{"role": "user", "text": big}]}
        for i in range(3)
    ]
    messages = build_messages("p", convs, [], on_date="2026-09-01")

    body = messages[1]["content"]
    assert "c2" in body  # newest survives
    assert "c0" not in body  # oldest dropped


def test_render_transcript_caps_even_the_lone_newest_section():
    # A single conversation bigger than the whole budget used to be
    # appended unmeasured (the `sections and ...` guard exempted the
    # first/newest section from the cap entirely). The budget must be a
    # real cap: the newest conversation is always included, but never
    # longer than TRANSCRIPT_BUDGET_CHARS.
    huge = "x" * (TRANSCRIPT_BUDGET_CHARS + 10_000)
    convs = [{"context_id": "only", "history": [{"role": "user", "text": huge}]}]

    transcript = _render_transcript(convs)

    assert "only" in transcript
    assert len(transcript) <= TRANSCRIPT_BUDGET_CHARS


def test_parse_candidates_valid_json():
    reply = json.dumps(
        {
            "candidates": [
                {
                    "content": "the canary gate is manual",
                    "kind": "fact",
                    "source": "Slack thread, Sep 1",
                    "evidence": "gate is manual now",
                }
            ]
        }
    )
    parsed = parse_candidates(reply)

    assert len(parsed) == 1
    assert parsed[0]["kind"] == "fact"


def test_parse_candidates_strips_code_fences():
    reply = '```json\n{"candidates": [{"content": "c", "kind": "fact", "source": "s"}]}\n```'
    assert len(parse_candidates(reply)) == 1


def test_parse_candidates_garbage_reads_as_empty():
    assert parse_candidates("I could not find anything today.") == []
    assert parse_candidates("") == []
    assert parse_candidates('{"candidates": "nope"}') == []


def test_parse_candidates_drops_invalid_and_clips_long():
    reply = json.dumps(
        {
            "candidates": [
                {"content": "", "kind": "fact", "source": "s"},  # empty content
                {"content": "c", "kind": "opinion", "source": "s"},  # bad kind
                {"content": "c", "kind": "fact", "source": ""},  # no source
                {"content": None, "kind": "fact", "source": "s"},  # non-string
                {"content": "y" * 999, "kind": "fact", "source": "s"},  # clipped
            ]
        }
    )
    parsed = parse_candidates(reply)

    assert len(parsed) == 1
    assert len(parsed[0]["content"]) == CONTENT_MAX_CHARS


def test_parse_candidates_caps_count():
    reply = json.dumps(
        {
            "candidates": [
                {"content": f"c{i}", "kind": "fact", "source": "s"} for i in range(30)
            ]
        }
    )
    assert len(parse_candidates(reply)) == MAX_CANDIDATES
