"""The a2a transport: card serving, message/send, tasks/get, continuity."""

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from daedalus.task import ReasonCode, Task, TaskState  # noqa: E402
from daedalus.transports.a2a import build_a2a_app, task_to_a2a  # noqa: E402

CARD = {"name": "greeter", "description": "golden", "version": "0.1.0", "skills": []}


class EchoHandler:
    async def handle_task(self, task: Task) -> Task:
        return task.reply(f"echo: {task.user_text()}")


class EscalatingHandler:
    async def handle_task(self, task: Task) -> Task:
        return task.reply(
            "needs a human", state=TaskState.INPUT_REQUIRED, reason=ReasonCode.ESCALATION
        )


def rpc(client, method, params, rpc_id=1):
    return client.post(
        "/", json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
    ).json()


def send_text(client, text, **message_extra):
    message = {"role": "user", "parts": [{"kind": "text", "text": text}], **message_extra}
    return rpc(client, "message/send", {"message": message})


def test_card_is_served_at_well_known():
    client = TestClient(build_a2a_app(EchoHandler(), card=CARD))
    response = client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    assert response.json()["name"] == "greeter"


def test_message_send_returns_completed_a2a_task():
    client = TestClient(build_a2a_app(EchoHandler(), card=CARD))
    body = send_text(client, "hello")
    task = body["result"]
    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["history"][0]["parts"][0]["text"] == "hello"
    assert task["history"][-1]["role"] == "agent"
    assert task["history"][-1]["parts"][0]["text"] == "echo: hello"


def test_task_continuity_via_task_id():
    client = TestClient(build_a2a_app(EchoHandler()))
    first = send_text(client, "one")["result"]
    second = send_text(client, "two", taskId=first["id"])["result"]
    assert second["id"] == first["id"]
    texts = [m["parts"][0]["text"] for m in second["history"]]
    assert texts == ["one", "echo: one", "two", "echo: two"]


def test_tasks_get_retrieves_stored_task():
    client = TestClient(build_a2a_app(EchoHandler()))
    created = send_text(client, "hi")["result"]
    fetched = rpc(client, "tasks/get", {"id": created["id"]})["result"]
    assert fetched == created


def test_reason_codes_ride_in_metadata():
    client = TestClient(build_a2a_app(EscalatingHandler()))
    task = send_text(client, "deploy prod")["result"]
    assert task["status"]["state"] == "input-required"
    assert task["metadata"]["daedalus_reason_code"] == 704


def test_unknown_method_is_32601():
    client = TestClient(build_a2a_app(EchoHandler()))
    body = rpc(client, "tasks/cancel", {})
    assert body["error"]["code"] == -32601


def test_bad_params_are_32602():
    client = TestClient(build_a2a_app(EchoHandler()))
    assert rpc(client, "message/send", {})["error"]["code"] == -32602
    no_text = rpc(
        client, "message/send", {"message": {"role": "user", "parts": [{"kind": "data"}]}}
    )
    assert no_text["error"]["code"] == -32602
    assert rpc(client, "tasks/get", {"id": "nope"})["error"]["code"] == -32602
    unknown_continue = send_text(client, "hi", taskId="missing")
    assert unknown_continue["error"]["code"] == -32602


def test_task_to_a2a_shape():
    task = Task.from_text("hi", context_id="ctx-9").reply("yo")
    a2a = task_to_a2a(task)
    assert a2a["contextId"] == "ctx-9"
    assert [m["role"] for m in a2a["history"]] == ["user", "agent"]
