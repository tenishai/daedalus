"""Vision plumbing: a Message with images becomes multipart LLM content;
one without stays a plain string."""

from daedalus.task import ImagePart, Task
from tests.fakes import ScriptedLLM
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
