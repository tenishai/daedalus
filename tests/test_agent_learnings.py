"""Agent dispatch of the learnings system task."""

import json

from daedalus.memory.journal import journal_path, record
from daedalus.memory.learnings import LEARNINGS_MARKER
from daedalus.memory.teach import SYSTEM_TASK_KEY
from daedalus.task import VERIFIED_SUB_KEY, Task, TaskState
from tests.fakes import ScriptedLLM
from tests.test_agent import make_agent
from tests.test_agent_journal import FakeMemory


def _learnings_task(*, sub="service:room", date="2026-09-01"):
    task = Task.from_text("Summarize your conversations.")
    task.metadata[SYSTEM_TASK_KEY] = LEARNINGS_MARKER
    task.metadata["date"] = date
    if sub is not None:
        task.metadata[VERIFIED_SUB_KEY] = sub
    return task


GOOD_REPLY = json.dumps(
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


async def test_unauthorized_learnings_refused(tmp_path):
    agent = make_agent(llm=ScriptedLLM(), memory=FakeMemory(tmp_path / "techne.db"))
    result = await agent.handle_task(_learnings_task(sub=None))

    assert result.state is TaskState.FAILED


async def test_empty_journal_short_circuits_without_model_call(tmp_path):
    # ScriptedLLM pops from an empty list and returns "(script exhausted)"
    # rather than raising -- so the no-model-call claim is verified directly
    # against its calls log, not by expecting an exception.
    llm = ScriptedLLM()
    agent = make_agent(llm=llm, memory=FakeMemory(tmp_path / "techne.db"))
    result = await agent.handle_task(_learnings_task())

    assert result.state is TaskState.COMPLETED
    assert json.loads(result.agent_text()) == {"candidates": []}
    assert llm.calls == []


async def test_learnings_summarizes_journal_into_candidates(tmp_path):
    db = tmp_path / "techne.db"
    record(
        journal_path(db),
        "slack:C1:171",
        [{"role": "user", "text": "the canary gate is manual now"}],
    )
    agent = make_agent(llm=ScriptedLLM(GOOD_REPLY), memory=FakeMemory(db))

    result = await agent.handle_task(_learnings_task())

    assert result.state is TaskState.COMPLETED
    parsed = json.loads(result.agent_text())
    assert len(parsed["candidates"]) == 1
    assert parsed["candidates"][0]["content"] == "the canary gate is manual"


async def test_model_garbage_reads_as_empty_candidates(tmp_path):
    db = tmp_path / "techne.db"
    record(journal_path(db), "c", [{"role": "user", "text": "hello"}])
    agent = make_agent(
        llm=ScriptedLLM("nothing structured here"), memory=FakeMemory(db)
    )

    result = await agent.handle_task(_learnings_task())

    assert result.state is TaskState.COMPLETED
    assert json.loads(result.agent_text()) == {"candidates": []}
