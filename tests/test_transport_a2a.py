"""The a2a transport: card serving, message/send, tasks/get, continuity."""

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from daedalus.agent import Agent  # noqa: E402
from daedalus.bundle import Bundle  # noqa: E402
from daedalus.task import VERIFIED_SUB_KEY, ReasonCode, Task, TaskState  # noqa: E402
from daedalus.transports.a2a import build_a2a_app, task_to_a2a  # noqa: E402
from tests.fakes import ScriptedLLM  # noqa: E402

CARD = {"name": "greeter", "description": "golden", "version": "0.1.0", "skills": []}


def _post_draft_blocking_bundle(tmp_path: Path) -> Path:
    """A real bundle whose one enforcement hook always rejects the draft --
    the reviewer's repro for the CRITICAL leaked-draft finding."""
    (tmp_path / "agent.yaml").write_text("name: x\nmodel:\n  id: m\n")
    (tmp_path / "agent-card.json").write_text('{"name": "x"}')
    (tmp_path / "persona.md").write_text("You are x.")
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "z_reject.py").write_text(
        "def post_draft(draft):\n    raise ValueError('leaks a secret')\n"
    )
    return tmp_path


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


def test_handler_exception_is_jsonrpc_error_not_http_500():
    """Finding #19, layer 2: a crashing handler yields -32000, so callers
    always get a JSON body they can parse."""

    class Crashing:
        async def handle_task(self, task):
            raise RuntimeError("backend on fire")

    client = TestClient(build_a2a_app(Crashing()))
    body = send_text(client, "hi")
    assert body["error"]["code"] == -32000
    assert "backend on fire" in body["error"]["message"]


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


# ---- the peer surface: cabal's federated dispatch (POST /a2a/tasks) --------


def cabal_task(text="hello", task_id="task-1", **extra):
    """The wire shape cabal's _dispatch_remote actually sends (finding #22):
    the current message rides in status.message, history is EMPTY."""
    return {
        "id": task_id,
        "contextId": "ctx-1",
        "status": {
            "state": "submitted",
            "message": {"role": "user", "parts": [{"kind": "text", "text": text}]},
        },
        "history": [],
        "artifacts": [],
        "metadata": {"cabal_trace_id": "trace-9", "cabal_span_id": "span-3"},
        **extra,
    }


def test_peer_dispatch_returns_completed_task():
    client = TestClient(build_a2a_app(EchoHandler(), card=CARD))
    response = client.post("/a2a/tasks", json=cabal_task("hello"))
    assert response.status_code == 200
    task = response.json()
    assert task["id"] == "task-1"
    assert task["contextId"] == "ctx-1"
    assert task["status"]["state"] == "completed"
    assert task["history"][-1]["role"] == "agent"
    assert task["history"][-1]["parts"][0]["text"] == "echo: hello"
    # cbl renders the reply from status.message (finding #22)
    assert task["status"]["message"]["parts"][0]["text"] == "echo: hello"


def test_peer_dispatch_accepts_history_carried_messages():
    """Spec callers may send the transcript in history with no status.message."""
    payload = cabal_task()
    payload["status"] = {"state": "submitted"}
    payload["history"] = [{"role": "user", "parts": [{"kind": "text", "text": "via history"}]}]
    client = TestClient(build_a2a_app(EchoHandler()))
    task = client.post("/a2a/tasks", json=payload).json()
    assert task["status"]["message"]["parts"][0]["text"] == "echo: via history"


def test_peer_dispatch_does_not_double_ingest_duplicated_message():
    """A caller that puts the same message in BOTH history and status.message
    gets one turn, not two."""
    payload = cabal_task("once")
    payload["history"] = [{"role": "user", "parts": [{"kind": "text", "text": "once"}]}]
    client = TestClient(build_a2a_app(EchoHandler()))
    task = client.post("/a2a/tasks", json=payload).json()
    user_turns = [m for m in task["history"] if m["role"] == "user"]
    assert len(user_turns) == 1


def test_peer_dispatch_echoes_cabal_metadata_back():
    """Cabal's routing context (trace/span ids) must survive the round trip."""
    client = TestClient(build_a2a_app(EchoHandler()))
    task = client.post("/a2a/tasks", json=cabal_task()).json()
    assert task["metadata"]["cabal_trace_id"] == "trace-9"
    assert task["metadata"]["cabal_span_id"] == "span-3"


