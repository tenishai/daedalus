"""Budget (702), checkpoints (save/drop/resume), and the techne memory seam."""

from pathlib import Path

import pytest

from daedalus.agent import Agent
from daedalus.budget import TokenBudget
from daedalus.bundle import Bundle, BundleError
from daedalus.checkpoints import SqliteCheckpoints
from daedalus.protocols import LLMResponse, ToolCall
from daedalus.task import ReasonCode, Task, TaskState
from tests.fakes import FakeTools, ScriptedLLM

READ_SPEC = {"name": "files.read", "description": "", "parameters": {}}


def write_bundle(tmp_path, extra_yaml: str = "") -> Path:
    (tmp_path / "agent.yaml").write_text("name: x\nmodel:\n  id: m\n" + extra_yaml)
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    return tmp_path


def tool_turn(i: int) -> LLMResponse:
    return LLMResponse(
        text="",
        tool_calls=[ToolCall(id=str(i), name="files__read", arguments={})],
        input_tokens=10,
        output_tokens=5,
    )


# --- budget -----------------------------------------------------------------


def test_token_budget_arithmetic():
    budget = TokenBudget(100)
    budget.spend(60)
    assert budget.remaining() == 40
    budget.spend(60)
    assert budget.remaining() == 0


async def test_budget_exhaustion_fails_with_702_and_keeps_checkpoint(tmp_path):
    bundle = write_bundle(
        tmp_path,
        "  budget:\n    per_task_tokens: 25\n"  # two 15-token turns exhaust it
        "tools:\n  allow: ['files.*']\n",
    )
    llm = ScriptedLLM(tool_turn(1), tool_turn(2), "never reached")
    store = SqliteCheckpoints(tmp_path / "cp.db")
    agent = Agent(
        Bundle.load(bundle), llm=llm, tools=FakeTools([READ_SPEC]), checkpoints=store
    )
    task = await agent.handle_task(Task.from_text("go"))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.BUDGET_EXHAUSTED
    assert "token budget (25)" in task.agent_text()
    assert await store.load(task.id) is not None  # kept for re-fund + resume


async def test_under_budget_completes_and_drops_checkpoint(tmp_path):
    bundle = write_bundle(tmp_path, "  budget:\n    per_task_tokens: 10000\n")
    store = SqliteCheckpoints(tmp_path / "cp.db")
    agent = Agent(Bundle.load(bundle), llm=ScriptedLLM("done"), checkpoints=store)
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.state is TaskState.COMPLETED
    assert await store.load(task.id) is None


async def test_budget_events_are_emitted(tmp_path):
    bundle = write_bundle(tmp_path, "  budget:\n    per_task_tokens: 100\n")
    agent = Agent(Bundle.load(bundle), llm=ScriptedLLM("done"))
    events: list[dict] = []
    agent.on_event = events.append
    await agent.handle_task(Task.from_text("hi"))
    budget_events = [e for e in events if e["type"] == "budget"]
    assert budget_events == [{"type": "budget", "spent": 15, "remaining": 85}]


# --- checkpoints ------------------------------------------------------------


async def test_sqlite_checkpoints_round_trip(tmp_path):
    store = SqliteCheckpoints(tmp_path / "cp.db")
    await store.save("t1", {"messages": [{"role": "user", "content": "hi"}]})
    await store.save("t1", {"messages": [{"role": "user", "content": "updated"}]})
    loaded = await store.load("t1")
    assert loaded["messages"][0]["content"] == "updated"
    await store.drop("t1")
    assert await store.load("t1") is None


