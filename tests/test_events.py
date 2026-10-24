"""EventBus fan-out and the SSE /events endpoint."""

import asyncio
import json

import pytest

from daedalus.agent import Agent
from daedalus.bundle import Bundle
from daedalus.events import EventBus
from daedalus.task import Task
from tests.fakes import ScriptedLLM


def test_publish_reaches_subscribers_and_replay_catches_up_late_ones():
    bus = EventBus(replay=3)
    bus.publish({"type": "one"})
    replay, queue = bus.subscribe()
    assert replay == [{"type": "one"}]
    bus.publish({"type": "two"})
    assert queue.get_nowait() == {"type": "two"}

    late_replay, _ = bus.subscribe()
    assert [e["type"] for e in late_replay] == ["one", "two"]


def test_replay_is_bounded():
    bus = EventBus(replay=2)
    for index in range(5):
        bus.publish({"type": str(index)})
    replay, _ = bus.subscribe()
    assert [e["type"] for e in replay] == ["3", "4"]


def test_slow_subscriber_drops_but_never_raises():
    bus = EventBus()
    _, queue = bus.subscribe()
    for index in range(600):  # queue cap is 500
        bus.publish({"type": str(index)})
    assert queue.qsize() == 500  # overflow dropped silently


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    _, queue = bus.subscribe()
    bus.unsubscribe(queue)
    bus.publish({"type": "gone"})
    assert queue.qsize() == 0


async def test_agent_emits_to_its_bus_even_without_on_event(tmp_path):
    (tmp_path / "agent.yaml").write_text("name: x\nmodel:\n  id: m\n")
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    agent = Agent(Bundle.load(tmp_path), llm=ScriptedLLM("done"))  # no on_event
    replay_before, queue = agent.events.subscribe()
    # Before any task the only event is which model loaded (#103), so a late subscriber sees it.
    assert [e["type"] for e in replay_before] == ["model_resolved"]
    await agent.handle_task(Task.from_text("hi"))
    types = []
    while queue.qsize():
        types.append(queue.get_nowait()["type"])
    assert "tools_offered" in types


async def test_sse_stream_replays_then_streams_live():
    """httpx's ASGITransport buffers to completion, so infinite SSE bodies
    can't be integration-tested that way — the generator is the unit."""
    pytest.importorskip("fastapi")
    from daedalus.transports.a2a import sse_stream

    bus = EventBus()
    bus.publish({"type": "warmup"})
    stream = sse_stream(bus)

    first = await asyncio.wait_for(anext(stream), timeout=2)
    assert json.loads(first.removeprefix("data: "))["type"] == "warmup"

    bus.publish({"type": "live"})
    second = await asyncio.wait_for(anext(stream), timeout=2)
    assert json.loads(second.removeprefix("data: "))["type"] == "live"

    await stream.aclose()
    bus.publish({"type": "after-close"})  # closed stream unsubscribed cleanly
    assert all(isinstance(q, asyncio.Queue) for q in bus._subscribers) and not bus._subscribers


async def test_sse_endpoint_404s_without_a_bus():
    pytest.importorskip("fastapi")
    import httpx

    from daedalus.transports.a2a import build_a2a_app

    class Idle:
        async def handle_task(self, task):
            return task.reply("ok")

    app = build_a2a_app(Idle())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert (await client.get("/events")).status_code == 404


def test_a2a_transport_gets_the_agent_bus(tmp_path):
    pytest.importorskip("fastapi")
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntransports:\n  - kind: a2a\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    agent = Agent.load(tmp_path, llm=ScriptedLLM())
    try:
        transports = agent.transports()
    except Exception:
        pytest.skip("a2a extra incomplete in this env")
    assert transports[0].events is agent.events

