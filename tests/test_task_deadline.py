"""Task deadlines and terminal states (#201).

A model call carries a timeout, the engine run carries a deadline, a tool call
running when the deadline passes is cancelled, and a task that times out or
whose handler raises ends FAILED with reason 704 instead of staying WORKING.
"""

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

from daedalus.agent import Agent
from daedalus.bundle import Bundle
from daedalus.llm import LiteLLMClient
from daedalus.protocols import LLMResponse, ToolCall
from daedalus.task import REASON_KEY, ReasonCode, Task, TaskState
from tests.fakes import ScriptedLLM

GOLDEN = Path(__file__).parent / "golden" / "greeter"


def bundle(tmp_path, extra: str = "", model: str = "  id: openai/m\n") -> Bundle:
    (tmp_path / "agent.yaml").write_text(f"name: x\nmodel:\n{model}{extra}")
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    return Bundle.load(tmp_path)


# ---- the model-call timeout -------------------------------------------------


async def test_the_client_passes_its_timeout_to_litellm(monkeypatch):
    seen = {}

    async def acompletion(**kwargs):
        seen.update(kwargs)
        message = types.SimpleNamespace(content="hi", tool_calls=None)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=message, finish_reason="stop")], usage=None
        )

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))
    await LiteLLMClient("openai/m", timeout=42.0).complete([{"role": "user", "content": "x"}])
    assert seen["timeout"] == 42.0

    seen.clear()
    await LiteLLMClient("openai/m").complete([{"role": "user", "content": "x"}])
    assert "timeout" not in seen  # no timeout given: litellm's own default


def test_the_agent_builds_its_client_with_the_bundle_timeout(tmp_path):
    assert Agent(bundle(tmp_path)).llm.timeout == 120.0  # the default
    agent = Agent(bundle(tmp_path, model="  id: openai/m\n  request_timeout: 30\n"))
    assert agent.llm.timeout == 30.0


TIERS = """
tiers:
  best: {id: openai/slow-local, request_timeout: 300}
  balanced: {id: openai/other}
  fast: balanced
"""


def test_a_tier_fills_the_request_timeout_unless_the_bundle_set_it(tmp_path, monkeypatch):
    tiers = tmp_path / "tiers.yaml"
    tiers.write_text(TIERS)
    monkeypatch.setenv("DAEDALUS_TIERS", str(tiers))
    monkeypatch.delenv("DAEDALUS_ENGINE", raising=False)
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()

    assert Agent(bundle(tmp_path / "a", model="  id: best\n")).llm.timeout == 300.0
    pinned = bundle(tmp_path / "b", model="  id: best\n  request_timeout: 45\n")
    assert Agent(pinned).llm.timeout == 45.0
    assert Agent(bundle(tmp_path / "c", model="  id: balanced\n")).llm.timeout == 120.0


def test_timeouts_must_be_positive(tmp_path):
    with pytest.raises(Exception, match="greater than 0"):
        bundle(tmp_path, "task_timeout: 0\n")
    with pytest.raises(Exception, match="greater than 0"):
        bundle(tmp_path, model="  id: openai/m\n  request_timeout: -1\n")


# ---- the task deadline ------------------------------------------------------


class HangingLLM:
    """A provider that never answers."""

    async def complete(self, messages, tools=None):
        await asyncio.Event().wait()


async def test_a_hung_model_call_fails_the_task_at_the_deadline(tmp_path):
    events = []
    agent = Agent(
        bundle(tmp_path, "task_timeout: 0.2\n"), llm=HangingLLM(), on_event=events.append
    )
    started = time.monotonic()
    task = await asyncio.wait_for(agent.handle_task(Task.from_text("hi")), 5)

    assert time.monotonic() - started < 2
    assert task.state is TaskState.FAILED
    assert task.metadata[REASON_KEY] == ReasonCode.ESCALATION
    assert "time limit" in task.agent_text()
    assert {"type": "task_timeout", "seconds": 0.2} in events


class HangingTool:
    """A tool provider whose call blocks until cancelled, and says so."""

    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False

    async def tools(self):
        return [{"name": "slow.wait", "description": "", "parameters": {}}]

    async def call(self, name, payload):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "never"


