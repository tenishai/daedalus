"""Session-wide guards.

DAEDALUS_REQUIRE_TECHNE=1 marks a CI leg as one that is supposed to have techne
installed (the `techne` job in .github/workflows/test.yml). On such a leg, the
three techne-path files' module-level `pytest.importorskip("techne", ...)`
must not be allowed to turn a broken or missing install into a quiet skip --
that quiet skip is daedalus#48: `pip install -e .[dev]` never pulls in techne,
so the gap read as "13 skipped" instead of a failure. Fail the session before
collection even starts, rather than let it report a green run that ran less
than it claims.

Local dev and the plain `test` job never set this, so `pytest.importorskip`
there behaves exactly as before -- techne is optional without it.

Lives at the repo root, not tests/, so the hook fires for testpaths' other
root (`docker/`) too, and for a bare `pytest docker/test_seeder.py` -- a
conftest.py inside tests/ is not an ancestor of docker/ and would not load.
"""

from __future__ import annotations

import os

import pytest


def pytest_sessionstart(session: pytest.Session) -> None:
    if os.environ.get("DAEDALUS_REQUIRE_TECHNE") != "1":
        return
    try:
        import techne.api  # noqa: F401  (0.19.1+, #228)
    except ImportError as exc:
        raise pytest.UsageError(
            "DAEDALUS_REQUIRE_TECHNE=1 but techne failed to import "
            f"({exc}) -- this CI leg is supposed to have it installed. "
            "A missing techne must fail the build, not skip the tests that need it."
        ) from exc
