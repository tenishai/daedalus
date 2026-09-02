"""The learnings system task: summarize the journal's last day into
candidate memory snippets for the room's owner-approval queue
(daily-learnings spec, 2026-09-01). Pure functions here; the dispatch
lives on Agent (_handle_learnings) beside the other system-task handlers.

The reply is JSON in the task's reply text: {"candidates": [...]} -- the
a2a envelope carries reply text verbatim (transports/a2a.task_to_a2a), so
the room json.loads it back out on its own side, and re-validates every
field itself (two independent walls, kobo never trusts pod output).

Nothing here writes to techne. Proposing is not learning; only the room's
approval flow (which rides the teach lane) ever stores anything.
"""

from __future__ import annotations

import json

LEARNINGS_MARKER = "learnings"
MAX_CANDIDATES = 10
CONTENT_MAX_CHARS = 400
EVIDENCE_MAX_CHARS = 200
SOURCE_MAX_CHARS = 512
KINDS = ("fact", "procedure")
# Bound the transcript fed to the model. Oldest conversations are dropped
# first when over budget -- the freshest context is the most valuable.
TRANSCRIPT_BUDGET_CHARS = 60_000

# NOTE: the instructions body below must never contain the exact phrase
# "already know" -- test_build_messages_includes_known_facts_block_only_
# when_present distinguishes the known-facts block's header ("## What you
# already know") from these instructions by that exact substring.
_INSTRUCTIONS = """\
Review your conversations from the last day, shown below. Propose up to
{max_candidates} candidate memory snippets: durable facts or procedures you
learned that are worth keeping permanently. Skip anything you have seen
before (your recalled knowledge is listed when present), anything ephemeral
(a deploy in progress, someone's availability today), and anything you were
explicitly taught already.

Reply with ONLY a JSON object, no prose, in exactly this shape:
{{"candidates": [{{"content": "...", "kind": "fact",
"source": "...", "evidence": "..."}}]}}

- content: the fact or procedure, at most {content_max} characters
- kind: "fact" or "procedure"
- source: where it came from, in plain language, e.g.
  "Slack thread with jsmith, {date}"
- evidence: a short verbatim quote from the conversation supporting it,
  at most {evidence_max} characters

If nothing from the day is worth keeping, reply {{"candidates": []}}.
"""


def build_messages(
    persona: str,
    conversations: list[dict],
    known: list[str],
    *,
    on_date: str,
) -> list[dict]:
    """The one-shot summarization prompt: persona as system (the agent
    summarizes as itself), instructions + recalled knowledge + transcript
    as the single user turn."""
    instructions = _INSTRUCTIONS.format(
        max_candidates=MAX_CANDIDATES,
        content_max=CONTENT_MAX_CHARS,
        evidence_max=EVIDENCE_MAX_CHARS,
        date=on_date,
    )
    parts = [instructions]
    if known:
        parts.append(
            "## What you already know (do not re-propose)\n"
            + "\n".join(f"- {item}" for item in known)
        )
    parts.append(
        "## Conversations from the last day\n" + _render_transcript(conversations)
    )
    return [
        {"role": "system", "content": persona},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _render_transcript(conversations: list[dict]) -> str:
    """Newest-preserving render: sections are gathered newest-first until
    the budget is spent, then emitted oldest-first so the model reads in
    chronological order. At least one section always survives, but the
    budget is a real cap -- even the newest section is clipped to whatever
    of TRANSCRIPT_BUDGET_CHARS remains rather than appended unmeasured."""
    sections: list[str] = []
    spent = 0
    for conv in reversed(conversations):
        lines = [f"### Conversation {conv.get('context_id', '?')}"]
        for message in conv.get("history", []):
            lines.append(f"{message.get('role', '?')}: {message.get('text', '')}")
        section = "\n".join(lines)
        remaining = TRANSCRIPT_BUDGET_CHARS - spent
        if remaining <= 0:
            break
        if len(section) > remaining:
            if sections:
                break
            section = section[:remaining]
        sections.append(section)
        spent += len(section)
    return "\n\n".join(reversed(sections))


def _string_field(item: dict, key: str) -> str:
    """A candidate field, accepted only when the model actually gave a
    string. `str(item.get(key, ""))` would turn a JSON null into the text
    "None" and a list into "['a', 'b']" -- both silently pass through as
    if they were real content. A non-string value reads as missing
    instead, same as the field being absent."""
    value = item.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def parse_candidates(text: str) -> list[dict]:
    """The model's reply -> validated candidate dicts. Garbage in (no JSON,
    wrong shape, missing fields) reads as no candidates, never an error --
    the nightly run must survive any model output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        inner = cleaned.split("```")
        cleaned = inner[1] if len(inner) > 1 else ""
        cleaned = cleaned.removeprefix("json").strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return []
    raw = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    candidates: list[dict] = []
    for item in raw[:MAX_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        content = _string_field(item, "content")
        kind = _string_field(item, "kind")
        source = _string_field(item, "source")
        evidence = _string_field(item, "evidence")
        if not content or kind not in KINDS or not source:
            continue
        candidates.append(
            {
                "content": content[:CONTENT_MAX_CHARS],
                "kind": kind,
                "source": source[:SOURCE_MAX_CHARS],
                "evidence": evidence[:EVIDENCE_MAX_CHARS],
            }
        )
    return candidates