async def test_resume_continues_from_checkpointed_messages(tmp_path):
    bundle = write_bundle(tmp_path, "  budget:\n    per_task_tokens: 25\n")
    store = SqliteCheckpoints(tmp_path / "cp.db")
    llm = ScriptedLLM(tool_turn(1), tool_turn(2))
    agent = Agent(
        Bundle.load(bundle),
        llm=llm,
        tools=FakeTools([READ_SPEC], {"files.read": "partial work"}),
        checkpoints=store,
    )
    # the tool ceiling is empty here (no allow) — but budget dies first anyway
    failed = await agent.handle_task(Task.from_text("go"))
    assert failed.reason_code is ReasonCode.BUDGET_EXHAUSTED

    # re-fund: fresh agent (fresh budget), same store, finish the task
    class PublishCounter:
        published = 0

        async def recall(self, query, k=5):
            return []

        async def commit(self, content, kind="fact"):
            pass

        async def publish(self):
            self.published += 1
            return {"enqueued": 0, "synced": 0, "failed": 0}

    llm2 = ScriptedLLM("picked up where we left off")
    memory = PublishCounter()
    agent2 = Agent(Bundle.load(bundle), llm=llm2, checkpoints=store, memory=memory)
    resumed = await agent2.resume(failed.id)
    assert resumed is not None
    assert resumed.state is TaskState.COMPLETED
    assert resumed.agent_text() == "picked up where we left off"
    # the resumed LLM call saw the checkpointed conversation, not a fresh one
    assert llm2.calls[0][0]["role"] == "system"
    assert await store.load(failed.id) is None  # dropped after completion
    assert memory.published == 1  # a resumed task publishes at its end too (#228)


async def test_resume_unknown_task_returns_none(tmp_path):
    bundle = write_bundle(tmp_path)
    agent = Agent(
        Bundle.load(bundle),
        llm=ScriptedLLM(),
        checkpoints=SqliteCheckpoints(tmp_path / "cp.db"),
    )
    assert await agent.resume("nope") is None


def test_checkpoints_sqlite_wires_from_spec(tmp_path):
    bundle = write_bundle(tmp_path, "checkpoints: sqlite\n")
    agent = Agent(Bundle.load(bundle), llm=ScriptedLLM())
    assert type(agent.checkpoints).__name__ == "SqliteCheckpoints"


# --- techne seam ------------------------------------------------------------


def _techne_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("techne") is not None


def test_memory_techne_without_techne_api_fails_at_load(tmp_path, monkeypatch):
    # techne missing, or older than 0.19.1 (no techne.api): fail at load (#228).
    import sys

    monkeypatch.setitem(sys.modules, "techne.api", None)
    bundle = write_bundle(tmp_path, "memory:\n  personal: techne\n")
    with pytest.raises(BundleError, match="techne 0.19.1 or later is not installed"):
        Agent(Bundle.load(bundle), llm=ScriptedLLM())


@pytest.mark.skipif(not _techne_installed(), reason="techne not installed")
async def test_techne_store_round_trip(tmp_path, monkeypatch):
    from daedalus.memory.techne import TechneMemoryStore

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.core.database import init_db

    db_path = tmp_path / "techne.db"
    init_db(db_path)
    store = TechneMemoryStore(db_path=db_path)
    await store.commit("spark5 exports PYTHONPATH globally; always env -u PYTHONPATH")
    recalled = await store.recall("PYTHONPATH venv problem")
    assert any("PYTHONPATH" in item for item in recalled)


@pytest.mark.skipif(not _techne_installed(), reason="techne not installed")
async def test_techne_recall_returns_full_content_past_search_clip(tmp_path):
    # techne's search() clips every returned row's content to 500 chars,
    # unmarked -- a whole-file skill procedure read through that clip is a
    # summary posing as the procedure (found live on clio, 2026-08-31).
    # recall() must re-fetch the full row by id.
    from techne.core.database import init_db

    from daedalus.memory.techne import TechneMemoryStore

    db_path = tmp_path / "techne.db"
    init_db(db_path)
    store = TechneMemoryStore(db_path=db_path)
    procedure = "## Skill: long-procedure\n\nzephyr calibration steps.\n" + ("step\n" * 300)
    assert len(procedure) > 500
    await store.commit(procedure, kind="procedure")
    recalled = await store.recall("zephyr calibration")
    assert any(item == procedure for item in recalled), [len(i) for i in recalled]


async def test_checkpoint_store_closes_its_sqlite_connections(tmp_path):
    """#95: `with sqlite3.connect()` leaves the connection open, and the
    ResourceWarning that GC raised later landed in an unrelated test
    (test_bundle's no-warnings assertion). Every operation must close its own."""
    import gc
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        store = SqliteCheckpoints(tmp_path / "cp.db")
        await store.save("t1", {"n": 1})
        assert await store.load("t1") == {"n": 1}
        await store.drop("t1")
        assert await store.load("t1") is None
        del store
        gc.collect()

    leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert not leaks, [str(w.message) for w in leaks]
