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
from daedalus.memory.teach import FILENAME_KEY, SYSTEM_TASK_KEY, TEACH_MARKER
from daedalus.task import VERIFIED_SUB_KEY, ReasonCode, Task, TaskState

techne = pytest.importorskip("techne", reason="techne is not installed")
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
    text="The dog is named Tsuru.", *, sub="service:room", filename="chat-20260820-dog.md"
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


async def test_teach_refused_for_colleague_sub(tmp_path, techne_db):
    agent = _techne_agent(tmp_path, techne_db)

    result = await agent.handle_task(_teach_task(sub="agent:ward"))

    assert result.agent_text() == (
        "Teaching is operator-only. This connection is not an operator door."
    )
    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.POLICY_VIOLATION
    assert _all_rows(techne_db) == []


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
