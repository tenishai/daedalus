"""JWT verification on the a2a transport — the cabal integration round.

Adversarial per design §5: forged algorithms, wrong audience, expiry,
garbage tokens, and key rotation all must fail closed (or rotate cleanly).
"""

import time

import pytest

pytest.importorskip("fastapi")
joserfc = pytest.importorskip("joserfc")

from fastapi.testclient import TestClient  # noqa: E402
from joserfc import jwt  # noqa: E402
from joserfc.jwk import OctKey, RSAKey  # noqa: E402

from daedalus.task import VERIFIED_SUB_KEY, Task  # noqa: E402
from daedalus.transports.a2a import build_a2a_app  # noqa: E402
from daedalus.transports.a2a_auth import AuthError, JwtVerifier  # noqa: E402

ISSUER = "cabal-core"
AUDIENCE = "scout-svc"


class EchoHandler:
    async def handle_task(self, task: Task) -> Task:
        return task.reply(f"echo: {task.user_text()}")


class CountingFetch:
    """JWKS fetch seam — counts calls, serves a (mutable) key set."""

    def __init__(self, *keys: RSAKey):
        self.keys = list(keys)
        self.calls = 0

    async def __call__(self) -> dict:
        self.calls += 1
        return {"keys": [key.as_dict(private=False) for key in self.keys]}


def make_key(kid: str) -> RSAKey:
    return RSAKey.generate_key(2048, {"kid": kid})


def mint(
    key,
    *,
    kid: str = "k1",
    alg: str = "RS256",
    iss: str = ISSUER,
    aud: str = AUDIENCE,
    exp_delta: int = 300,
    iat_delta: int = 0,
) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "sub": "rook:1",
        "aud": aud,
        "iat": now + iat_delta,
        "exp": now + exp_delta,
    }
    return jwt.encode({"alg": alg, "kid": kid}, claims, key)


def verifier_with(fetch: CountingFetch) -> JwtVerifier:
    return JwtVerifier("http://unused/.well-known/jwks.json", ISSUER, AUDIENCE, fetch=fetch)


def peer_body(text="hello"):
    return {
        "id": "task-1",
        "status": {"state": "submitted"},
        "history": [{"role": "user", "parts": [{"kind": "text", "text": text}]}],
        "metadata": {},
    }


@pytest.fixture()
def key():
    return make_key("k1")


@pytest.fixture()
def client(key):
    fetch = CountingFetch(key)
    return TestClient(build_a2a_app(EchoHandler(), verifier=verifier_with(fetch)))


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_valid_token_passes_both_dispatch_surfaces(client, key):
    token = mint(key)
    peer = client.post("/a2a/tasks", json=peer_body(), headers=auth_header(token))
    assert peer.status_code == 200
    assert peer.json()["status"]["state"] == "completed"

    rpc = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}},
        },
        headers=auth_header(token),
    )
    assert rpc.json()["result"]["status"]["state"] == "completed"


def test_missing_token_is_401_on_both_surfaces(client):
    for path, body in (("/a2a/tasks", peer_body()), ("/", {"jsonrpc": "2.0"})):
        response = client.post(path, json=body)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert "unauthorized" in response.json()["error"]


def test_wrong_audience_rejected(client, key):
    token = mint(key, aud="someone-else")
    response = client.post("/a2a/tasks", json=peer_body(), headers=auth_header(token))
    assert response.status_code == 401
    assert "audience mismatch" in response.json()["error"]


def test_wrong_issuer_rejected(client, key):
    token = mint(key, iss="rogue-cabal")
    response = client.post("/a2a/tasks", json=peer_body(), headers=auth_header(token))
    assert response.status_code == 401


def test_expired_token_rejected(client, key):
    token = mint(key, exp_delta=-120)  # beyond the 30s leeway
    response = client.post("/a2a/tasks", json=peer_body(), headers=auth_header(token))
    assert response.status_code == 401
    assert "expired" in response.json()["error"]


def test_future_iat_rejected(client, key):
    token = mint(key, iat_delta=300)
    response = client.post("/a2a/tasks", json=peer_body(), headers=auth_header(token))
    assert response.status_code == 401


def test_hs256_forgery_rejected(client):
    """A symmetric token wearing a known kid must not pass the RS256 pin."""
    forged = mint(OctKey.import_key("attacker-secret-attacker-secret!"), alg="HS256")
    response = client.post("/a2a/tasks", json=peer_body(), headers=auth_header(forged))
    assert response.status_code == 401


def test_garbage_token_never_touches_the_jwks(key):
    """Unauthenticated garbage must not become fetch amplification."""
    fetch = CountingFetch(key)
    client = TestClient(build_a2a_app(EchoHandler(), verifier=verifier_with(fetch)))
    response = client.post(
        "/a2a/tasks", json=peer_body(), headers=auth_header("not-a-jwt-at-all")
    )
    assert response.status_code == 401
    assert fetch.calls == 0


