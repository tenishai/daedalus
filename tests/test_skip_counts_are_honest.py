"""The skip reasons name how many tests they hide, so the numbers must be true.

Three test files begin with a module-level `pytest.importorskip("techne")`.
When techne is absent — which is every CI run today, because the workflow
installs `.[dev]` and techne is in the `techne` extra — pytest collapses each
of those files into ONE "s". Three characters standing in for 89 tests.

daedalus#48 is about that gap. Until techne can be installed in CI, the reason
string is the only place the real number appears, and a hand-written number in
a string is exactly the kind of claim that rots the moment someone adds a test.

So it is checked here instead of trusted. This file imports nothing from
techne and always runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent

GUARDED_FILES = [
    "test_agent_teach.py",
    "test_memory_teach.py",
    "test_memory_revision.py",
]

_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test_", re.M)
_SKIP_REASON = re.compile(
    r'reason="techne is not installed — (\d+) tests in this file did not run"'
)


@pytest.mark.parametrize("filename", GUARDED_FILES)
def test_skip_reason_names_the_real_test_count(filename: str) -> None:
    source = (TESTS_DIR / filename).read_text()

    actual = len(_TEST_DEF.findall(source))
    match = _SKIP_REASON.search(source)

    assert match, (
        f"{filename} has no importorskip reason naming a test count. "
        "If the module-level skip was removed, delete this file's entry from "
        "GUARDED_FILES; if it was reworded, keep the count in it."
    )

    claimed = int(match.group(1))
    assert claimed == actual, (
        f"{filename} says {claimed} tests are hidden by its techne skip, "
        f"but it defines {actual}. Update the reason string."
    )


def test_every_guarded_file_is_still_guarded() -> None:
    """A file that stops skipping should leave this list, not sit here passing."""
    for filename in GUARDED_FILES:
        source = (TESTS_DIR / filename).read_text()
        assert 'importorskip("techne"' in source or "importorskip(\n" in source, (
            f"{filename} no longer has a module-level techne importorskip. "
            "Remove it from GUARDED_FILES — a guard over nothing passes forever."
        )