class RecordingHandler:
    """Records the Task it receives so a test can inspect the metadata the
    transport stamped before handing off to the handler."""

    def __init__(self):
        self.received: Task | None = None

    async def handle_task(self, task: Task) -> Task:
        self.received = task
        return task.reply(f"echo: {task.user_text()}")


def test_peer_dispatch_discards_wire_supplied_sub_when_unverified():
    """No verifier configured -- a caller cannot forge the trust marker by
    just putting it in the wire body (Task 2)."""
    handler = RecordingHandler()
    client = TestClient(build_a2a_app(handler))
    payload = cabal_task()
    payload["metadata"][VERIFIED_SUB_KEY] = "service:forged"
    response = client.post("/a2a/tasks", json=payload)
    assert response.status_code == 200
    assert VERIFIED_SUB_KEY not in handler.received.metadata


def test_peer_dispatch_reason_codes_use_cabal_key():
    client = TestClient(build_a2a_app(EscalatingHandler()))
    task = client.post("/a2a/tasks", json=cabal_task("deploy prod")).json()
    assert task["metadata"]["cabal_reason_code"] == 704
    assert task["metadata"]["daedalus_reason_code"] == 704


def test_peer_dispatch_tolerates_data_parts_and_extra_fields():
    """Cabal's models are extra='allow'; ours must not choke on the extras."""
    payload = cabal_task(unknown_future_field={"x": 1})
    payload["history"].insert(
        0, {"role": "agent", "parts": [{"kind": "data", "data": {"k": "v"}}]}
    )
    client = TestClient(build_a2a_app(EchoHandler()))
    response = client.post("/a2a/tasks", json=payload)
    assert response.status_code == 200