async def test_the_deadline_cancels_a_running_tool_call(tmp_path):
    tool = HangingTool()
    llm = ScriptedLLM(
        LLMResponse(text="", tool_calls=[ToolCall(id="1", name="slow__wait", arguments={})]),
        "finished after the tool",
    )
    agent = Agent(
        bundle(tmp_path, 'task_timeout: 0.2\ntools:\n  allow: ["slow.*"]\n'),
        llm=llm,
        tools=tool,
    )
    task = await asyncio.wait_for(agent.handle_task(Task.from_text("hi")), 5)

    assert tool.started.is_set()
    assert tool.cancelled, "the tool call was not cancelled at the deadline"
    assert task.state is TaskState.FAILED
    assert task.metadata[REASON_KEY] == ReasonCode.ESCALATION
    assert len(llm.calls) == 1, "the run went on after the deadline"


class RaisingTimeoutLLM:
    async def complete(self, messages, tools=None):
        raise TimeoutError("the provider's own timeout")


async def test_a_timeout_the_deadline_did_not_cause_is_not_relabelled(tmp_path):
    agent = Agent(bundle(tmp_path), llm=RaisingTimeoutLLM())
    with pytest.raises(TimeoutError, match="provider's own"):
        await agent.handle_task(Task.from_text("hi"))


async def test_a_task_inside_its_deadline_completes(tmp_path):
    agent = Agent(bundle(tmp_path, "task_timeout: 5\n"), llm=ScriptedLLM("hello"))
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.state is TaskState.COMPLETED
    assert task.agent_text() == "hello"


# ---- a2a: the stored task ends FAILED -------------------------------------


class RaisingHandler:
    seen: str | None = None

    async def handle_task(self, task: Task) -> Task:
        self.seen = task.id
        task.state = TaskState.WORKING
        raise RuntimeError("provider exploded")


class CancelledHandler:
    async def handle_task(self, task: Task) -> Task:
        task.state = TaskState.WORKING
        raise asyncio.CancelledError


def _client(handler):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from daedalus.transports.a2a import build_a2a_app

    return TestClient(build_a2a_app(handler))


def _get(client, task_id):
    body = {"jsonrpc": "2.0", "id": 9, "method": "tasks/get", "params": {"id": task_id}}
    return client.post("/", json=body).json()["result"]


def _peer_body(task_id):
    message = {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}
    return {"id": task_id, "status": {"message": message}}


def test_a_peer_dispatch_whose_handler_raises_leaves_the_task_failed():
    client = _client(RaisingHandler())
    response = client.post("/a2a/tasks", json=_peer_body("t-1"))

    assert response.status_code == 500  # the response contract is unchanged
    assert "provider exploded" in response.json()["error"]
    stored = _get(client, "t-1")
    assert stored["status"]["state"] == "failed"
    assert stored["metadata"]["cabal_reason_code"] == 704
    assert stored["metadata"][REASON_KEY] == 704


def test_a_message_send_whose_handler_raises_leaves_the_task_failed():
    handler = RaisingHandler()
    client = _client(handler)
    message = {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}
    body = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": message}}
    response = client.post("/", json=body).json()

    assert response["error"]["code"] == -32000
    stored = _get(client, handler.seen)
    assert stored["status"]["state"] == "failed"
    assert stored["metadata"]["cabal_reason_code"] == 704


def test_a_cancelled_handler_leaves_the_task_failed():
    from daedalus.transports.a2a import run_handler

    task = Task.from_text("hi")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_handler(CancelledHandler(), task))
    assert task.state is TaskState.FAILED
    assert task.metadata[REASON_KEY] == ReasonCode.ESCALATION
    assert "cancelled" in task.agent_text()


# ---- slack: the thread leaves its in-progress state ------------------------


async def test_a_timed_out_task_ends_the_slack_threads_working_state(tmp_path):
    from tests.test_transport_slack import event, make

    agent = Agent(bundle(tmp_path, "task_timeout: 0.2\n"), llm=HangingLLM())
    transport, api, _ = make(handler=agent)
    await asyncio.wait_for(transport.handle_event(event(ts="90.0"), agent), 5)

    assert ("90.0", "eyes") in api.unreacts
    assert ("90.0", "x") in api.reacts
