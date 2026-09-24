"""#72: the tests must import the checkout they run in.

`import daedalus` resolves through whatever is installed, and the dev venv has
daedalus installed editable against the canonical checkout. Without
`pythonpath = ["src"]`, pytest run from a git worktree tests the canonical
checkout, not the branch, and still reports green. Measured 2026-09-23:
`daedalus.__file__` resolved to the canonical checkout from inside a worktree.
Same defect as tenishai/liminal#85 and tenishai/cabal#211.

Two guards, covering different things:

  test_pyproject_still_sets_pythonpath  a config assertion; catches the setting
      being removed, from anywhere.
  test_daedalus_imports_from_this_checkout  the property itself. It cannot fail
      in the canonical checkout, where the editable install points at the same
      tree; it bites only in a worktree, which is where the bug happens.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import daedalus

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_still_sets_pythonpath() -> None:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    pythonpath = cfg["tool"]["pytest"]["ini_options"].get("pythonpath")
    assert pythonpath == ["src"], (
        f"[tool.pytest.ini_options] pythonpath is {pythonpath!r}, expected "
        '["src"]; without it a worktree run imports another checkout (#72).'
    )


def test_daedalus_imports_from_this_checkout() -> None:
    resolved = Path(daedalus.__file__).resolve()
    assert resolved.is_relative_to(ROOT), (
        f"tests import daedalus from {resolved}, outside this checkout ({ROOT}); "
        "an editable install is shadowing the tree under test (#72)."
    )