def test_peer_dispatch_bad_bodies_are_readable_422s():
    """Cabal wraps >=400 response text in DispatchError — make it legible."""
    client = TestClient(build_a2a_app(EchoHandler()))
    no_id = cabal_task()
    del no_id["id"]
    response = client.post("/a2a/tasks", json=no_id)
    assert response.status_code == 422
    assert "task.id is required" in response.json()["error"]

    no_text = cabal_task()
    no_text["status"] = {"state": "submitted"}  # no status.message either
    no_text["history"] = [{"role": "user", "parts": [{"kind": "data", "data": {}}]}]
    response = client.post("/a2a/tasks", json=no_text)
    assert response.status_code == 422
    assert "no user text" in response.json()["error"]

    not_a_task = client.post("/a2a/tasks", json={"kind": "message", "id": "x"})
    assert not_a_task.status_code == 422

    response = client.post(
        "/a2a/tasks", content=b"not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400


def test_peer_dispatch_handler_crash_is_readable_500():
    class Crashing:
        async def handle_task(self, task):
            raise RuntimeError("backend on fire")

    client = TestClient(build_a2a_app(Crashing()))
    response = client.post("/a2a/tasks", json=cabal_task())
    assert response.status_code == 500
    assert "backend on fire" in response.json()["error"]


def test_peer_task_visible_to_jsonrpc_tasks_get():
    """One task store across both dispatch surfaces."""
    client = TestClient(build_a2a_app(EchoHandler()))
    client.post("/a2a/tasks", json=cabal_task(task_id="task-77"))
    fetched = rpc(client, "tasks/get", {"id": "task-77"})["result"]
    assert fetched["status"]["state"] == "completed"


def test_peer_dispatch_tool_blocked_is_a_failed_task_not_a_500():
    """Enforcement blocking a task is a task outcome, not a transport error --
    the handler returns a failed Task rather than raising."""

    class ToolBlockedHandler:
        async def handle_task(self, task: Task) -> Task:
            return task.reply(
                "A rule stopped this task: blocked by z.py: nope.",
                state=TaskState.FAILED,
                reason=ReasonCode.TOOL_BLOCKED,
            )

    client = TestClient(build_a2a_app(ToolBlockedHandler(), card=CARD))
    response = client.post("/a2a/tasks", json=cabal_task("deploy"))
    assert response.status_code == 200
    task = response.json()
    assert task["status"]["state"] == "failed"
    assert task["metadata"]["daedalus_reason_code"] == 703
    assert task["status"]["message"]["parts"][0]["text"] == (
        "A rule stopped this task: blocked by z.py: nope."
    )


# ---- reviewed CRITICAL: a blocked draft leaves no trace on the wire ---------


def test_peer_dispatch_blocked_draft_never_appears_in_response_payload(tmp_path):
    """post_draft blocks -> the drafted secret must not leak to an a2a caller
    anywhere in the response body. task_to_a2a serializes history verbatim,
    so this is an end-to-end check with a real Agent, not just agent_text()."""
    bundle_path = _post_draft_blocking_bundle(tmp_path)
    agent = Agent(Bundle.load(bundle_path), llm=ScriptedLLM("the secret is 42"))
    client = TestClient(build_a2a_app(agent, card=CARD))

    response = client.post("/a2a/tasks", json=cabal_task("hi"))
    assert response.status_code == 200
    assert "the secret is 42" not in response.text

    task = response.json()
    assert task["status"]["state"] == "failed"
    assert task["status"]["message"]["parts"][0]["text"] == (
        "A rule stopped this reply: blocked by z_reject.py: leaks a secret."
    )


def test_taskid_continuation_never_refeeds_a_blocked_draft_to_the_model(tmp_path):
    """The a2a taskId-continuation path persists Task across turns -- exactly
    the transport the finding calls out (alongside Slack threads). A draft
    blocked on turn 1 must not resurface in the model's messages on turn 2."""
    bundle_path = _post_draft_blocking_bundle(tmp_path)
    llm = ScriptedLLM("the secret is 42", "moving on")
    agent = Agent(Bundle.load(bundle_path), llm=llm)
    client = TestClient(build_a2a_app(agent, card=CARD))

    first = send_text(client, "hi")["result"]
    assert first["status"]["state"] == "failed"

    send_text(client, "again", taskId=first["id"])

    assert len(llm.calls) == 2
    second_turn_messages = llm.calls[1]
    assert not any(
        "the secret is 42" in (message.get("content") or "")
        for message in second_turn_messages
    )


# ---- the boot line states the auth regime; an open listener warns (#204) ----


def test_listening_line_names_the_auth_regime():
    from daedalus.transports.a2a import A2aTransport

    assert A2aTransport(host="0.0.0.0", port=8080).listening_line() == (
        "(a2a listening on 0.0.0.0:8080, NO AUTH)"
    )
    with_auth = A2aTransport(host="0.0.0.0", port=8080, verifier=object())
    assert with_auth.listening_line() == "(a2a listening on 0.0.0.0:8080, JWT auth)"


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::", "agent.svc"])
def test_no_auth_on_a_reachable_address_warns(host):
    from daedalus.transports.a2a import A2aTransport

    warning = A2aTransport(host=host, port=8080, name="scout").open_warning()
    assert warning and "no auth" in warning
    assert "bundle scout" in warning and f"{host}:8080" in warning
    assert "every tool the allowlist admits" in warning and "Add an auth block" in warning


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_no_auth_on_loopback_does_not_warn(host):
    from daedalus.transports.a2a import A2aTransport

    assert A2aTransport(host=host).open_warning() is None


def test_auth_on_any_address_does_not_warn():
    from daedalus.transports.a2a import A2aTransport

    assert A2aTransport(host="0.0.0.0", verifier=object()).open_warning() is None


async def _serve_once(transport, monkeypatch):
    """Run serve() with uvicorn stubbed out, so only the boot output happens."""
    import uvicorn

    class _Server:
        def __init__(self, config):
            pass

        async def serve(self):
            pass

    monkeypatch.setattr(uvicorn, "Server", _Server)
    await transport.serve(handler=None)


@pytest.mark.asyncio
async def test_serve_logs_and_prints_the_warning_when_open(monkeypatch, caplog, capsys):
    from daedalus.transports.a2a import A2aTransport

    with caplog.at_level("WARNING", logger="daedalus.transports.a2a"):
        await _serve_once(A2aTransport(host="0.0.0.0", port=8080, name="scout"), monkeypatch)
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "bundle scout" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs", [{"host": "127.0.0.1"}, {"host": "0.0.0.0", "verifier": object()}]
)
async def test_serve_is_quiet_on_loopback_or_with_auth(monkeypatch, caplog, capsys, kwargs):
    from daedalus.transports.a2a import A2aTransport

    with caplog.at_level("WARNING", logger="daedalus.transports.a2a"):
        await _serve_once(A2aTransport(name="scout", **kwargs), monkeypatch)
    assert not caplog.records
    assert capsys.readouterr().err == ""
