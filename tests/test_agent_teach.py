"""RED-first tests for Task 3: the agent's pre-model teach interception.

A teach task (daedalus.memory.teach.is_teach_task) short-circuits
Agent.handle_task entirely -- above the enforcement locked check, before
run_pre_task, before recall, before the model loop -- writes straight into
techne via apply_teach, and fires the existing in-session revision pass
when memory.revision is configured. Mirrors tests/test_memory_teach.py's
fixture/helper style; guards techne the same way.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from daedalus.agent import Agent
from daedalus.memory.teach import (
    FILENAME_KEY,
    KNOWLEDGE_MARKER,
    SYSTEM_TASK_KEY,
    TEACH_MARKER,
    TOOL_MARKER,
)
from daedalus.task import VERIFIED_SUB_KEY, ReasonCode, Task, TaskState

# NOT ONE SKIP — 22. A module-level importorskip collapses this whole file
# into a single "s" in pytest output, so the gap reads as 1 skipped test when
# it is 22 (daedalus#48). The reason string is the only place that number can
# be seen, so it says it; `pytest -rs` prints it.
techne = pytest.importorskip(
    "techne",
    reason="techne is not installed — 22 tests in this file did not run",
)
from techne.core import knowledge  # noqa: E402
from techne.core.database import init_db  # noqa: E402

from daedalus.memory.techne import TechneMemoryStore  # noqa: E402


class _NoCallLLM:
    """Fails the test loudly if the model loop is ever reached."""

    async def complete(self, messages, tools=None):
        raise AssertionError("model must not be called")


class _BlockingEnforcement:
    """Stands in for a locked Enforcement whose hooks would block anything.

    Teach interception sits above the locked check by design, so neither
    hook should ever be invoked for a teach task."""

    locked = True
    active = True
    load_error = ("blocked.py", "boom")

    async def run_pre_task(self, message):
        raise AssertionError("enforcement must not run for teach tasks")

    async def run_post_draft(self, draft):
        raise AssertionError("enforcement must not run for teach tasks")


def _bundle(tmp_path: Path, memory_yaml: str = "") -> Path:
    (tmp_path / "agent.yaml").write_text(f"name: x\nmodel:\n  id: m\n{memory_yaml}")
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    return tmp_path


def _techne_agent(tmp_path: Path, db_path: Path, **overrides) -> Agent:
    root = _bundle(tmp_path, "memory:\n  personal: techne\n")
    overrides.setdefault("llm", _NoCallLLM())
    overrides.setdefault("memory", TechneMemoryStore(db_path=db_path))
    return Agent.load(root, **overrides)


def _teach_task(
    text="The dog is named Tsuru.", *, sub="service:kobo", filename="chat-20260820-dog.md"
):
    task = Task.from_text(text)
    task.metadata[SYSTEM_TASK_KEY] = TEACH_MARKER
    task.metadata[FILENAME_KEY] = filename
    if sub is not None:
        task.metadata[VERIFIED_SUB_KEY] = sub
    return task


def _all_rows(db_path: Path) -> list[dict]:
    return knowledge.list_entries(db_path, limit=10_000_000, include_superseded=True)


@pytest.fixture()
def techne_db(tmp_path):
    db = tmp_path / "techne.db"
    init_db(db).close()
    return db


async def test_teach_task_stores_and_replies_learned(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)

    result = await agent.handle_task(_teach_task())

    assert result.agent_text() == "Learned. I will remember that."
    assert result.state is TaskState.COMPLETED
    rows = [e for e in _all_rows(techne_db) if e.get("source", "").startswith("intake:")]
    assert len(rows) == 1


async def test_teach_never_reaches_model_or_enforcement(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)
    agent.enforcement = _BlockingEnforcement()

    result = await agent.handle_task(_teach_task())

    assert result.agent_text() == "Learned. I will remember that."
    assert result.state is TaskState.COMPLETED
    rows = [e for e in _all_rows(techne_db) if e.get("source", "").startswith("intake:")]
    assert len(rows) == 1


async def test_teach_refused_for_a_prefixed_agent_sub(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)

    result = await agent.handle_task(_teach_task(sub="agent:ward"))

    assert result.agent_text() == (
        "Teaching is operator-only. This connection is not an operator door."
    )
    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.POLICY_VIOLATION
    assert _all_rows(techne_db) == []


async def test_teach_refused_for_a_PERSONA_sub_which_is_what_cabal_actually_mints(
    tmp_path, techne_db
):
    """THE #43 REGRESSION, and the one the old gate let straight through.

    The gate refused `agent:` and cabal has never minted that. Its two
    bootstrap shapes are `operator:<id>` and `persona:<name>`
    (cabal/core/routes/auth.py:75-79), and the dispatch re-mint propagates
    whichever started the chain -- so a colleague agent arrives here as
    `persona:<name>` and, before this test, was operator-grade.

    That matters beyond one forged fact. apply_teach writes the source as
    `intake:<caller-supplied filename>`, and memory/revision.py auto-promotes
    the `intake:chat-` slice WITHOUT human review, retiring whatever it
    contradicts -- though only when `memory.revision` is enabled, which is off
    by default. `apply_knowledge_delete` needs no such setting: it retires
    entries directly, returning before the revision pass the write branches
    make. So the reachable consequence was an agent retiring another agent's
    beliefs through the lane built to keep that human-gated, and the leg that
    does not depend on any config is the delete (#49 r2).
    """
    agent = _techne_agent(tmp_path, techne_db)

    result = await agent.handle_task(_teach_task(sub="persona:scout"))

    assert result.agent_text() == (
        "Teaching is operator-only. This connection is not an operator door."
    )
    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.POLICY_VIOLATION
    assert _all_rows(techne_db) == []


async def test_teach_refused_for_the_bare_operator_sub_that_means_nobody_identified(
    tmp_path, techne_db
):
    """cabal re-mints a sub-less token as the literal string "operator"
    (`sub=claims.get("sub", "operator")`, cabal#184) -- not `operator:<id>`,
    just the word. A bearer that failed to identify itself must not land one
    character from the most privileged prefix in the fleet."""
    agent = _techne_agent(tmp_path, techne_db)

    result = await agent.handle_task(_teach_task(sub="operator"))

    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.POLICY_VIOLATION
    assert _all_rows(techne_db) == []


async def test_an_operator_prefixed_sub_still_teaches(tmp_path, techne_db):
    """The fix must not close the door it exists to keep open: cabal's real
    operator shape (`operator:<id>`) is still admitted."""
    agent = _techne_agent(tmp_path, techne_db)

    result = await agent.handle_task(_teach_task(sub="operator:richard"))

    assert result.state is not TaskState.FAILED
    assert [e for e in _all_rows(techne_db) if e.get("source", "").startswith("intake:")]


async def test_the_gate_records_the_sub_it_ADMITTED_not_only_the_ones_it_refused(
    tmp_path, techne_db
):
    """The allow event carries the sub it admitted (#47, #195).

    kobo's sub is deployment-chosen (`CABAL_OPERATOR_TOKEN`, propagated by
    cabal's re-mint). The default allowlist names what kobo's docs mint,
    `service:kobo`; a deployment that differs reads the sub off
    `<lane>_refused` and lists it in `auth.operator_subs`. Both events are
    pinned rather than left as a convenience."""
    seen: list[dict] = []
    agent = _techne_agent(tmp_path, techne_db, on_event=seen.append)

    await agent.handle_task(_teach_task(sub="service:kobo"))

    allowed = [e for e in seen if e["type"] == "teach_allowed"]
    assert allowed == [{"type": "teach_allowed", "sub": "service:kobo"}]


async def test_teach_refused_without_verified_sub(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)

    result = await agent.handle_task(_teach_task(sub=None))

    assert result.agent_text() == (
        "Teaching is operator-only. This connection is not an operator door."
    )
    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.POLICY_VIOLATION
    assert _all_rows(techne_db) == []


async def test_teach_without_memory_replies_no_memory(tmp_path):
    root = _bundle(tmp_path)  # memory.personal defaults to "none" -> NoMemory
    agent = Agent.load(root, llm=_NoCallLLM())

    result = await agent.handle_task(_teach_task())

    assert result.agent_text() == "I have no personal memory configured, so I cannot keep this."
    assert result.state is TaskState.FAILED


async def test_teach_already_known_copy(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)

    first = await agent.handle_task(_teach_task())
    second = await agent.handle_task(_teach_task())

    assert first.agent_text() == "Learned. I will remember that."
    assert second.agent_text() == "I already know this one. Nothing new to keep."
    assert second.state is TaskState.COMPLETED


async def test_teach_fires_revision_pass_and_serializes(tmp_path, monkeypatch, techne_db):
    root = _bundle(tmp_path, "memory:\n  revision: operator\n")
    agent = Agent.load(root, llm=_NoCallLLM(), memory=TechneMemoryStore(db_path=techne_db))

    calls: list[tuple[float, float]] = []

    async def fake_run_revision_pass(
        db_path, model, *, policy, emit, colleagues=None, reask_cooldowns=None
    ):
        start = time.monotonic()
        await asyncio.sleep(0.01)
        calls.append((start, time.monotonic()))

    monkeypatch.setattr("daedalus.memory.revision.run_revision_pass", fake_run_revision_pass)

    fired: list[asyncio.Task] = []
    original = agent._start_revision_pass

    def spy():
        task = original()
        if task is not None:
            fired.append(task)
        return task

    monkeypatch.setattr(agent, "_start_revision_pass", spy)

    result1 = await agent.handle_task(
        _teach_task(text="Fact one.", filename="chat-20260820-one.md")
    )
    result2 = await agent.handle_task(
        _teach_task(text="Fact two.", filename="chat-20260820-two.md")
    )

    assert result1.agent_text() == "Learned. I will remember that."
    assert result2.agent_text() == "Learned. I will remember that."
    assert len(fired) == 2

    await asyncio.gather(*fired)

    assert len(calls) == 2
    (_, first_end), (second_start, _) = sorted(calls)
    assert first_end <= second_start  # never interleaved: the lock serialized them


async def test_teach_invalid_filename_replies_error(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)

    result = await agent.handle_task(_teach_task(filename="../evil.md"))

    assert result.state is TaskState.FAILED
    assert "invalid teach filename" in result.agent_text()
    assert _all_rows(techne_db) == []


# ---- the marker dispatch: knowledge, tool (stub), and unknown markers ----


def _knowledge_task(
    text="The dog is named Tsuru.",
    *,
    sub="service:kobo",
    filename="chat-20260820-dog.md",
    delete=False,
):
    task = Task.from_text(text)
    task.metadata[SYSTEM_TASK_KEY] = KNOWLEDGE_MARKER
    task.metadata[FILENAME_KEY] = filename
    if delete:
        from daedalus.memory.teach import DELETE_KEY

        task.metadata[DELETE_KEY] = True
    if sub is not None:
        task.metadata[VERIFIED_SUB_KEY] = sub
    return task


async def test_knowledge_task_stores_and_replies_learned(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)

    result = await agent.handle_task(_knowledge_task())

    assert result.agent_text() == "Learned. I will remember that."
    assert result.state is TaskState.COMPLETED
    rows = [e for e in _all_rows(techne_db) if e.get("source", "").startswith("intake:")]
    assert len(rows) == 1


async def test_knowledge_task_never_reaches_model_or_enforcement(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)
    agent.enforcement = _BlockingEnforcement()

    result = await agent.handle_task(_knowledge_task())

    assert result.agent_text() == "Learned. I will remember that."
    assert result.state is TaskState.COMPLETED


async def test_knowledge_task_edit_retires_the_old_fact(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)

    await agent.handle_task(_knowledge_task(text="The dog is named Tsuru."))
    result = await agent.handle_task(_knowledge_task(text="The dog is named Kestrel."))

    assert result.agent_text() == "Learned. I will remember that."
    live = knowledge.list_entries(techne_db, limit=10_000_000)
    contents = [e["content"] for e in live]
    assert not any("Tsuru" in c for c in contents)
    assert any("Kestrel" in c for c in contents)


async def test_knowledge_task_delete_removes_recall(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)

    await agent.handle_task(_knowledge_task(text="The dog is named Tsuru."))
    result = await agent.handle_task(_knowledge_task(delete=True))

    assert result.state is TaskState.COMPLETED
    live = knowledge.list_entries(techne_db, limit=10_000_000)
    assert not any("Tsuru" in e["content"] for e in live)


async def test_knowledge_refused_for_a_prefixed_agent_sub(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)

    result = await agent.handle_task(_knowledge_task(sub="agent:ward"))

    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.POLICY_VIOLATION
    assert _all_rows(techne_db) == []


async def test_tool_marker_without_a_filename_never_reaches_model(tmp_path, techne_db):
    """TOOL_MARKER's real handler lands in tests/test_agent_tool.py (arc3d
    Task 5); this file just keeps the "never falls through to the model"
    guarantee pinned for the marker family as a whole. No memory.home is
    configured on this techne-only bundle, so the handler's own
    no-persistent-volume refusal is the exact path exercised here."""
    agent = _techne_agent(tmp_path, techne_db)
    task = Task.from_text("do something")
    task.metadata[SYSTEM_TASK_KEY] = TOOL_MARKER

    result = await agent.handle_task(task)

    assert result.state is TaskState.FAILED


async def test_unknown_system_task_marker_is_refused_and_never_reaches_model(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)
    task = Task.from_text("hi")
    task.metadata[SYSTEM_TASK_KEY] = "reboot"

    result = await agent.handle_task(task)

    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.POLICY_VIOLATION


@pytest.mark.parametrize(
    "marker,lane",
    [(TEACH_MARKER, "teach"), (KNOWLEDGE_MARKER, "knowledge"), (TOOL_MARKER, "tool")],
)
async def test_the_operator_gate_event_NAMES_are_pinned(tmp_path, techne_db, marker, lane):
    """`_operator_gate`'s docstring promises these names are stable, and
    nothing pinned them (#49 r1).

    Prudence renamed all three refusal events at once and the suite stayed
    green at 399 passed — `teach_allowed` was the only one of the six names
    with a test, and only because it was pinned deliberately. The kaminos
    trace pane consumes this stream by name, so a rename is a silent break
    for a consumer in another repo: exactly the kind of cross-repo coupling
    that produced #43 in the first place.
    """
    seen: list[dict] = []
    agent = _techne_agent(tmp_path, techne_db, on_event=seen.append)

    task = _teach_task(sub="persona:scout")  # refused
    task.metadata[SYSTEM_TASK_KEY] = marker
    await agent.handle_task(task)

    assert {"type": f"{lane}_refused", "sub": "persona:scout"} in seen

    allowed = _teach_task(sub="service:kobo")
    allowed.metadata[SYSTEM_TASK_KEY] = marker
    await agent.handle_task(allowed)

    assert {"type": f"{lane}_allowed", "sub": "service:kobo"} in seen

