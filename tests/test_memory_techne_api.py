"""TechneMemoryStore through techne.api (#228): recall reaches installed recall
sources and keeps where each hit came from; a task's tagged facts are published once
at task end."""

import json
import threading

import pytest

pytest.importorskip("techne.api", reason="needs techne 0.19.1 or later")

import techne.core.recall_sources as rs  # noqa: E402
from techne.core.archivist import ARCHIVIST_PUBLISH_TARGET, PublishPayload  # noqa: E402
from techne.sync.sinks import DeliveryResult, OutboxSink, use_registry  # noqa: E402

from daedalus.agent import Agent  # noqa: E402
from daedalus.bundle import Bundle  # noqa: E402
from daedalus.memory.techne import TechneMemoryStore  # noqa: E402
from daedalus.task import Task, TaskState  # noqa: E402
from tests.fakes import FakeTools, ScriptedLLM  # noqa: E402


class PoolSource:
    name = "liminal_archivist"
    escalation = False

    def search(self, topic, limit):
        return [{"content": f"pool fact about {topic}", "written_by": "agent-a"}]

    def health(self):
        return True


class PoolEntryPoint:
    name = "liminal_archivist"

    def load(self):
        return PoolSource


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A techne home with the pool source allowed and the publish lane on for `team`."""
    for attr in ("_LOADED", "_FAILED", "_IN_FLIGHT", "_TEAM", "_TEAM_ERROR"):
        monkeypatch.setattr(rs, attr, {})
    monkeypatch.setattr(rs, "_WARNED", set())
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "standalone: true\narchivist_publish_tags: [team]\n"
        "recall:\n  allowed_sources: [liminal_archivist]\n"
    )
    from techne.core.database import init_db

    init_db(tmp_path / "techne.db")
    return tmp_path


def _sink(delivered):
    def handler(payload):
        delivered.append(payload)
        return DeliveryResult(ok=True)

    return OutboxSink(name=ARCHIVIST_PUBLISH_TARGET, payload_schema=PublishPayload, handler=handler)


async def test_recall_hits_name_the_pool_and_the_local_store(home, monkeypatch):
    monkeypatch.setattr(rs, "entry_points", lambda group: [PoolEntryPoint()])
    store = TechneMemoryStore(home=home)
    await store.commit("the zephyr runbook lives in ops/zephyr")

    hits = await store.recall_hits("zephyr runbook")

    assert {"content": "pool fact about zephyr runbook", "source": "liminal_archivist"} in hits
    assert {"content": "the zephyr runbook lives in ops/zephyr", "source": "local"} in hits
    assert await store.recall("zephyr runbook") == [h["content"] for h in hits]


async def test_recall_keeps_full_local_content_past_the_clip(home):
    store = TechneMemoryStore(home=home)
    procedure = "## Skill: long-procedure\n\nzephyr calibration steps.\n" + ("step\n" * 300)
    await store.commit(procedure, kind="procedure")
    assert procedure in await store.recall("zephyr calibration")


async def test_recall_hits_honours_k_up_to_techne_cap(home):
    store = TechneMemoryStore(home=home)
    for n in range(14):
        await store.commit(f"zephyr fact number {n}")
    assert len(await store.recall_hits("zephyr fact", k=3)) == 3
    # techne 0.19.1 searches at most 10 local rows.
    assert len(await store.recall_hits("zephyr fact", k=12)) == 10


async def test_pool_hits_survive_a_full_local_page(home, monkeypatch):
    monkeypatch.setattr(rs, "entry_points", lambda group: [PoolEntryPoint()])
    store = TechneMemoryStore(home=home)
    for n in range(12):
        await store.commit(f"zephyr fact number {n}")
    hits = await store.recall_hits("zephyr fact", k=20)
    assert {"content": "pool fact about zephyr fact", "source": "liminal_archivist"} in hits


async def test_publish_reads_config_off_the_event_loop(home, monkeypatch):
    store = TechneMemoryStore(home=home)
    threads = []
    real = store._config

    def spy():
        threads.append(threading.current_thread() is threading.main_thread())
        return real()

    monkeypatch.setattr(store, "_config", spy)
    with use_registry({ARCHIVIST_PUBLISH_TARGET: _sink([])}):
        await store.publish()
    assert threads == [False]


async def test_recall_degrades_to_nothing_when_techne_fails(home, monkeypatch):
    import techne.api

    def boom(*a, **k):
        raise RuntimeError("store is gone")

    monkeypatch.setattr(techne.api, "recall", boom)
    assert await TechneMemoryStore(home=home).recall_hits("anything") == []


async def test_publish_sends_only_team_tagged_facts(home):
    store = TechneMemoryStore(home=home)
    await store.commit("the lab fact is 42", tags="team")
    await store.commit("a private note", tags="mine")
    delivered = []

    with use_registry({ARCHIVIST_PUBLISH_TARGET: _sink(delivered)}):
        result = await store.publish()

    assert [p["content"] for p in delivered] == ["the lab fact is 42"]
    assert (result["enqueued"], result["synced"]) == (1, 1)


def _scripted_agent(home, monkeypatch, memory):
    monkeypatch.setenv("DAEDALUS_SCRIPTED_ENGINE", "1")
    (home / "bundle").mkdir()
    b = home / "bundle"
    (b / "agent.yaml").write_text("name: x\nmodel:\n  id: m\ntools:\n  provider: none\n")
    (b / "agent-card.json").write_text("{}")
    (b / "persona.md").write_text("You are x.")
    return Agent(
        Bundle.load(b), llm=ScriptedLLM(), memory=memory, tools=FakeTools([]), engine="scripted"
    )


async def test_a_task_publishes_its_tagged_fact_once_at_task_end(home, monkeypatch):
    events = []
    agent = _scripted_agent(home, monkeypatch, TechneMemoryStore(home=home))
    agent.on_event = events.append
    delivered = []

    with use_registry({ARCHIVIST_PUBLISH_TARGET: _sink(delivered)}):
        task = await agent.handle_task(Task.from_text("remember: the lab fact is 42 #team"))
        assert task.state is TaskState.COMPLETED
        assert task.agent_text() == "remembered: the lab fact is 42 [team]"
        assert [p["content"] for p in delivered] == ["the lab fact is 42"]
        assert [e for e in events if e["type"] == "memory_published"][0]["synced"] == 1
        # The next task has nothing new to send.
        await agent.handle_task(Task.from_text("recall: lab fact"))
    assert len(delivered) == 1


async def test_scripted_recall_reports_each_hits_source(home, monkeypatch):
    monkeypatch.setattr(rs, "entry_points", lambda group: [PoolEntryPoint()])
    agent = _scripted_agent(home, monkeypatch, TechneMemoryStore(home=home))
    task = await agent.handle_task(Task.from_text("recall: lab fact"))
    hits = json.loads(task.agent_text())
    assert {"content": "pool fact about lab fact", "source": "liminal_archivist"} in hits


async def test_a_failing_publish_never_fails_the_task(home, monkeypatch):
    class BrokenPublish(TechneMemoryStore):
        async def publish(self):
            raise RuntimeError("sink exploded")

    events = []
    agent = _scripted_agent(home, monkeypatch, BrokenPublish(home=home))
    agent.on_event = events.append
    task = await agent.handle_task(Task.from_text("remember: still fine"))
    assert task.state is TaskState.COMPLETED
    assert [e["error"] for e in events if e["type"] == "memory_published"] == ["sink exploded"]


def test_use_vectors_is_deprecated(home):
    with pytest.warns(DeprecationWarning, match="use_vectors"):
        TechneMemoryStore(home=home, use_vectors=False)
