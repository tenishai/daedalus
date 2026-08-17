"""Enforcement hooks: the law layer around every reply.

Loads bundle/tools/*.py (kobo's staged Tooling door) and runs their
pre_task/post_draft hooks around the agent loop. Distinct from
daedalus.tools (LLM function-calling providers). Fail-closed everywhere:
an exception, a timeout, or a load failure never lets an unlawful reply
through."""

import asyncio
import importlib.util
from collections import namedtuple
from pathlib import Path

HOOK_TIMEOUT_S = 2.0
PreTaskResult = namedtuple("PreTaskResult", "blocked notices detail")
PostDraftResult = namedtuple("PostDraftResult", "blocked text detail")


def _first_line(exc: Exception) -> str:
    text = str(exc)
    first = text.splitlines()[0] if text else exc.__class__.__name__
    return first[:200]


class Enforcement:
    """Loads bundle/tools/*.py hook modules and composes them fail-closed.

    A missing or empty tools directory is a no-op (active is False). Any
    import failure, or a module that defines neither hook, locks the whole
    instance rather than running a partial set of hooks: the law never
    half-loads.
    """

    def __init__(self, modules, load_error=None):
        self._modules = modules  # list[(filename, module)]
        self.load_error = load_error
        self.active = bool(modules) or load_error is not None

    @classmethod
    def load(cls, tools_dir: Path) -> "Enforcement":
        if not tools_dir.is_dir():
            return cls([])
        modules = []
        for path in sorted(tools_dir.glob("*.py")):
            try:
                spec = importlib.util.spec_from_file_location(f"kobo_tool_{path.stem}", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if not callable(getattr(mod, "pre_task", None)) and not callable(
                    getattr(mod, "post_draft", None)
                ):
                    return cls([], load_error=(path.name, "defines neither pre_task nor post_draft"))
                modules.append((path.name, mod))
            except Exception as exc:  # noqa: BLE001  law never half-loads
                return cls([], load_error=(path.name, _first_line(exc)))
        return cls(modules)

    @property
    def locked(self) -> bool:
        return self.load_error is not None

    @property
    def hook_files(self) -> list[str]:
        return sorted(name for name, _mod in self._modules)

    async def run_pre_task(self, message: str) -> PreTaskResult:
        notices: list[str] = []
        for filename, mod in self._modules:
            hook = getattr(mod, "pre_task", None)
            if not callable(hook):
                continue
            try:
                result = await asyncio.wait_for(asyncio.to_thread(hook, message), HOOK_TIMEOUT_S)
            except TimeoutError:
                return PreTaskResult(True, notices, f"blocked by {filename}: timed out")
            except Exception as exc:  # noqa: BLE001  fail closed on any hook error
                return PreTaskResult(True, notices, f"blocked by {filename}: {_first_line(exc)}")
            if not isinstance(result, dict):
                return PreTaskResult(
                    True, notices, f"blocked by {filename}: returned an unexpected shape"
                )
            notices.extend(result.get("notices", []))
        return PreTaskResult(False, notices, "")

    async def run_post_draft(self, draft: str) -> PostDraftResult:
        text = draft
        for filename, mod in self._modules:
            hook = getattr(mod, "post_draft", None)
            if not callable(hook):
                continue
            try:
                result = await asyncio.wait_for(asyncio.to_thread(hook, text), HOOK_TIMEOUT_S)
            except TimeoutError:
                return PostDraftResult(True, text, f"blocked by {filename}: timed out")
            except Exception as exc:  # noqa: BLE001  fail closed on any hook error
                return PostDraftResult(True, text, f"blocked by {filename}: {_first_line(exc)}")
            if not isinstance(result, str):
                return PostDraftResult(
                    True, text, f"blocked by {filename}: returned an unexpected shape"
                )
            text = result
        return PostDraftResult(False, text, "")

    def card_stamp(self) -> dict:
        stamp = {"version": 1, "hooks": self.hook_files, "locked": self.locked}
        if self.locked:
            stamp["locked_file"] = self.load_error[0]
        return stamp
