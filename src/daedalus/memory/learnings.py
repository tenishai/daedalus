"""The learnings system task: propose what people told the agent that day,
for the room's owner-approval queue (daily-learnings spec, 2026-09-01).
Pure functions here; the dispatch lives on Agent (_handle_learnings)
beside the other system-task handlers.

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
MAX_CANDIDATES = 5
STATEMENT_MAX_CHARS = 160
EVIDENCE_MAX_CHARS = 200
SAID_BY_MAX_CHARS = 120
WHERE_MAX_CHARS = 120
# Legacy already-proposed rows can be longer than STATEMENT_MAX_CHARS (the
# cap has changed over time, and rows predate this fix). Clip on render
# rather than reject, so old rows still show up in the prompt.
ALREADY_STATEMENT_MAX_CHARS = 400
# Bound the transcript fed to the model. Oldest conversations are dropped
# first when over budget -- the freshest context is the most valuable.
TRANSCRIPT_BUDGET_CHARS = 60_000

# NOTE: the instructions body below must never contain the exact phrase
# "already know" -- test_build_messages_includes_known_facts_block_only_
# when_present distinguishes the known-facts block's header ("## What you
# already know") from these instructions by that exact substring.
_INSTRUCTIONS = """\
Review your conversations from the last day, shown below. Propose up to
{max_candidates} things worth remembering permanently.

A candidate is ONLY something a person explicitly said or corrected in
these conversations: a fact they stated, a correction they made to you, a
rule or preference they told you. It is never your own conclusion, never
something you inferred by combining threads, never the output of a tool,
and never a general lesson you drew. If no person said it, it is not a
candidate.

Write each one as a single plain sentence, at most {statement_max}
characters, specific enough to act on, in the person's meaning (not
necessarily their exact words). If the same topic came up more than once,
propose it once. Skip anything ephemeral (a deploy in progress, someone's
availability today), anything listed as recalled knowledge, and anything
listed as proposed or decided before. If today's conversation refines
something listed as pending, propose the refined sentence and name the
pending entry's id in "replaces".

Reply with ONLY a JSON object, no prose, in exactly this shape:
{{"candidates": [{{"statement": "...", "said_by": "...",
"quote": "...", "where": "..."}}]}}

- statement: the one sentence
- said_by: who said it, exactly as the conversation shows them; omit only
  if the conversation truly does not show who
- quote: their words, verbatim, at most {quote_max} characters
- where: where it was said, in plain language, e.g.
  "Slack thread, {date}"
- replaces: optional; the id of a pending entry this sentence refines

If nobody told you anything worth keeping, reply {{"candidates": []}}.
"""


def _one_line(text: str) -> str:
    """Collapse every whitespace run (newlines, tabs, carriage returns
    included) to a single space. A learning is one sentence; more to the
    point, anything rendered into a prompt line must not be able to start
    a new line and forge a heading."""
    return " ".join(text.split())


def build_messages(
    persona: str,
    conversations: list[dict],
    known: list[str],
    *,
    on_date: str,
    already: list[dict] | None = None,
) -> list[dict]:
    """The one-shot summarization prompt: persona as system (the agent
    summarizes as itself), instructions + recalled knowledge + already
    proposed/decided entries + transcript as the single user turn."""
    instructions = _INSTRUCTIONS.format(
        max_candidates=MAX_CANDIDATES,
        statement_max=STATEMENT_MAX_CHARS,
        quote_max=EVIDENCE_MAX_CHARS,
        date=on_date,
    )
    parts = [instructions]
    if known:
        parts.append(
            "## What you already know (do not re-propose)\n"
            + "\n".join(f"- {item}" for item in known)
        )
    _ALREADY_STATUSES = ("pending", "remembered", "skipped")
    listed = []
    for entry in already or []:
        if not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("id"), int) or isinstance(entry.get("id"), bool):
            continue
        if entry.get("status") not in _ALREADY_STATUSES:
            continue
        if not isinstance(entry.get("statement"), str):
            continue
        statement = _one_line(entry["statement"])[:ALREADY_STATEMENT_MAX_CHARS]
        if not statement:
            continue
        listed.append(f"- [{entry['id']}, {entry['status']}] {statement}")
    if listed:
        parts.append("## Already proposed or decided (do not repeat)\n" + "\n".join(listed))
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
    for item in raw:
        if len(candidates) == MAX_CANDIDATES:
            break
        if not isinstance(item, dict):
            continue
        statement = _one_line(_string_field(item, "statement"))
        if not statement or len(statement) > STATEMENT_MAX_CHARS:
            continue
        candidate = {
            "statement": statement,
            "said_by": _one_line(_string_field(item, "said_by"))[:SAID_BY_MAX_CHARS],
            "quote": _one_line(_string_field(item, "quote"))[:EVIDENCE_MAX_CHARS],
            "where": _one_line(_string_field(item, "where"))[:WHERE_MAX_CHARS],
        }
        replaces = item.get("replaces")
        if isinstance(replaces, int) and not isinstance(replaces, bool):
            candidate["replaces"] = replaces
        candidates.append(candidate)
    return candidates
