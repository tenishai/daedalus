"""Enforcement wired into the agent loop: every reply passes through the law.

Real Enforcement, real Bundle (tmp_path mini-bundles mirroring
tests/golden/greeter's shape plus a tools/ dir) and ScriptedLLM for the model
seam -- house rule from test_enforcement.py: exercise the real
importlib.util path, not a mock of Enforcement itself.
"""

from pathlib import Path

import pytest

from daedalus.agent import Agent
from daedalus.bundle import Bundle
from daedalus.task import ImagePart, Message, ReasonCode, Task, TaskState
from tests.fakes import CannedMemory, ScriptedLLM


def _write_bundle(
    tmp_path: Path, *, transports: str = "", tools: dict[str, str] | None = None
) -> Path:
    (tmp_path / "agent.yaml").write_text(f"name: x\nmodel:\n  id: m\n{transports}")
    (tmp_path / "agent-card.json").write_text('{"name": "x"}')
    (tmp_path / "persona.md").write_text("You are x.")
    if tools:
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        for name, body in tools.items():
            (tools_dir / name).write_text(body)
    return tmp_path


def make_agent(tmp_path, *, tools=None, **overrides) -> Agent:
    overrides.setdefault("llm", ScriptedLLM("hello!"))
    return Agent(Bundle.load(_write_bundle(tmp_path, tools=tools)), **overrides)


# ---- notices reach the model's context --------------------------------------


async def test_notices_land_after_recalled_entries_in_system_message(tmp_path):
    llm = ScriptedLLM("ok")
    tools = {
        "a_notice.py": (
            "def pre_task(message):\n"
            "    return {'notices': ['keep replies short']}\n"
        )
    }
    agent = make_agent(
        tmp_path, tools=tools, llm=llm, memory=CannedMemory("shaz likes brevity")
    )
    await agent.handle_task(Task.from_text("hi"))
    system = llm.calls[0][0]["content"]
    assert "shaz likes brevity" in system
    assert "keep replies short" in system
    assert system.index("shaz likes brevity") < system.index("keep replies short")


async def test_no_notices_adds_nothing(tmp_path):
    llm = ScriptedLLM("ok")
    agent = make_agent(tmp_path, llm=llm)
    await agent.handle_task(Task.from_text("hi"))
    assert "Notices" not in llm.calls[0][0]["content"]


# ---- who sent a turn: the model reads it, the hooks and recall do not --------


class _QueryMemory:
    def __init__(self):
        self.queries: list[str] = []

    async def recall(self, query: str, k: int = 5) -> list[str]:
        self.queries.append(query)
        return []

    async def commit(self, content: str, kind: str = "fact") -> None:
        pass


async def test_a_labelled_turn_is_read_by_the_model_and_not_by_the_hooks(tmp_path):
    """A person's Slack turn carries who sent it (Message.sender_label). The
    model reads it as the message's first line, on every later turn too. The
    law hooks and recall get the person's own words: a hook may take the first
    email in a message as the subject of the ask."""
    llm = ScriptedLLM("first", "second")
    memory = _QueryMemory()
    tools = {
        "a_witness.py": (
            "def pre_task(message):\n"
            "    return {'notices': ['hook saw: ' + message]}\n"
        )
    }
    agent = make_agent(tmp_path, tools=tools, llm=llm, memory=memory)
    task = Task(
        history=[
            Message(
                role="user",
                text="audit jdoe@example.com",
                sender="U1",
                sender_label="Ada Lovelace <ada@example.com>",
            )
        ]
    )
    task = await agent.handle_task(task)
    task.history.append(Message(role="user", text="and since May?", sender="U2"))
    await agent.handle_task(task)

    first, second = llm.calls
    assert first[1]["content"] == "(From Ada Lovelace <ada@example.com>.)\naudit jdoe@example.com"
    assert "hook saw: audit jdoe@example.com" in first[0]["content"]
    assert "ada@" not in first[0]["content"]
    assert memory.queries == ["audit jdoe@example.com", "and since May?"]
    # Every turn the thread has had keeps its line; a turn with no label has none.
    assert [m["content"] for m in second[1:]] == [
        "(From Ada Lovelace <ada@example.com>.)\naudit jdoe@example.com",
        "first",
        "and since May?",
    ]


async def test_a_labelled_turn_with_an_image_says_who_sent_it_in_its_text_part(tmp_path):
    llm = ScriptedLLM("ok")
    agent = make_agent(tmp_path, llm=llm)
    task = Task(
        history=[
            Message(
                role="user",
                text="what is this?",
                images=[ImagePart(data="QUFB", media_type="image/png")],
                sender_label="Ada Lovelace <ada@example.com>",
            )
        ]
    )
    await agent.handle_task(task)
    parts = llm.calls[0][1]["content"]
    assert parts[0] == {
        "type": "text",
        "text": "(From Ada Lovelace <ada@example.com>.)\nwhat is this?",
    }
    assert parts[1]["type"] == "image_url"


# ---- post_draft transforms the final reply ----------------------------------