async def test_events_stay_open_without_a_verifier():
    """No `auth` block means every a2a surface is open; /events is unchanged."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import daedalus.transports.a2a as a2a

    class Idle:
        async def handle_task(self, task):
            return task.reply("ok")

    async def finite(events, content=False):
        yield "data: {}\n\n"

    original = a2a.sse_stream
    a2a.sse_stream = finite
    try:
        response = TestClient(a2a.build_a2a_app(Idle(), events=EventBus())).get("/events")
    finally:
        a2a.sse_stream = original
    assert response.status_code == 200


async def test_the_wire_drops_content_by_default():
    """#196: tool arguments, tool results and reasoning leave the process
    only as sizes unless DAEDALUS_TRACE_CONTENT=1. Names stay."""
    pytest.importorskip("fastapi")
    from daedalus.transports.a2a import sse_stream

    bus = EventBus()
    bus.publish({"type": "tool_call", "name": "files.read", "arguments": {"path": "/etc/x"}})
    bus.publish({"type": "tool_result", "name": "files.read", "content": "SECRET-BODY"})
    bus.publish({"type": "reasoning", "text": "private thought"})
    bus.publish({"type": "recall", "count": 3})
    stream = sse_stream(bus)
    frames = [await asyncio.wait_for(anext(stream), timeout=2) for _ in range(4)]
    await stream.aclose()
    wire = "".join(frames)
    for secret in ("/etc/x", "SECRET-BODY", "private thought"):
        assert secret not in wire
    call, result, reasoning, recall = (json.loads(f.removeprefix("data: ")) for f in frames)
    assert call == {"type": "tool_call", "name": "files.read", "arguments_chars": 18}
    assert result == {"type": "tool_result", "name": "files.read", "content_chars": 11}
    assert reasoning == {"type": "reasoning", "text_chars": 15}
    assert recall == {"type": "recall", "count": 3}
    # The bus itself is untouched: in-process consumers (-v, Slack progress) keep content.
    assert bus.subscribe()[0][1]["content"] == "SECRET-BODY"


def test_trace_content_flag_puts_content_back(monkeypatch):
    pytest.importorskip("fastapi")
    from daedalus.transports.a2a import trace_content_enabled, wire_event

    event = {"type": "tool_result", "name": "n", "content": "body"}
    monkeypatch.delenv("DAEDALUS_TRACE_CONTENT", raising=False)
    assert trace_content_enabled() is False
    monkeypatch.setenv("DAEDALUS_TRACE_CONTENT", "1")
    assert trace_content_enabled() is True
    assert wire_event(event, content=True) == event


@pytest.mark.parametrize("env,expected", [(None, False), ("1", True), ("true", False)])
def test_the_route_passes_the_trace_flag_to_the_stream(monkeypatch, env, expected):
    """The redaction a network reader gets is the route's choice, not the
    stream's parameter default: pin what `event_stream` passes (review of
    PR #207)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import daedalus.transports.a2a as a2a

    class Idle:
        async def handle_task(self, task):
            return task.reply("ok")

    seen = []

    async def finite(events, content=False):
        seen.append(content)
        yield "data: {}\n\n"

    monkeypatch.setattr(a2a, "sse_stream", finite)
    if env is None:
        monkeypatch.delenv("DAEDALUS_TRACE_CONTENT", raising=False)
    else:
        monkeypatch.setenv("DAEDALUS_TRACE_CONTENT", env)
    TestClient(a2a.build_a2a_app(Idle(), events=EventBus())).get("/events")
    assert seen == [expected]


async def test_the_live_path_redacts_too():
    """Frames published after subscribe go through the live loop, not the
    replay: they must be redacted the same way (review of PR #207)."""
    from daedalus.transports import a2a

    bus = EventBus()
    stream = a2a.sse_stream(bus)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    bus.publish({"type": "tool_result", "name": "n", "content": "SECRET-BODY"})
    frame = await asyncio.wait_for(pending, timeout=2)
    await stream.aclose()
    assert json.loads(frame.removeprefix("data: ")) == {
        "type": "tool_result", "name": "n", "content_chars": 11,
    }