async def test_key_rotation_triggers_exactly_one_refetch(key):
    fetch = CountingFetch(key)
    verifier = verifier_with(fetch)
    assert await verifier.verify(mint(key))  # warms the cache
    assert fetch.calls == 1

    rotated = make_key("k2")
    fetch.keys.append(rotated)  # the cabal rotated; JWKS now serves both
    claims = await verifier.verify(mint(rotated, kid="k2"))
    assert claims["sub"] == "rook:1"
    assert fetch.calls == 2


async def test_unknown_kid_after_refresh_fails_closed(key):
    fetch = CountingFetch(key)
    verifier = verifier_with(fetch)
    ghost = make_key("k9")  # never in the JWKS
    with pytest.raises(AuthError, match="unknown signing key"):
        await verifier.verify(mint(ghost, kid="k9"))
    assert fetch.calls == 2  # initial + one rotation refresh, no more


def test_gets_stay_open_when_auth_is_configured(client):
    """Auth gates dispatch. The card is a public document (cabal serves its
    agents' cards openly too); the events door is the kaminos-auth item."""
    assert client.get("/healthz").status_code == 200
    assert client.get("/.well-known/agent-card.json").status_code == 200


def test_agent_wiring_validates_auth_config():
    from daedalus.agent import Agent
    from daedalus.bundle import BundleError

    assert Agent._a2a_verifier(None) is None
    verifier = Agent._a2a_verifier(
        {"jwks_url": "http://c:7400/.well-known/jwks.json", "issuer": ISSUER, "audience": AUDIENCE}
    )
    assert isinstance(verifier, JwtVerifier)

    with pytest.raises(BundleError, match="issuer, audience"):
        Agent._a2a_verifier({"jwks_url": "http://c:7400/jwks.json"})
    with pytest.raises(BundleError, match="mapping"):
        Agent._a2a_verifier("not-a-dict")


# ---- stamping the verified subject onto task metadata (Task 2) -------------


class FakeVerifier:
    """Duck-typed verifier: always passes, returning whatever claims it was
    built with. The stamping tests don't need real crypto -- that's covered
    above -- only control over what verify_header hands back."""

    def __init__(self, claims: dict):
        self.claims = claims

    async def verify_header(self, authorization: str | None) -> dict:
        return self.claims


class RecordingHandler:
    """Records the Task it receives so a test can inspect the metadata the
    transport stamped before handing off to the handler."""

    def __init__(self):
        self.received: Task | None = None

    async def handle_task(self, task: Task) -> Task:
        self.received = task
        return task.reply(f"echo: {task.user_text()}")


def test_peer_dispatch_stamps_verified_sub():
    handler = RecordingHandler()
    client = TestClient(
        build_a2a_app(handler, verifier=FakeVerifier({"sub": "service:room"}))
    )
    response = client.post(
        "/a2a/tasks", json=peer_body(), headers=auth_header("unused")
    )
    assert response.status_code == 200
    assert handler.received.metadata[VERIFIED_SUB_KEY] == "service:room"


def test_peer_dispatch_overwrites_wire_supplied_sub_when_verified():
    handler = RecordingHandler()
    client = TestClient(
        build_a2a_app(handler, verifier=FakeVerifier({"sub": "agent:ward"}))
    )
    body = peer_body()
    body["metadata"] = {VERIFIED_SUB_KEY: "service:forged"}
    response = client.post("/a2a/tasks", json=body, headers=auth_header("unused"))
    assert response.status_code == 200
    assert handler.received.metadata[VERIFIED_SUB_KEY] == "agent:ward"


def test_rpc_message_send_stamps_verified_sub():
    handler = RecordingHandler()
    client = TestClient(
        build_a2a_app(handler, verifier=FakeVerifier({"sub": "service:room"}))
    )

    fresh = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}},
        },
        headers=auth_header("unused"),
    ).json()
    assert handler.received.metadata[VERIFIED_SUB_KEY] == "service:room"
    task_id = fresh["result"]["id"]

    client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "again"}],
                    "taskId": task_id,
                }
            },
        },
        headers=auth_header("unused"),
    )
    assert handler.received.metadata[VERIFIED_SUB_KEY] == "service:room"


def test_verifier_claims_without_sub_leave_key_absent():
    handler = RecordingHandler()
    client = TestClient(build_a2a_app(handler, verifier=FakeVerifier({})))
    response = client.post(
        "/a2a/tasks", json=peer_body(), headers=auth_header("unused")
    )
    assert response.status_code == 200
    assert VERIFIED_SUB_KEY not in handler.received.metadata
