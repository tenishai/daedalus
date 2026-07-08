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

Verification mirrors cabal's own (core/auth/jwt.py): signature against the
JWKS, iss + aud pinned, exp/iat with 30s leeway — plus an explicit RS256
algorithm allowlist at decode. Requires joserfc + httpx (the a2a extra).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Awaitable, Callable

from joserfc import jwt
from joserfc.jwk import KeySet

LEEWAY_SECONDS = 30


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
    """Verifies bearer JWTs against a JWKS endpoint, with rotation-aware
    refetch: an unknown kid triggers exactly one refresh, a garbage token
    triggers none (no fetch amplification from unauthenticated callers)."""

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

    async def _http_fetch(self) -> dict:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(self.jwks_url)
            response.raise_for_status()
            return response.json()

    async def _keys(self, *, refresh: bool = False) -> KeySet:
        if self._key_set is None or refresh:
            try:
                self._key_set = KeySet.import_key_set(await self._fetch())
            except Exception as exc:
                raise AuthError(f"could not load JWKS from {self.jwks_url}: {exc}") from exc
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
