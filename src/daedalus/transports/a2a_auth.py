"""JWT verification for the a2a transport — the cabal integration round.

A cabal forwards the caller's bearer JWT on every dispatch (RS256, signed by
the cabal's active key, aud = this agent's federation audience). When a
bundle configures `auth` on its a2a transport, every POST must carry one:

    transports:
      - kind: a2a
        auth:
          jwks_url: http://127.0.0.1:7400/.well-known/jwks.json
          issuer: cabal-core
          audience: scout-svc

Claim verification mirrors cabal's own (core/auth/jwt.py): signature against
the JWKS, iss + aud pinned, exp/iat with 30s leeway — plus an explicit RS256
algorithm allowlist at decode. Requires joserfc + httpx (the a2a extra).

THE KEY CACHE IS NOT MIRRORED, AND ITS FAILURES ARE OURS (#44). cabal has no
JWKS cache at all — `verify` there is handed keys. Everything below about
TTLs, refetching and cooldowns exists only here, so "mirrors cabal's own" is
true of the claim checks and says nothing about the part that had the bugs.

One known divergence in the claim checks: `aud` is compared as a string, so a
list-valued audience is rejected even though RFC 7519 permits one. joserfc's
`decode` does not check `aud` at all without a claims registry, so that manual
comparison IS the whole check. cabal mints `aud` as a str, so nothing in the
integration presents a list today; this would bite the first time the config
points at another issuer.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable

from joserfc import jwt
from joserfc.jwk import KeySet

log = logging.getLogger(__name__)

LEEWAY_SECONDS = 30

#: How long a fetched JWKS may be trusted before it is refetched (#44).
#:
#: WITHOUT THIS, REVOCATION NEVER TAKES EFFECT. The refresh path is gated on
#: a kid being ABSENT, so rotation works -- a new kid is unknown and forces a
#: fetch -- while revocation is the exact opposite shape and produces no
#: signal at all. Pull a compromised key from the JWKS and this verifier goes
#: on accepting tokens signed by it for the life of the process.
#:
#: SIXTY SECONDS, BECAUSE `exp` BOUNDS NOTHING HERE (#50 r1). A signing key is
#: revoked when its PRIVATE half is compromised, and whoever holds that mints
#: fresh tokens at will -- so token expiry gives no bound at all, and this TTL
#: is the ENTIRE exposure window for unlimited forgery. The cost of shortening
#: it is FIVE TIMES THE PERIODIC FETCH RATE -- under steady traffic with no
#: unknown kids, one refetch per TTL becomes five per 300s, so four extra GETs
#: per five minutes per process. Lopsided enough to take without a
#: measurement. (An earlier version said "one extra GET per interval,
#: multiplied by five", which double-counts: multiplying an extra of one by
#: five asserts five extra, and the real figure is four.)
KEYS_TTL_SECONDS = 60.0

#: The minimum gap between two JWKS fetches, however many unknown kids arrive
#: in between (#44).
#:
#: AN UNKNOWN KID IS AN UNAUTHENTICATED FETCH TRIGGER, and the class docstring
#: used to claim the opposite ("a garbage token triggers none"). `_header_kid`
#: needs only a base64-decodable JSON header with a string `kid` -- no
#: signature, no authentication -- so a token with a random kid is "not
#: garbage" by this code's own test and cost one outbound GET each. Measured
#: before this: 20 such requests produced 20 fetches.
#:
#: THE OVERCLAIM WAS IN THE PARENTHETICAL, NOT IN THE TEST. `test_garbage_
#: token_never_touches_the_jwks` does exactly what its name says -- it sends
#: `not-a-jwt-at-all`, which is a garbage token, and no fetch follows. The
#: sentence that promised more was this docstring's "(no fetch amplification
#: from unauthenticated callers)", which generalised a narrow true result into
#: a claim about a whole class. That case is also the CHEAPEST to defend
#: rather than one nobody sends -- a fuzzer sends it constantly, and it is a
#: reasonable regression guard on its own terms (#50 r1).
#:
#: A COOLDOWN, NOT A PER-KID MEMORY, and the difference is the whole fix. My
#: first attempt remembered each kid it had missed and refused repeats without
#: fetching -- useless here, because an attacker sends a DIFFERENT random kid
#: every time and no remembered miss ever matches. Measured: still 20 fetches
#: for 20 requests. Bounding the fetch itself is the only thing that bounds an
#: attacker who never repeats himself.
#:
#: The cost is rotation latency: a genuinely new kid arriving inside the
#: cooldown is refused until it expires. Ten seconds keeps that unnoticeable
#: while cutting a flood by whatever its request rate is.
REFRESH_COOLDOWN_SECONDS = 10.0


class AuthError(Exception):
    """The request carries no token, or the token failed verification."""


def _header_kid(token: str) -> str | None:
    """The kid from the JOSE header, or None — cheap, before any crypto."""
    try:
        segment = token.split(".")[0]
        padded = segment + "=" * (-len(segment) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded))
        kid = header.get("kid")
        return kid if isinstance(kid, str) else None
    except Exception:
        return None


class JwtVerifier:
    """Verifies bearer JWTs against a JWKS endpoint.

    ROTATION AND REVOCATION ARE DIFFERENT SHAPES AND NEED DIFFERENT
    MECHANISMS (#44). Rotation is served by refetching on an unknown kid: the
    new key is absent, so it forces a fetch. Revocation produces no such
    signal -- the kid is still cached and nothing asks again -- so it is
    served by a TTL on the cached set instead. An earlier version had only
    the first and called itself "rotation-aware", which was true and read as
    covering both.

    The refetch an unknown kid triggers is reachable by an unauthenticated
    caller, so it is rate-limited. The cold leg -- `_key_set is None`, before
    any fetch has succeeded -- used to be bounded by nothing: 20 concurrent
    verifies on a cold process produced 20 GETs. All three legs now go through
    one single-flight lock (#55), so concurrent callers share one fetch. What
    stays open: with the endpoint down and no cached set, each LATER request
    (not concurrent ones) still tries one fetch."""

    def __init__(
        self,
        jwks_url: str,
        issuer: str,
        audience: str,
        fetch: Callable[[], Awaitable[dict]] | None = None,
    ):
        self.jwks_url = jwks_url
        self.issuer = issuer
        self.audience = audience
        self._fetch = fetch or self._http_fetch
        self._key_set: KeySet | None = None
        self._fetched_at: float = 0.0
        #: When a fetch last SUCCEEDED, as distinct from when one was last
        #: attempted (#50 r3 pre-pass).
        #:
        #: `_fetched_at` is stamped on every ATTEMPT -- that is the r1 fix and
        #: it is right for the TTL -- so it cannot answer "how old is the data
        #: we are serving".
        #:
        #: THE OLD CODE CAPTURED `stale_for = self._age` ON THE LINE ABOVE THE
        #: STAMP, and logged that variable from inside the `except`. So it
        #: reported the gap between the last two ATTEMPTS, not the age of the
        #: data: during a sustained outage each failure restamped, and every
        #: line said one TTL regardless of the real age. Measured against that
        #: exact tree, last success t=0, endpoint down:
        #:
        #:     t=60   "60s old"    true
        #:     t=120  "60s old"    the data was 120s old
        #:     t=180  "60s old"    the data was 180s old
        #:
        #: WHERE THE CAPTURE HAPPENS IS THE WHOLE MECHANISM, and an earlier
        #: version of this comment said only "reporting `_age` in the warning"
        #: -- which reads as evaluated AT the log line, where it would have been
        #: ~0 rather than 60. A reviewer took exactly that reading and reached a
        #: wrong conclusion from a true premise (#50 r3). The imprecision was
        #: mine; the numbers above are measured.
        #:
        #: That is the same renews-per-attempt mechanism this module documents
        #: two screens down, biting the diagnostic added to make the trade
        #: visible -- a log built for exactly the scenario it under-reports.
        self._loaded_at: float = 0.0
        # -inf, NOT 0.0, because `time.monotonic()`'s reference point is
        # UNDEFINED and is uptime on Linux (#50 r1 nit). A process starting
        # within REFRESH_COOLDOWN_SECONDS of that epoch would refuse its first
        # genuine rotation without a fetch -- measured with the clock stubbed,
        # refused at epoch+3.0s and accepted at epoch+30.0s. A sentinel cannot
        # be mistaken for a real reading.
        self._refreshed_at: float = float("-inf")
        # Single-flight state (#55): the lock, a count of fetch ATTEMPTS (so a
        # caller that waited can tell one happened), and the last cold failure.
        self._fetch_lock = asyncio.Lock()
        self._fetch_count = 0
        self._cold_failure = "could not load JWKS"

    async def _http_fetch(self) -> dict:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(self.jwks_url)
            response.raise_for_status()
            return response.json()

    @property
    def _age(self) -> float:
        """Seconds since the cached key set was fetched — the TTL's clock."""
        return time.monotonic() - self._fetched_at

    @property
    def _since_refresh(self) -> float:
        """Seconds since the last UNKNOWN-KID-triggered refresh — the
        cooldown's clock, and deliberately not `_age`.

        Timing the cooldown off any fetch refuses the FIRST unknown kid after
        every ordinary fetch, which is exactly when a real rotation arrives.
        That broke `test_key_rotation_triggers_exactly_one_refetch` when this
        was written, and it should: a cooldown is meant to bound a flood, not
        to make the first request of one indistinguishable from a rotation."""
        return time.monotonic() - self._refreshed_at

    async def _keys(self, *, refresh: bool = False) -> KeySet:
        if self._key_set is None or refresh or self._age >= KEYS_TTL_SECONDS:
            # SINGLE-FLIGHT (#55): one fetch at a time, and a caller that queued
            # behind it takes what that fetch produced instead of fetching again.
            # `_key_set is None` is true for the whole of an in-flight cold fetch
            # and no stamp clears it, so before this lock 20 concurrent verifies
            # on a fresh process made 20 GETs. The lock covers all three legs
            # (cold, TTL, unknown-kid refresh). `seen` is read BEFORE waiting, so
            # "a fetch happened while I waited" is `_fetch_count != seen`.
            #
            # The lock is held across the network await. A hung fetch therefore
            # holds every caller, where before each failed on its own; the
            # default fetch has a 5s client timeout, which bounds it. A custom
            # `fetch=` callable that can hang should carry its own timeout.
            seen = self._fetch_count
            async with self._fetch_lock:
                if self._fetch_count != seen:
                    if self._key_set is None:
                        # The fetch we queued behind failed cold: share its failure.
                        raise AuthError(self._cold_failure)
                    return self._key_set
                try:
                    # STAMP THE ATTEMPT, NOT THE SUCCESS, AND SERVE STALE ON FAILURE
                    # (#50 r1 Critical). Stamping after the await left `_fetched_at`
                    # unadvanced whenever a fetch raised, so `_age` stayed above the
                    # TTL FOREVER and every request re-entered the fetch. Two things
                    # fell out, and neither was fail-closed by design -- `_key_set` is
                    # still populated and still correct when it happens:
                    #   * the 1:1 unauthenticated amplification this module exists to
                    #     close came back, aimed at a JWKS endpoint already degraded
                    #     (measured, endpoint down: 20 requests -> 20 GETs);
                    #   * a JWKS blip, previously survivable indefinitely from cache,
                    #     became 100% auth failure one TTL in.
                    # The cooldown cannot bound either, because `_keys()` raises
                    # before `verify` ever reaches the `_since_refresh` check.
                    #
                    # It also closes a thundering herd that needs no outage at all:
                    # one verifier serves the whole process on one loop, so N
                    # concurrent requests all see `_age >= TTL` before any of them
                    # stamps. Measured: 20 concurrent verifies at expiry -> 20 GETs
                    # before, 1 after. The unknown-kid path already did this right,
                    # which is what made the asymmetry visible -- `_refreshed_at` is
                    # stamped BEFORE its await and `_fetched_at` was stamped after.
                    #
                    # The cost is that a failed refresh extends the cached set's trust
                    # by another TTL -- AND RENEWS THAT EXTENSION ON EVERY ATTEMPT, so
                    # holding the endpoint down holds the extension open indefinitely,
                    # not once (#50 r2). The stamp above is outside the `try`, which is
                    # what makes the fix work and also what makes the extension renew.
                    #
                    # That is the standard serve-stale trade and the right one: a
                    # revocation you cannot fetch is undeliverable anyway. The accepted
                    # limit is that someone who can hold the JWKS endpoint unreachable
                    # can hold a revoked key alive, and the alternative hands them a
                    # total outage instead.
                    #
                    # An earlier version of this paragraph added "not invisibly", which
                    # was a property this module did not have: the except swallowed the
                    # failure with no log, no counter and no readable state, so from
                    # the agent's side auth simply kept succeeding on a stale set. That
                    # clause is the one that made the trade sound like housekeeping.
                    # It is true now because of the warning below, not because it was
                    # asserted.
                    self._fetched_at = time.monotonic()
                    try:
                        self._key_set = KeySet.import_key_set(await self._fetch())
                        self._loaded_at = self._fetched_at
                    except Exception as exc:
                        if self._key_set is None:
                            # Cold start: there is nothing to serve stale.
                            self._cold_failure = f"could not load JWKS from {self.jwks_url}: {exc}"
                            raise AuthError(self._cold_failure) from exc
                        # CHOOSING TO KEEP SERVING A STALE SET IS EXACTLY THE DECISION
                        # THAT WANTS A RECORD. Without this the agent shows green auth
                        # throughout an outage it cannot see, and the comment above
                        # claimed a visibility the code did not provide.
                        log.warning(
                            "JWKS refresh from %s failed (%s); still serving the cached "
                            "key set, now %.0fs old. Revocations cannot reach this "
                            "verifier until the endpoint recovers.",
                            self.jwks_url,
                            exc,
                            time.monotonic() - self._loaded_at,
                        )
                finally:
                    # AFTER the fetch, so a caller that arrived while it was in
                    # flight sees the count move and shares its result.
                    self._fetch_count += 1
        return self._key_set

    async def verify_header(self, authorization: str | None) -> dict:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError("missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        if not token:
            raise AuthError("empty bearer token")
        return await self.verify(token)

    async def verify(self, token: str) -> dict:
        kid = _header_kid(token)
        if kid is None:
            raise AuthError("malformed token (no kid)")
        keys = await self._keys()
        if not any(key.kid == kid for key in keys.keys):
            if self._since_refresh < REFRESH_COOLDOWN_SECONDS:
                # Refused WITHOUT a fetch. We looked recently; an unknown kid
                # is not evidence enough to look again, and re-asking on every
                # such request is the amplification.
                raise AuthError(f"unknown signing key {kid!r}")
            self._refreshed_at = time.monotonic()
            keys = await self._keys(refresh=True)  # key rotation
            if not any(key.kid == kid for key in keys.keys):
                raise AuthError(f"unknown signing key {kid!r}")
        try:
            decoded = jwt.decode(token, keys, algorithms=["RS256"])
        except Exception as exc:
            raise AuthError(f"signature verification failed: {exc}") from exc

        claims = decoded.claims
        now = int(time.time())
        if claims.get("iss") != self.issuer:
            raise AuthError(f"unexpected iss: {claims.get('iss')!r}")
        if claims.get("aud") != self.audience:
            raise AuthError(
                f"audience mismatch: token aud={claims.get('aud')!r}, "
                f"expected {self.audience!r}"
            )
        exp = claims.get("exp")
        if exp is None or now > int(exp) + LEEWAY_SECONDS:
            raise AuthError("token expired")
        iat = claims.get("iat")
        if iat is None or int(iat) > now + LEEWAY_SECONDS:
            raise AuthError("token issued in the future")
        return dict(claims)
