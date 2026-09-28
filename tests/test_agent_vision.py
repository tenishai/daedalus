"""Vision plumbing: a Message with images becomes multipart LLM content;
one without stays a plain string."""

import json

from daedalus.agent import Agent
from daedalus.bundle import Bundle
from daedalus.checkpoints import SqliteCheckpoints
from daedalus.protocols import ToolCall
from daedalus.task import ImagePart, Message, ReasonCode, Task, TaskState
from tests.fakes import FakeTools, ScriptedLLM
from tests.test_agent import make_agent


async def test_message_with_images_becomes_multipart_content():
    llm = ScriptedLLM("seen")
    agent = make_agent(llm=llm)
    task = Task.from_text("what is this?")
    task.history[0].images = [ImagePart(data="aGVsbG8=", media_type="image/png")]

    await agent.handle_task(task)

    sent = llm.calls[0]
    user_turn = next(m for m in sent if m["role"] == "user")
    assert isinstance(user_turn["content"], list)
    assert user_turn["content"][0] == {"type": "text", "text": "what is this?"}
    assert user_turn["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,aGVsbG8="},
    }


async def test_message_without_images_stays_a_plain_string():
    llm = ScriptedLLM("plain")
    agent = make_agent(llm=llm)

    await agent.handle_task(Task.from_text("no pictures here"))

    sent = llm.calls[0]
    user_turn = next(m for m in sent if m["role"] == "user")
    assert user_turn["content"] == "no pictures here"


async def test_two_images_produce_two_image_parts_in_order():
    llm = ScriptedLLM("ok")
    agent = make_agent(llm=llm)
    task = Task.from_text("compare")
    task.history[0].images = [
        ImagePart(data="Zmlyc3Q=", media_type="image/png"),
        ImagePart(data="c2Vjb25k", media_type="image/jpeg"),
    ]

    await agent.handle_task(task)

    user_turn = next(m for m in llm.calls[0] if m["role"] == "user")
    urls = [p["image_url"]["url"] for p in user_turn["content"] if p["type"] == "image_url"]
    assert urls == [
        "data:image/png;base64,Zmlyc3Q=",
        "data:image/jpeg;base64,c2Vjb25k",
    ]


async def test_two_image_bearing_turns_each_keep_their_own_image_part():
    """Thread continuation: an earlier image-bearing turn stays multipart
    with its own image in later calls, it does not collapse or leak its
    image into the newer turn."""
    llm = ScriptedLLM("ok")
    agent = make_agent(llm=llm)
    task = Task.from_text("first pic")
    task.history[0].images = [ImagePart(data="Zmlyc3Q=", media_type="image/png")]
    task.history.append(
        Message(
            role="user",
            text="second pic",
            images=[ImagePart(data="c2Vjb25k", media_type="image/jpeg")],
        )
    )

    await agent.handle_task(task)

    sent = llm.calls[0]
    user_turns = [m for m in sent if m["role"] == "user"]
    assert len(user_turns) == 2
    for turn in user_turns:
        assert isinstance(turn["content"], list)
        image_parts = [p for p in turn["content"] if p["type"] == "image_url"]
        assert len(image_parts) == 1
    assert user_turns[0]["content"][1]["image_url"]["url"] == "data:image/png;base64,Zmlyc3Q="
    assert user_turns[1]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,c2Vjb25k"


async def test_checkpoint_never_carries_image_base64(tmp_path):
    """Checkpoints are written on every loop step (LiteLLMEngine.run) -- a
    multi-step tool-using turn with an image attached must never balloon
    the checkpoint with the same base64 bytes, saved twice over (once from
    task.history, once from the LLM message parts)."""
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "  budget:\n    per_task_tokens: 10\n"
        "tools:\n  allow: ['files.*']\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")

    store = SqliteCheckpoints(tmp_path / "cp.db")
    tool_turn = ToolCall(id="1", name="files__read", arguments={})
    from daedalus.protocols import LLMResponse

    llm = ScriptedLLM(
        LLMResponse(text="", tool_calls=[tool_turn], input_tokens=10, output_tokens=5)
    )
    agent = Agent(
        Bundle.load(tmp_path),
        llm=llm,
        tools=FakeTools([{"name": "files.read", "description": "", "parameters": {}}]),
        checkpoints=store,
    )
    task = Task.from_text("what is this?")
    big_b64 = "a" * 5000
    task.history[0].images = [ImagePart(data=big_b64, media_type="image/png")]

    result = await agent.handle_task(task)

    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.BUDGET_EXHAUSTED

    state = await store.load(result.id)
    assert state is not None
    assert big_b64 not in json.dumps(state)
