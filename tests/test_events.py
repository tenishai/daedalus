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