"""The scripted engine (#227): commands through the real seams, no model, and only
behind DAEDALUS_SCRIPTED_ENGINE=1."""

import json

import pytest

from daedalus.agent import ENGINES, Agent, engines
from daedalus.bundle import Bundle, BundleError
from daedalus.task import REASON_KEY, ReasonCode, Task, TaskState
from tests.fakes import CannedMemory, FakeTools, RecordingApprover, ScriptedLLM
from tests.test_agent_tools import READ_SPEC, WRITE_SPEC


def _bundle(tmp_path, *, allow=("files.read",), gates="", model="m"):
    allow_yaml = "".join(f"    - {p}\n" for p in allow)
    (tmp_path / "agent.yaml").write_text(
        f"name: x\nmodel:\n  id: {model}\ntools:\n  provider: none\n  allow:\n{allow_yaml}"
        + (f"approvals:\n  gates:\n{gates}" if gates else "")
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    return Bundle.load(tmp_path)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("DAEDALUS_SCRIPTED_ENGINE", "1")


def _agent(tmp_path, *, memory=None, tools=None, approver=None, **bundle_kw):
    llm = ScriptedLLM()  # no replies: any model call would fail the test
    agent = Agent(
        _bundle(tmp_path, **bundle_kw),
        llm=llm,
        memory=memory if memory is not None else CannedMemory(),
        tools=tools or FakeTools([READ_SPEC, WRITE_SPEC]),
        approver=approver,
        engine="scripted",
    )
    return agent, llm


def test_scripted_is_refused_without_the_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("DAEDALUS_SCRIPTED_ENGINE", raising=False)
    assert engines() == ENGINES
    with pytest.raises(BundleError, match="DAEDALUS_SCRIPTED_ENGINE=1"):
        Agent(_bundle(tmp_path), llm=ScriptedLLM(), engine="scripted")


def test_only_exactly_1_turns_it_on(tmp_path, monkeypatch):
    monkeypatch.setenv("DAEDALUS_SCRIPTED_ENGINE", "true")
    assert "scripted" not in engines()


async def test_remember_commits_through_the_memory_store(tmp_path, on):
    memory = CannedMemory()
    agent, llm = _agent(tmp_path, memory=memory)
    task = await agent.handle_task(Task.from_text("remember: the lab fact is 42 #team"))
    assert task.state is TaskState.COMPLETED
    assert task.agent_text() == "remembered: the lab fact is 42 [team]"
    assert memory.committed == [("the lab fact is 42", "fact", "team")]
    assert llm.calls == []


async def test_recall_returns_the_hits_as_json(tmp_path, on):
    agent, llm = _agent(tmp_path, memory=CannedMemory("seed for A", "another"))
    task = await agent.handle_task(Task.from_text("recall: seed"))
    assert task.state is TaskState.COMPLETED
    # A store without recall_hits (this fake) reports every hit as local.
    assert json.loads(task.agent_text()) == [
        {"content": "seed for A", "source": "local"},
        {"content": "another", "source": "local"},
    ]
    assert llm.calls == []


async def test_call_runs_an_allowed_tool_through_the_provider(tmp_path, on):
    tools = FakeTools([READ_SPEC, WRITE_SPEC], {"files.read": "contents: hi"})
    agent, llm = _agent(tmp_path, tools=tools)
    task = await agent.handle_task(Task.from_text('call: files.read {"path": "/x"}'))
    assert task.agent_text() == "contents: hi"
    assert tools.calls == [("files.read", {"path": "/x"})]
    assert llm.calls == []


async def test_call_outside_the_allowlist_is_refused_by_the_allowlist(tmp_path, on):
    tools = FakeTools([READ_SPEC, WRITE_SPEC])
    agent, _ = _agent(tmp_path, tools=tools)
    task = await agent.handle_task(Task.from_text('call: files.write {"path": "/x"}'))
    assert task.agent_text() == "error: unknown or disallowed tool 'files.write'"
    assert tools.calls == []


async def test_call_meets_the_approval_gates(tmp_path, on):
    tools = FakeTools([WRITE_SPEC])
    agent, _ = _agent(
        tmp_path,
        tools=tools,
        approver=RecordingApprover(False),
        allow=("files.write",),
        gates='    - match: "files.write"\n      require: human\n',
    )
    task = await agent.handle_task(Task.from_text('call: files.write {"path": "/x"}'))
    assert task.agent_text().startswith("blocked: files.write requires approval")
    assert tools.calls == []


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "remember:",
        "remember: #team",
        "forget: x",
        "call: files.read {not json",
        "call: files.read [1]",
    ],
)
async def test_anything_else_fails_the_task(tmp_path, on, text):
    agent, llm = _agent(tmp_path)
    task = await agent.handle_task(Task.from_text(text))
    assert task.state is TaskState.FAILED
    assert task.metadata[REASON_KEY] == ReasonCode.ESCALATION
    assert task.agent_text().startswith("scripted: ")
    assert llm.calls == []


def test_a_tier_word_needs_no_tiers_file(tmp_path, on, monkeypatch):
    monkeypatch.delenv("DAEDALUS_TIERS", raising=False)
    agent, _ = _agent(tmp_path, model="best")
    assert agent.engine_name == "scripted"
    assert agent.model_resolution.engine == "scripted"
