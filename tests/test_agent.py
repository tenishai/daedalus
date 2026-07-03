import warnings
from pathlib import Path

import pytest

from daedalus.agent import Agent
from daedalus.task import Task, TaskState
from tests.fakes import CannedMemory, ScriptedLLM

GOLDEN = Path(__file__).parent / "golden" / "greeter"


def make_agent(**overrides) -> Agent:
    overrides.setdefault("llm", ScriptedLLM("hello!"))
    return Agent.load(GOLDEN, **overrides)


async def test_handle_task_completes_with_llm_reply():
    agent = make_agent()
    task = await agent.handle_task(Task.from_text("hi there"))
    assert task.state is TaskState.COMPLETED
    assert task.agent_text() == "hello!"


async def test_persona_is_the_system_message_and_history_maps_roles():
    llm = ScriptedLLM("first", "second")
    agent = make_agent(llm=llm)
    task = await agent.handle_task(Task.from_text("hi"))
    task.history.append(task.history[0].model_copy(update={"text": "again"}))
    await agent.handle_task(task)

    first_call, second_call = llm.calls
    assert first_call[0]["role"] == "system"
    assert "Greeter" in first_call[0]["content"]
    assert [m["role"] for m in second_call] == ["system", "user", "assistant", "user"]


async def test_recalled_memory_lands_in_system_message():
    llm = ScriptedLLM("ok")
    agent = make_agent(llm=llm, memory=CannedMemory("shaz prefers short greetings"))
    await agent.handle_task(Task.from_text("hi"))
    assert "shaz prefers short greetings" in llm.calls[0][0]["content"]


async def test_no_memory_default_adds_nothing():
    llm = ScriptedLLM("ok")
    agent = make_agent(llm=llm)
    await agent.handle_task(Task.from_text("hi"))
    assert "Recalled context" not in llm.calls[0][0]["content"]


def test_unknown_transport_kind_warns_and_is_skipped(tmp_path):
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntransports:\n  - kind: telepathy\n  - kind: cli\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    agent = Agent.load(tmp_path, llm=ScriptedLLM())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        transports = agent.transports()
    assert len(transports) == 1
    assert any("telepathy" in str(w.message) for w in caught)


async def test_serve_with_no_usable_transports_raises(tmp_path):
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntransports:\n  - kind: telepathy\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    agent = Agent.load(tmp_path, llm=ScriptedLLM())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(RuntimeError, match="no usable transports"):
            await agent.serve()
