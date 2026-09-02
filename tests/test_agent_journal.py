"""Completed conversations land in the journal; failures and system tasks
do not; a journal error never fails the task."""

from daedalus.memory.journal import recent
from daedalus.memory.learnings import LEARNINGS_MARKER
from daedalus.memory.teach import SYSTEM_TASK_KEY
from daedalus.task import ImagePart, Task, TaskState
from tests.fakes import ScriptedLLM
from tests.test_agent import make_agent


class FakeMemory:
    """Just enough memory seam: a db_path for journal_path derivation and
    inert recall/commit."""

    def __init__(self, db_path):
        self.db_path = db_path

    async def recall(self, query, k=5):
        return []

    async def commit(self, content, kind="fact"):
        return None


async def test_completed_task_journals_conversation(tmp_path):
    agent = make_agent(
        llm=ScriptedLLM("hello there"), memory=FakeMemory(tmp_path / "techne.db")
    )
    task = Task.from_text("hi")

    result = await agent.handle_task(task)

    assert result.state is TaskState.COMPLETED
    rows = recent(tmp_path / "journal.db")
    assert len(rows) == 1
    assert rows[0]["history"][0]["text"] == "hi"
    assert any(m["role"] == "agent" for m in rows[0]["history"])


async def test_agent_without_memory_db_does_not_journal(tmp_path):
    agent = make_agent(llm=ScriptedLLM("hello"))  # NoMemory: no db_path
    task = Task.from_text("hi")

    result = await agent.handle_task(task)

    assert result.state is TaskState.COMPLETED
    assert not (tmp_path / "journal.db").exists()


async def test_journal_failure_never_fails_the_task(tmp_path, monkeypatch):
    agent = make_agent(
        llm=ScriptedLLM("hello"), memory=FakeMemory(tmp_path / "techne.db")
    )

    def boom(*args, **kwargs):
        raise OSError("disk full")

    import daedalus.memory.journal as journal_module

    monkeypatch.setattr(journal_module, "record", boom)
    result = await agent.handle_task(Task.from_text("hi"))

    assert result.state is TaskState.COMPLETED


async def test_system_task_never_journals(tmp_path):
    """handle_task dispatches a system-task-marked Task before the model
    path and returns directly from _dispatch_system_task -- it never
    reaches the post-completion journaling hook, no matter how the system
    task itself resolves. Here the learnings task is refused outright (no
    VERIFIED_SUB_KEY), but the invariant under test is the journal, not
    the refusal."""
    agent = make_agent(
        llm=ScriptedLLM("hello"), memory=FakeMemory(tmp_path / "techne.db")
    )
    task = Task.from_text("Summarize your conversations.")
    task.metadata[SYSTEM_TASK_KEY] = LEARNINGS_MARKER

    result = await agent.handle_task(task)

    assert result.state is TaskState.FAILED
    assert recent(tmp_path / "journal.db") == []
    assert not (tmp_path / "journal.db").exists()


async def test_journal_strips_image_bytes_and_leaves_a_marker(tmp_path):
    agent = make_agent(
        llm=ScriptedLLM("nice photo"), memory=FakeMemory(tmp_path / "techne.db")
    )
    task = Task.from_text("look at this")
    task.history[0].images = [ImagePart(data="QUFB" * 1000, media_type="image/png")]

    result = await agent.handle_task(task)

    assert result.state is TaskState.COMPLETED
    rows = recent(tmp_path / "journal.db")
    entry = rows[0]["history"][0]
    assert "images" not in entry
    assert "QUFB" not in str(rows[0]["history"])
    assert entry["text"] == "look at this [shared 1 image(s)]"