async def test_post_draft_transform_applied_to_final_reply(tmp_path):
    tools = {"a_shout.py": "def post_draft(draft):\n    return draft.upper()\n"}
    agent = make_agent(tmp_path, tools=tools, llm=ScriptedLLM("hello!"))
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.state is TaskState.COMPLETED
    assert task.agent_text() == "HELLO!"


# ---- pre-blocked and post-blocked: exact reply strings ----------------------


async def test_pre_blocked_produces_exact_reply_and_tool_blocked(tmp_path):
    tools = {
        "z_block.py": (
            "def pre_task(message):\n"
            "    raise ValueError('no deploys on friday')\n"
        )
    }
    llm = ScriptedLLM("should not be called")
    agent = make_agent(tmp_path, tools=tools, llm=llm)
    task = await agent.handle_task(Task.from_text("deploy"))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.TOOL_BLOCKED
    assert task.agent_text() == (
        "A rule stopped this task: blocked by z_block.py: no deploys on friday."
    )
    assert llm.calls == []


async def test_post_blocked_produces_exact_reply_and_discards_draft(tmp_path):
    tools = {
        "z_reject.py": (
            "def post_draft(draft):\n"
            "    raise ValueError('leaks a secret')\n"
        )
    }
    agent = make_agent(tmp_path, tools=tools, llm=ScriptedLLM("the secret is 42"))
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.TOOL_BLOCKED
    assert task.agent_text() == (
        "A rule stopped this reply: blocked by z_reject.py: leaks a secret."
    )
    assert "the secret is 42" not in task.agent_text()
    # Reviewed CRITICAL finding: the blocked draft must vanish from history
    # entirely -- task_to_a2a serializes the FULL history verbatim (it would
    # leak to a2a callers), and transports that persist the Task across turns
    # (Slack threads, a2a taskId continuation) feed history back into the
    # model's context on the next turn. "Discarded, never partially sent."
    assert all("the secret is 42" not in message.text for message in task.history)
    assert [m.role for m in task.history] == ["user", "agent"]


def test_discard_last_agent_message_removes_it_from_history():
    """Unit test for the history-hygiene helper agent.py leans on above --
    Task owns its history invariants, agent.py doesn't reach in directly."""
    task = Task.from_text("hi")
    task.reply("draft that must not survive")
    task.discard_last_agent_message()
    assert task.history == [Message(role="user", text="hi")]
    assert task.agent_text() == ""


def test_discard_last_agent_message_removes_the_most_recent_agent_message_only():
    task = Task.from_text("first")
    task.reply("first reply")
    task.history.append(Message(role="user", text="second"))
    task.reply("draft to discard")
    task.discard_last_agent_message()
    assert [m.text for m in task.history] == ["first", "first reply", "second"]


# ---- locked: zero llm calls --------------------------------------------------


async def test_locked_short_circuits_before_any_llm_call(tmp_path):
    tools = {"broken.py": "import definitely_not_a_module\n"}
    llm = ScriptedLLM("should not be called")
    agent = make_agent(tmp_path, tools=tools, llm=llm)
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.TOOL_BLOCKED
    assert task.agent_text() == (
        "My orchestrator file broken.py failed to load. Fix or remove it in the room."
    )
    assert llm.calls == []


# ---- no tools dir: byte-identical to pre-enforcement behavior ---------------


async def test_no_tools_dir_is_byte_identical_to_pre_enforcement(tmp_path):
    llm = ScriptedLLM("hello!")
    agent = make_agent(tmp_path, llm=llm)  # no tools dir at all
    assert agent.enforcement.active is False
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.state is TaskState.COMPLETED
    assert task.agent_text() == "hello!"
    assert llm.calls[0][0]["role"] == "system"
    assert "Recalled context" not in llm.calls[0][0]["content"]
    assert "Notices" not in llm.calls[0][0]["content"]


# ---- card stamp: presence when active, absence when not --------------------


def _a2a_agent(tmp_path, *, tools=None, **overrides) -> Agent:
    overrides.setdefault("llm", ScriptedLLM())
    return Agent.load(
        _write_bundle(tmp_path, transports="transports:\n  - kind: a2a\n", tools=tools),
        **overrides,
    )


def test_card_carries_enforcement_stamp_when_active(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from daedalus.transports.a2a import build_a2a_app

    tools = {"a_notice.py": "def pre_task(message):\n    return {'notices': []}\n"}
    agent = _a2a_agent(tmp_path, tools=tools)
    transport = agent.transports()[0]
    client = TestClient(build_a2a_app(agent, card=transport.card))
    card = client.get("/.well-known/agent-card.json").json()
    assert card["enforcement"] == {"version": 1, "hooks": ["a_notice.py"], "locked": False}
    # the on-disk / in-memory bundle card is never mutated to build the stamp
    assert "enforcement" not in agent.bundle.card


def test_card_has_no_enforcement_key_when_not_active(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from daedalus.transports.a2a import build_a2a_app

    agent = _a2a_agent(tmp_path)  # no tools dir -> enforcement inactive
    transport = agent.transports()[0]
    client = TestClient(build_a2a_app(agent, card=transport.card))
    card = client.get("/.well-known/agent-card.json").json()
    assert "enforcement" not in card
