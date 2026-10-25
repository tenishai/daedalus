"""Task ownership on the a2a transport (#197).

With a verifier configured, a stored task belongs to the sub that created it:
another sub's tasks/get and message/send continuation get the same answer as a
missing id, and POST /a2a/tasks never replaces a stored task. With no
verifier there is no caller identity, and every task stays visible to every
caller, as before.
"""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from daedalus.task import Task  # noqa: E402
from daedalus.transports.a2a import build_a2a_app  # noqa: E402
from daedalus.transports.a2a_auth import AuthError  # noqa: E402

ALICE = "operator:alice"
BOB = "persona:bob"


class TokenVerifier:
    """Bearer <name> -> {"sub": <the sub mapped to name>}; "nosub" verifies
    with no sub claim; anything else fails."""

    subs = {"alice": ALICE, "bob": BOB}

    async def verify_header(self, header: str | None) -> dict:
        token = (header or "").removeprefix("Bearer ")
        if token == "nosub":
            return {}
        if token in self.subs:
            return {"sub": self.subs[token]}
        raise AuthError("bad token")


class CountingHandler:
    def __init__(self):
        self.calls = 0

    async def handle_task(self, task: Task) -> Task:
        self.calls += 1
        return task.reply(f"echo: {task.user_text()}")


def as_(name: str | None) -> dict:
    return {"Authorization": f"Bearer {name}"} if name else {}


def send(client, who, text, task_id=None):
    message = {"role": "user", "parts": [{"kind": "text", "text": text}]}
    if task_id is not None:
        message["taskId"] = task_id
    body = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": message}}
    return client.post("/", json=body, headers=as_(who)).json()


def get(client, who, task_id):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": task_id}}
    return client.post("/", json=body, headers=as_(who)).json()


def peer(client, who, task_id, text):
    body = {
        "id": task_id,
        "status": {"message": {"role": "user", "parts": [{"kind": "text", "text": text}]}},
    }
    return client.post("/a2a/tasks", json=body, headers=as_(who))


def history_texts(result: dict) -> list[str]:
    return [m["parts"][0]["text"] for m in result["history"]]


def not_found_shape(response: dict, task_id: str) -> dict:
    """The error with the id taken out, so two ids compare by shape."""
    error = dict(response["error"])
    error["message"] = error["message"].replace(repr(task_id), "<id>")
    return error


@pytest.fixture()
def handler():
    return CountingHandler()


@pytest.fixture()
def client(handler):
    return TestClient(build_a2a_app(handler, verifier=TokenVerifier()))


def test_another_sub_cannot_read_a_task_and_sees_the_missing_id_answer(client):
    tid = send(client, "alice", "alice's private question")["result"]["id"]

    bobs = get(client, "bob", tid)
    missing = get(client, "bob", "no-such-task")

    assert "result" not in bobs
    assert not_found_shape(bobs, tid) == not_found_shape(missing, "no-such-task")
    assert "alice" not in str(bobs)


def test_another_sub_cannot_continue_a_task(client, handler):
    tid = send(client, "alice", "alice's private question")["result"]["id"]
    calls = handler.calls

    bobs = send(client, "bob", "bob continues", task_id=tid)
    missing = send(client, "bob", "bob continues", task_id="no-such-task")

    assert "result" not in bobs
    assert not_found_shape(bobs, tid) == not_found_shape(missing, "no-such-task")
    assert handler.calls == calls, "the handler ran a turn on another caller's task"
    assert history_texts(get(client, "alice", tid)["result"]) == [
        "alice's private question",
        "echo: alice's private question",
    ]


def test_the_owner_reads_and_continues_its_own_task(client):
    """cabal's re-mint copies the caller's sub verbatim, so a human or kobo
    continuing its own task through cabal arrives with the same sub."""
    tid = send(client, "alice", "first")["result"]["id"]

    again = send(client, "alice", "second", task_id=tid)

    assert again["result"]["status"]["state"] == "completed"
    assert history_texts(get(client, "alice", tid)["result"])[-2:] == ["second", "echo: second"]


def test_a_peer_dispatched_task_is_owned_too(client):
    assert peer(client, "alice", "t-alice", "via cabal").status_code == 200

    assert "error" in get(client, "bob", "t-alice")
    assert "error" in send(client, "bob", "hijack", task_id="t-alice")
    assert get(client, "alice", "t-alice")["result"]["id"] == "t-alice"


def test_peer_dispatch_refuses_an_existing_id_and_keeps_the_stored_task(client, handler):
    tid = send(client, "alice", "alice's private question")["result"]["id"]
    calls = handler.calls

    overwrite = peer(client, "bob", tid, "overwrite")

    assert overwrite.status_code == 409
    assert "already exists" in overwrite.json()["error"]
    assert handler.calls == calls
    assert history_texts(get(client, "alice", tid)["result"])[0] == "alice's private question"


def test_peer_dispatch_refuses_the_owners_own_repeated_id(client):
    assert peer(client, "alice", "t-1", "once").status_code == 200
    assert peer(client, "alice", "t-1", "twice").status_code == 409
    assert history_texts(get(client, "alice", "t-1")["result"])[0] == "once"


def test_a_token_without_a_sub_sees_no_task(client):
    """Fail closed: under auth, a sub-less token has no identity to own or
    match, so it reads nothing, its own tasks included."""
    owned = send(client, "alice", "alice's")["result"]["id"]
    subless = send(client, "nosub", "mine?")["result"]["id"]

    assert "error" in get(client, "nosub", owned)
    assert "error" in get(client, "nosub", subless)
    assert "error" in get(client, "alice", subless)


def test_without_a_verifier_every_task_stays_visible(handler):
    client = TestClient(build_a2a_app(handler))
    tid = send(client, None, "open question")["result"]["id"]

    assert get(client, None, tid)["result"]["id"] == tid
    assert send(client, None, "more", task_id=tid)["result"]["status"]["state"] == "completed"
    assert peer(client, None, "t-open", "hello").status_code == 200
    assert get(client, None, "t-open")["result"]["id"] == "t-open"
    # The 409 for a reused id applies without auth too.
    assert peer(client, None, "t-open", "again").status_code == 409
    assert history_texts(get(client, None, "t-open")["result"])[0] == "hello"
