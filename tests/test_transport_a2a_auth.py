"""JWT verification on the a2a transport — the cabal integration round.

Adversarial per design §5: forged algorithms, wrong audience, expiry,
garbage tokens, and key rotation all must fail closed (or rotate cleanly).
"""

import asyncio
import logging
import time

import pytest

pytest.importorskip("fastapi")
joserfc = pytest.importorskip("joserfc")

from fastapi.testclient import TestClient  # noqa: E402
from joserfc import jwt  # noqa: E402
from joserfc.jwk import OctKey, RSAKey  # noqa: E402

from daedalus.task import VERIFIED_SUB_KEY, Task  # noqa: E402
from daedalus.transports.a2a import build_a2a_app  # noqa: E402
from daedalus.transports.a2a_auth import (  # noqa: E402
    KEYS_TTL_SECONDS,
    REFRESH_COOLDOWN_SECONDS,
    AuthError,
    JwtVerifier,
)  # noqa: E402

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
    process. Measured before the fix: still accepted after the endpoint
    stopped serving the key."""
    fetch = CountingFetch(key)
    verifier = verifier_with(fetch)
    assert await verifier.verify(mint(key))

    # The operator pulls the compromised key and the endpoint serves its
    # REPLACEMENT. Not an empty set, and the reason is not the one an earlier
    # version of this comment gave.
    #
    # I wrote that an empty set "would make this test pass without exercising
    # the TTL". That is backwards, and I had the measurement in front of me
    # when I wrote it: an empty set makes `import_key_set` raise, `_keys()`
    # re-raises it as `could not load JWKS ... No keys to import`, and the
    # `match=` below does NOT match that -- so the test FAILS. It was a
    # rationalisation, not a finding (#50 r1).
    #
    # The real reason to serve a replacement is that it models revocation
    # rather than outage. An empty set proves only that an unreadable JWKS
    # fails closed, which is a coarser and less interesting property; a
    # healthy endpoint that no longer lists ONE key is what an operator
    # actually does when a key is compromised, and it is the case where the
    # cached set would otherwise still verify it.
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
    _cool_down(verifier)
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


def _cool_down(verifier) -> None:
    """Age the cooldown out, ONCE — the `_age_out` principle, other constant.

    NOT by pinning REFRESH_COOLDOWN_SECONDS to 0, which DELETES the mechanism
    rather than ageing past it. That is the same mistake `_age_out` exists to
    prevent wearing the other constant's name, and it shipped here: the
    rotation-during-a-flood test pinned the constant to zero, so a mutant that
    turned the fixed cooldown into a SLIDING window -- restamping
    `_refreshed_at` on every refusal, which a sustained flood then holds open
    forever -- passed all 25 tests (#50 r2 nit)."""
    verifier._refreshed_at -= REFRESH_COOLDOWN_SECONDS * 10


def _age_out(verifier) -> None:
    """Age the cached key set past its TTL, ONCE.

    NOT by pinning KEYS_TTL_SECONDS to 0 — that makes the cache permanently
    stale, so every call refetches no matter when the stamp lands, and the
    test then fails against the FIX as loudly as against the bug. (It did.)
    Reality is a cache that ages out once and is then re-stamped; winding the
    stamp backwards models that and leaves the TTL doing its real job."""
    verifier._fetched_at -= KEYS_TTL_SECONDS * 10


class FlakyFetch(CountingFetch):
    """A JWKS that can be switched to failing, counting attempts either way."""

    def __init__(self, *keys):
        super().__init__(*keys)
        self.down = False

    async def __call__(self) -> dict:
        self.calls += 1
        if self.down:
            raise ConnectionError("connection refused")
        return {"keys": [key.as_dict(private=False) for key in self.keys]}


async def test_a_JWKS_OUTAGE_does_not_become_an_auth_outage(key):
    """#50 r1 Critical: stamping the SUCCESS left `_fetched_at` unadvanced on
    failure, so `_age` stayed over the TTL forever.

    Before this PR a token whose kid was already cached verified whether or
    not the endpoint was reachable — a blip was survivable indefinitely. The
    TTL turned that into 100% auth failure one TTL into any outage, and
    nothing in the diff claimed that as a design: the cached set is still
    populated and still correct while it happens.
    """
    fetch = FlakyFetch(key)
    verifier = verifier_with(fetch)
    assert await verifier.verify(mint(key))  # warm

    fetch.down = True
    _age_out(verifier)

    assert await verifier.verify(mint(key)), "a cached, still-correct key stopped verifying"


async def test_an_outage_does_not_reopen_the_fetch_amplification(key):
    """The same root cause, and the nastier half: the amplification this
    module exists to close came back, aimed at an endpoint already degraded.
    Measured before the fix, endpoint down: 20 requests -> 20 GETs."""
    fetch = FlakyFetch(key)
    verifier = verifier_with(fetch)
    assert await verifier.verify(mint(key))
    fetch.down = True
    _age_out(verifier)

    before = fetch.calls
    for _ in range(20):
        await verifier.verify(mint(key))

    assert fetch.calls - before <= 2, (
        f"{fetch.calls - before} fetches for 20 requests during an outage"
    )


async def test_a_COLD_start_with_no_keys_still_fails_closed(key):
    """Serve-stale must not become serve-nothing: with no cached set there is
    nothing to serve, and the caller has to hear about it."""
    fetch = FlakyFetch(key)
    fetch.down = True
    verifier = verifier_with(fetch)

    with pytest.raises(AuthError, match="could not load JWKS"):
        await verifier.verify(mint(key))


async def test_a_TTL_expiry_is_not_a_thundering_herd(key):
    """#50 r1 Important, and it needs no outage. One verifier serves the whole
    process on one loop, so N concurrent requests all read `_age >= TTL`
    before any of them stamps. The unknown-kid path already stamped before its
    await, which is what made the asymmetry visible."""
    # A FETCH THAT ACTUALLY YIELDS. `CountingFetch` is `async def` with no
    # await inside, so it completes without ever suspending -- the gather runs
    # the twenty coroutines to completion one at a time and NOTHING
    # interleaves. This test passed against the bug for that reason until the
    # mutant caught it. Concurrency needs a suspension point to be concurrent.
    class SlowFetch(CountingFetch):
        async def __call__(self) -> dict:
            self.calls += 1
            await asyncio.sleep(0.01)
            return {"keys": [k.as_dict(private=False) for k in self.keys]}

    fetch = SlowFetch(key)
    verifier = verifier_with(fetch)
    assert await verifier.verify(mint(key))
    _age_out(verifier)

    before = fetch.calls
    await asyncio.gather(*(verifier.verify(mint(key)) for _ in range(20)))

    assert fetch.calls - before == 1, (
        f"{fetch.calls - before} concurrent fetches at TTL expiry"
    )


class _SlowFetch(CountingFetch):
    """A fetch that suspends, so concurrent callers actually interleave."""

    async def __call__(self) -> dict:
        self.calls += 1
        await asyncio.sleep(0.01)
        return {"keys": [k.as_dict(private=False) for k in self.keys]}


async def test_a_COLD_herd_shares_one_fetch(key):
    """#55: `_key_set is None` stayed true for the whole of an in-flight cold
    fetch, so 20 concurrent verifies on a fresh process made 20 GETs. Every
    request must still succeed, off the one fetch."""
    fetch = _SlowFetch(key)
    verifier = verifier_with(fetch)

    results = await asyncio.gather(*(verifier.verify(mint(key)) for _ in range(20)))

    assert len(results) == 20
    assert fetch.calls == 1, f"{fetch.calls} concurrent cold fetches"


async def test_a_COLD_herd_against_a_dead_endpoint_shares_one_failure(key):
    """The queued callers share the winner's failure instead of each trying."""

    class DeadFetch(CountingFetch):
        async def __call__(self) -> dict:
            self.calls += 1
            await asyncio.sleep(0.01)
            raise OSError("endpoint down")

    fetch = DeadFetch(key)
    verifier = verifier_with(fetch)

    results = await asyncio.gather(
        *(verifier.verify(mint(key)) for _ in range(20)), return_exceptions=True
    )

    assert all(isinstance(r, AuthError) and "could not load JWKS" in str(r) for r in results)
    assert fetch.calls == 1


async def test_the_cooldown_sentinel_is_not_a_real_point_on_the_clock(key, monkeypatch):
    """#50 r1 nit: `time.monotonic()`'s epoch is undefined and is uptime on
    Linux, so `_refreshed_at = 0.0` refuses the first rotation of a process
    that starts inside the cooldown window."""
    fetch = CountingFetch(key)
    verifier = verifier_with(fetch)
    # WARM THE CACHE WITHOUT k2 FIRST. Appending the rotated key before the
    # first fetch puts it in the very first key set, so the kid is FOUND and
    # the cooldown branch is never entered -- this test passed against the bug
    # for exactly that reason until the mutant caught it.
    assert await verifier.verify(mint(key))

    monkeypatch.setattr("daedalus.transports.a2a_auth.time.monotonic", lambda: 3.0)
    rotated = make_key("k2")
    fetch.keys.append(rotated)
    claims = await verifier.verify(mint(rotated, kid="k2"))
    assert claims["sub"] == "rook:1"


def test_card_and_health_gets_stay_open_when_auth_is_configured(client):
    """The card is a public document (cabal serves its agents' cards openly
    too) and healthz carries nothing. /events is gated: see below."""
    assert client.get("/healthz").status_code == 200
    assert client.get("/.well-known/agent-card.json").status_code == 200


def _bus_with_a_secret():
    from daedalus.events import EventBus

    bus = EventBus()
    bus.publish({"type": "tool_result", "name": "files.read", "content": "SECRET-BODY"})
    return bus


def test_events_without_a_token_is_401_when_auth_is_configured(key, monkeypatch):
    """#196: the replay alone holds the last 100 events, tool results included.
    A finite stream stands in so a regression fails instead of hanging."""
    import daedalus.transports.a2a as a2a

    async def finite(events, content=False):
        yield "data: SECRET-BODY\n\n"

    monkeypatch.setattr(a2a, "sse_stream", finite)
    client = TestClient(
        build_a2a_app(
            EchoHandler(), events=_bus_with_a_secret(), verifier=verifier_with(CountingFetch(key))
        )
    )
    for headers in ({}, auth_header("not-a-jwt"), auth_header(mint(key, aud="someone-else"))):
        response = client.get("/events", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert "SECRET-BODY" not in response.text


def test_events_with_a_valid_token_streams(key, monkeypatch):
    """The authorised path still reaches the stream. The real stream never
    ends, and TestClient buffers to completion, so a finite one stands in."""
    import daedalus.transports.a2a as a2a

    async def finite(events, content=False):
        yield "data: {}\n\n"

    monkeypatch.setattr(a2a, "sse_stream", finite)
    client = TestClient(
        build_a2a_app(
            EchoHandler(), events=_bus_with_a_secret(), verifier=verifier_with(CountingFetch(key))
        )
    )
    response = client.get("/events", headers=auth_header(mint(key)))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


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


async def test_the_cooldown_does_not_SLIDE_under_a_sustained_flood(key, monkeypatch):
    """#50 r2 nit: a mutant turning the fixed cooldown into a sliding window
    passed all 25 tests, because the only test exercising the cooldown's
    EXPIRY had pinned the constant to zero — deleting the mechanism rather
    than ageing past it, which is the mistake `_age_out` exists to prevent.

    THE FLOOD HAS TO CONTINUE WHILE THE ROTATION IS TRIED, and my first
    version of this test got that wrong: it flooded, stopped, then tried the
    rotation, and the correct code refused it too — legitimately, because
    under 10s had passed since the last real refresh. Stopping the flood makes
    both implementations converge, so it discriminates nothing.

    Interleaved, they diverge. Correct: every ~10s one attempt gets a refresh
    and finds the rotated key. Sliding: every refusal restamps, so a flood at
    any interval under the cooldown holds the window open and the rotation is
    never picked up.

    The blast radius is capped at KEYS_TTL_SECONDS -- the TTL refetch picks up
    a rotated key independently -- so this costs rotation latency rather than
    indefinite refusal. That is why it is a nit, and why the window below stays
    inside one TTL: past it the TTL would mask the mutant.
    """
    now = [1_000.0]
    monkeypatch.setattr("daedalus.transports.a2a_auth.time.monotonic", lambda: now[0])
    fetch = CountingFetch(key)
    verifier = verifier_with(fetch)
    assert await verifier.verify(mint(key))  # warm; stamps _fetched_at at 1000

    # THE ROTATION MUST ARRIVE AFTER THE FIRST REFRESH, and my first version
    # of this test appended it before the flood started. The first flood token
    # is always ALLOWED a refresh (`_since_refresh` is -inf), so that refresh
    # picked up k2 immediately and the cooldown was never consulted — the test
    # passed under the mutant and under the fix alike. Third time in this file
    # a test of mine passed for a reason unrelated to its name.
    rotated = make_key("k2")
    for i in range(3):
        now[0] += 1.0
        with pytest.raises(AuthError, match="unknown signing key"):
            await verifier.verify(_kid_only_token(f"pre-{i}"))
    fetch.keys.append(rotated)  # the cabal rotates, with a warm cache lacking k2

    accepted_at = None
    for i in range(40):
        now[0] += 1.0
        if now[0] - 1_000.0 >= KEYS_TTL_SECONDS:
            break  # past here the TTL would refetch anyway and mask the mutant
        # the flood never repeats a kid, so a per-kid memory would not help
        with pytest.raises(AuthError, match="unknown signing key"):
            await verifier.verify(_kid_only_token(f"flood-{i}"))
        try:
            claims = await verifier.verify(mint(rotated, kid="k2"))
        except AuthError:
            continue
        assert claims["sub"] == "rook:1"
        accepted_at = now[0] - 1_000.0
        break

    assert accepted_at is not None, (
        "a rotation was never picked up during a sustained flood — the "
        "cooldown slid instead of expiring"
    )
    assert accepted_at <= REFRESH_COOLDOWN_SECONDS * 2, (
        f"the rotation took {accepted_at}s against a {REFRESH_COOLDOWN_SECONDS}s cooldown"
    )


async def test_serving_a_STALE_key_set_says_so(key, caplog):
    """#50 r2: "not invisibly" was a property the module did not have.

    The `except` swallowed the failure with no log, no counter and no readable
    state, so from the agent's side auth simply kept succeeding on a stale set
    for as long as the endpoint stayed down. The comment claiming the trade was
    visible is what made it sound like housekeeping.

    Choosing to keep serving a stale set is exactly the decision that wants a
    record, so the record is pinned here — a downgrade to debug leaves the
    clause false again and nothing else would notice.
    """
    fetch = FlakyFetch(key)
    verifier = verifier_with(fetch)
    assert await verifier.verify(mint(key))

    fetch.down = True
    _age_out(verifier)

    with caplog.at_level(logging.WARNING):
        assert await verifier.verify(mint(key))

    assert "still serving the cached key set" in caplog.text
    assert verifier.jwks_url in caplog.text
    assert "Revocations cannot reach this verifier" in caplog.text


async def test_the_stale_age_GROWS_across_a_sustained_outage(key, monkeypatch, caplog):
    """#50 r3 pre-pass: the age reported was time-since-last-ATTEMPT.

    `_fetched_at` is stamped on every attempt — the r1 fix, correct for the
    TTL — so it cannot answer "how old is the data we are serving". Each
    failure restamped, and every line after the first said one TTL regardless
    of the true age: at t=120 with the last success at t=0 it claimed 60 when
    the answer was 120, understating without bound.

    That is the renews-per-attempt mechanism this module documents, biting the
    diagnostic added to make the trade visible — a log built for exactly the
    scenario it under-reported. `_loaded_at` tracks the last SUCCESS.
    """
    now = [1_000.0]
    monkeypatch.setattr("daedalus.transports.a2a_auth.time.monotonic", lambda: now[0])
    fetch = FlakyFetch(key)
    verifier = verifier_with(fetch)
    assert await verifier.verify(mint(key))  # success at t=1000
    fetch.down = True

    ages = []
    for t in (1_060.0, 1_120.0, 1_180.0):
        now[0] = t
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert await verifier.verify(mint(key))
        ages.append(caplog.text)

    assert "60s old" in ages[0]
    assert "120s old" in ages[1], "the age reset — it is measuring attempts, not staleness"
    assert "180s old" in ages[2]
