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


def test_local_api_base_gets_placebo_key_without_env(tmp_path, monkeypatch):
    """Finding #21: a kaminos-spawned agent lacks the operator's shell env;
    a bundle pointing at a local server must be self-contained — litellm
    demands a key that vLLM ignores."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: openai/local\n  api_base: http://10.0.0.15:8001/v1\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    agent = Agent.load(tmp_path)
    assert agent.llm.api_key == "no-auth"


def test_api_key_env_reads_real_key_and_no_base_means_no_placebo(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-real")
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: openai/hosted\n  api_key_env: MY_KEY\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    assert Agent.load(tmp_path).llm.api_key == "sk-real"

    (tmp_path / "agent.yaml").write_text("name: x\nmodel:\n  id: anthropic/claude-sonnet-4-5\n")
    assert Agent.load(tmp_path).llm.api_key is None  # provider env defaults apply


def test_model_api_base_flows_into_default_llm(tmp_path):
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: openai/some-local-model\n  api_base: http://10.0.0.15:8000/v1\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    agent = Agent.load(tmp_path)  # no llm override — default LiteLLMClient is built
    assert agent.llm.model == "openai/some-local-model"
    assert agent.llm.api_base == "http://10.0.0.15:8000/v1"


def test_a2a_transport_wires_with_card_and_options(tmp_path):
    import pytest

    pytest.importorskip("fastapi")
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntransports:\n  - kind: a2a\n    port: 9911\n"
    )
    (tmp_path / "agent-card.json").write_text('{"name": "x"}')
    (tmp_path / "persona.md").write_text("persona")
    agent = Agent.load(tmp_path, llm=ScriptedLLM())
    # uvicorn is required to SERVE but not to construct in tests
    try:
        transports = agent.transports()
    except Exception as exc:
        assert "daedalus[a2a]" in str(exc)
        return
    assert transports[0].port == 9911
    assert transports[0].card == {"name": "x"}


def test_slack_transport_requires_channel_and_extra(tmp_path):
    import importlib.util

    import pytest

    from daedalus.bundle import BundleError

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntransports:\n  - kind: slack\n    channel: C0123\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    agent = Agent.load(tmp_path, llm=ScriptedLLM())
    if importlib.util.find_spec("slack_sdk") is None:
        with pytest.raises(BundleError, match=r"daedalus\[slack\]"):
            agent.transports()
    else:
        transport = agent.transports()[0]
        assert transport.channel == "C0123"

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntransports:\n  - kind: slack\n"
    )
    agent2 = Agent.load(tmp_path, llm=ScriptedLLM())
    if importlib.util.find_spec("slack_sdk") is not None:
        with pytest.raises(BundleError, match="channel"):
            agent2.transports()


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


# --- finding #27: truncation is never silent --------------------------------


async def test_truncated_reply_is_marked_and_traced():
    """finish_reason == "length" (born as kobo #1/#4: a 1024-token cap made
    replies LOOK complete on every surface) must mark the reply text AND
    emit a truncated event for the trace panes."""
    from daedalus.protocols import LLMResponse

    events = []
    agent = make_agent(
        llm=ScriptedLLM(LLMResponse(text="the first half of a long ans", finish_reason="length")),
        on_event=events.append,
    )
    task = await agent.handle_task(Task.from_text("explain everything"))
    assert task.state is TaskState.COMPLETED
    assert task.agent_text().startswith("the first half of a long ans")
    assert "[reply truncated: hit the model's max_tokens" in task.agent_text()
    truncated = [e for e in events if e["type"] == "truncated"]
    assert len(truncated) == 1
    assert truncated[0]["max_tokens"] == agent.bundle.spec.model.max_tokens


async def test_complete_reply_carries_no_truncation_marker():
    from daedalus.protocols import LLMResponse

    events = []
    agent = make_agent(
        llm=ScriptedLLM(LLMResponse(text="all done", finish_reason="stop")),
        on_event=events.append,
    )
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.agent_text() == "all done"
    assert not [e for e in events if e["type"] == "truncated"]


async def test_finish_reason_defaults_to_none_for_scripted_replies():
    """Fakes and older LLM impls that never set finish_reason stay silent —
    absence of evidence is not evidence of truncation."""
    agent = make_agent()  # plain ScriptedLLM string reply
    task = await agent.handle_task(Task.from_text("hi"))
    assert "[reply truncated" not in task.agent_text()


# --- attribution enforcement (2026-08-27 ruling) --------------------------------


async def test_consulted_colleague_omitted_from_draft_gets_appended():
    """The runtime enforces the attribution law: a colleague consulted
    during the task but unnamed in the draft is appended deterministically."""
    from daedalus.tools.colleagues import consulted_colleagues

    class ConsultingLLM(ScriptedLLM):
        async def complete(self, messages, tools=None):
            ledger = consulted_colleagues.get()
            if ledger is not None:
                ledger.append("gemini-one")
            return await super().complete(messages, tools)

    agent = make_agent(llm=ConsultingLLM("The PSA range is 10.140.9.0/24."))
    task = await agent.handle_task(Task.from_text("what's our PSA range?"))
    assert "(consulted: gemini-one)" in task.agent_text()


async def test_consulted_colleague_already_named_is_left_alone():
    from daedalus.tools.colleagues import consulted_colleagues

    class ConsultingLLM(ScriptedLLM):
        async def complete(self, messages, tools=None):
            ledger = consulted_colleagues.get()
            if ledger is not None:
                ledger.append("gemini-one")
            return await super().complete(messages, tools)

    agent = make_agent(llm=ConsultingLLM("gemini-one says the range is 10.140.9.0/24."))
    task = await agent.handle_task(Task.from_text("what's our PSA range?"))
    assert "consulted:" not in task.agent_text()


async def test_no_consultation_appends_nothing():
    agent = make_agent(llm=ScriptedLLM("plain answer"))
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.agent_text() == "plain answer"


@pytest.mark.parametrize(
    "junk", [float("inf"), float("-inf"), float("nan"), "nonsense", None, [], {"a": 1}, -5]
)
async def test_an_unreadable_inbound_depth_serves_the_task_at_the_top(junk) -> None:
    """daedalus#40 pre-pass — THE SHAPE THAT CRASHED INSTEAD OF DEGRADING.

    The inbound parser caught `(TypeError, ValueError)`, and
    `int(float("inf"))` raises `OverflowError`. So an infinite claimed depth
    escaped `handle_task` and the transport turned it into an HTTP 500 — in the
    branch whose entire stated property is that a malformed number costs a hop
    of slack rather than an error. `nan` lands on `ValueError` and always
    worked, which is why the neighbouring float shape hid it.

    REACHABLE OVER THE WIRE, not just from a test: `Task.metadata` has no
    value-type constraint, it rides through `task_from_a2a` verbatim, and
    Starlette's `Request.json()` is `json.loads`, which parses a bare
    `Infinity` token into this float.

    Asserts the task is SERVED, not that the parser returns 0 — serving is the
    property, and a parser that returned 0 while something downstream still
    raised would pass a narrower test.
    """
    from daedalus.tools.colleagues import DEPTH_KEY, current_delegation_depth

    agent = make_agent()
    task = Task.from_text("hi")
    task.metadata[DEPTH_KEY] = junk

    result = await agent.handle_task(task)

    assert result.state is TaskState.COMPLETED
    assert current_delegation_depth.get() == 0, (
        "a malformed depth must read as the top of the chain"
    )
