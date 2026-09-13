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


def _kid_only_token(kid: str) -> str:
    """A token with a WELL-FORMED header naming `kid` and nothing valid after.

    This is what an attacker sends, and what the pre-#44 code called "not
    garbage": `_header_kid` needs only a base64-decodable JSON header with a
    string kid -- no signature, no authentication -- so each one reached the
    kid lookup and, missing, triggered a JWKS fetch."""
    import base64 as _b64
    import json as _json

    header = _b64.urlsafe_b64encode(
        _json.dumps({"alg": "RS256", "kid": kid}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.eyJhIjoxfQ.not-a-real-signature"


async def test_a_flood_of_unknown_kids_does_not_become_a_flood_of_FETCHES(key):
    """#44: the amplification the old docstring said could not happen.

    `test_garbage_token_never_touches_the_jwks` above is true and tests the
    EASY half -- an unparseable token. It passed while this was wide open,
    which is how the claim looked verified. Measured before the fix: 20
    unauthenticated requests with random kids produced 20 outbound GETs to
    the cabal's JWKS endpoint, from a surface that is reachable before auth
    by construction."""
    fetch = CountingFetch(key)
    verifier = verifier_with(fetch)
    assert await verifier.verify(mint(key))  # warm the cache
    warm = fetch.calls

    for i in range(20):
        with pytest.raises(AuthError, match="unknown signing key"):
            await verifier.verify(_kid_only_token(f"attacker-{i}"))

    # One refresh for the first miss; the rest are refused from memory.
    assert fetch.calls - warm == 1, (
        f"{fetch.calls - warm} fetches for 20 unauthenticated requests — "
        "an unknown kid is still an outbound fetch trigger"
    )


async def test_a_REVOKED_key_stops_verifying_once_the_cache_expires(key, monkeypatch):
    """#44, and the serious half.

    Refresh is gated on a kid being ABSENT, so rotation works and revocation
    produces no signal at all: the kid is still cached, nothing asks again,
    and a key pulled from the JWKS goes on verifying for the life of the
    process. Measured before the fix: still accepted after the endpoint began
    serving an empty key set."""
    fetch = CountingFetch(key)
    verifier = verifier_with(fetch)
    assert await verifier.verify(mint(key))

    # The operator pulls the compromised key and the endpoint serves its
    # replacement. NOT an empty set: emptying it fails `import_key_set`
    # outright, which is fail-closed but by a different door and would make
    # this test pass without exercising the TTL at all.
    fetch.keys[:] = [make_key("k-replacement")]

    # Still accepted inside the TTL — that window is the deliberate cost.
    assert await verifier.verify(mint(key))

    monkeypatch.setattr("daedalus.transports.a2a_auth.KEYS_TTL_SECONDS", 0.0)
    with pytest.raises(AuthError, match="unknown signing key"):
        await verifier.verify(mint(key))


async def test_the_cooldown_expires_so_a_ROTATION_during_a_flood_still_works(
    key, monkeypatch
):
    """The fix must not invent an availability bug.

    The cooldown refuses an unknown kid without fetching — so a genuinely new
    key that arrives while it is running is refused too. That is the cost, and
    it has to be bounded: once the cooldown expires the rotation must land."""
    fetch = CountingFetch(key)
    verifier = verifier_with(fetch)
    assert await verifier.verify(mint(key))

    with pytest.raises(AuthError):
        await verifier.verify(_kid_only_token("k2"))  # probed before it exists

    rotated = make_key("k2")
    fetch.keys.append(rotated)  # now the cabal really does rotate to k2
    monkeypatch.setattr("daedalus.transports.a2a_auth.REFRESH_COOLDOWN_SECONDS", 0.0)
    claims = await verifier.verify(mint(rotated, kid="k2"))
    assert claims["sub"] == "rook:1"


async def test_the_ttl_does_not_refetch_on_every_verification(key):
    """A TTL that expired instantly would turn every request into a JWKS GET,
    which is the amplification this file also exists to prevent."""
    fetch = CountingFetch(key)
    verifier = verifier_with(fetch)
    for _ in range(10):
        assert await verifier.verify(mint(key))
    assert fetch.calls == 1


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
    above -- only control over what verify_header hands back.

    Supports returning different claims on successive calls via claims_sequence.
    """

    def __init__(self, claims: dict | None = None, claims_sequence: list[dict] | None = None):
        self.claims = claims
        self.claims_sequence = claims_sequence or []
        self.call_count = 0

    async def verify_header(self, authorization: str | None) -> dict:
        if self.claims_sequence:
            result = self.claims_sequence[self.call_count % len(self.claims_sequence)]
            self.call_count += 1
            return result
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
    """The continuation-turn (taskId) stamp MUST refresh, not persist from turn one.
    This test proves the stamp is stamped per-request by using different verifier
    claims on the second (taskId) request and asserting the metadata was updated."""
    handler = RecordingHandler()
    # Use claims_sequence to return different sub on the second call
    verifier = FakeVerifier(
        claims_sequence=[
            {"sub": "service:room"},    # First request (fresh task)
            {"sub": "service:other"},   # Second request (continuation)
        ]
    )
    client = TestClient(build_a2a_app(handler, verifier=verifier))

    # First request: create a new task
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

    # Second request: continuation turn with different claims
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
    # PROVE the stamp was refreshed: it should be the NEW sub, not the old one
    assert handler.received.metadata[VERIFIED_SUB_KEY] == "service:other"


def test_verifier_claims_without_sub_leave_key_absent():
    handler = RecordingHandler()
    client = TestClient(build_a2a_app(handler, verifier=FakeVerifier({})))
    response = client.post(
        "/a2a/tasks", json=peer_body(), headers=auth_header("unused")
    )
    assert response.status_code == 200
    assert VERIFIED_SUB_KEY not in handler.received.metadata
