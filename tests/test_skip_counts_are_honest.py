"""The skip reasons name how many tests they hide, so the numbers must be true.

Three test files begin with a module-level `pytest.importorskip("techne")`.
When techne is absent — which is every CI run today, because the workflow
installs `.[dev]` and techne is in the `techne` extra — pytest collapses each
of those files into ONE "s". Three characters standing in for 92 tests.

daedalus#48 is about that gap. Until techne can be installed in CI, the reason
string is the only place the real number appears, and a hand-written number in
a string is exactly the kind of claim that rots the moment someone adds a test.

SO IT IS CHECKED, AND IT IS CHECKED THE WAY PYTEST COUNTS. The first version of
this file counted `def test_` occurrences, which is not what pytest collects: a
parametrised test is one `def` and several items. That undercounted by 3 and —
worse — the guard used the same method as the strings it guarded, so the two
agreed with each other while both were wrong. Two numbers computed the same
incomplete way do not check anything.

Counting below expands `@pytest.mark.parametrize` through the AST. Where a
decorator's values are not a literal sequence the count cannot be known
statically, and this file says so loudly rather than guessing low.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent

GUARDED_FILES = [
    "test_agent_teach.py",
    "test_memory_teach.py",
    "test_memory_revision.py",
]

_SKIP_REASON = re.compile(
    r'reason="techne is not installed — (\d+) tests in this file did not run"'
)


def _parametrize_factor(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """How many items pytest collects for one test function.

    Each `@pytest.mark.parametrize` decorator multiplies. Stacked decorators
    multiply together, which is pytest's own behaviour.
    """
    factor = 1
    for dec in node.decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        if call is None:
            continue
        name = call.func
        parts = []
        while isinstance(name, ast.Attribute):
            parts.append(name.attr)
            name = name.value
        if isinstance(name, ast.Name):
            parts.append(name.id)
        if "parametrize" not in parts:
            continue
        if len(call.args) < 2:
            continue
        values = call.args[1]
        if not isinstance(values, (ast.List, ast.Tuple)):
            raise AssertionError(
                f"{node.name}: parametrize values are not a literal sequence, so the "
                "collected count cannot be derived statically. Either inline the "
                "values or switch this guard to `pytest --collect-only`."
            )
        factor *= len(values.elts)
    return factor


def collected_items(source: str) -> int:
    tree = ast.parse(source)
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            total += _parametrize_factor(node)
    return total


@pytest.mark.parametrize("filename", GUARDED_FILES)
def test_skip_reason_names_the_real_test_count(filename: str) -> None:
    source = (TESTS_DIR / filename).read_text()

    actual = collected_items(source)
    match = _SKIP_REASON.search(source)

    assert match, (
        f"{filename} has no importorskip reason naming a test count. "
        "If the module-level skip was removed, delete this file's entry from "
        "GUARDED_FILES; if it was reworded, keep the count in it."
    )

    claimed = int(match.group(1))
    assert claimed == actual, (
        f"{filename} says {claimed} tests are hidden by its techne skip, but pytest "
        f"would collect {actual}. Update the reason string. (Remember a parametrised "
        "test is one def and several items — that is what the first version of this "
        "guard got wrong.)"
    )


def test_the_counting_expands_parametrize() -> None:
    """The guard's own method, pinned — this is what it got wrong before."""
    source = (
        "import pytest\n"
        "def test_plain(): ...\n"
        "@pytest.mark.parametrize('x', [1, 2, 3])\n"
        "def test_three(x): ...\n"
        "@pytest.mark.parametrize('y', [1, 2])\n"
        "@pytest.mark.parametrize('x', [1, 2, 3])\n"
        "def test_six(x, y): ...\n"
    )
    assert collected_items(source) == 1 + 3 + 6


def test_every_guarded_file_is_still_guarded() -> None:
    """A file that stops skipping should leave this list, not sit here passing."""
    for filename in GUARDED_FILES:
        source = (TESTS_DIR / filename).read_text()
        assert 'importorskip(' in source and '"techne"' in source, (
            f"{filename} no longer has a module-level techne importorskip. "
            "Remove it from GUARDED_FILES — a guard over nothing passes forever."
        )
