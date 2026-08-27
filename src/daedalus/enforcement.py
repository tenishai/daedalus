"""Enforcement hooks: the law layer around every reply.

Loads bundle/tools/*.py (kobo's staged Tooling door) and runs their
pre_task/post_draft hooks around the agent loop. Distinct from
daedalus.tools (LLM function-calling providers). Fail-closed everywhere:
an exception, a timeout, or a load failure never lets an unlawful reply
through. When locked, blocking every task is the Agent layer's responsibility;
this module only reports its locked state.

Boot-only loading, image tools first (arc3d Task 5): `load` also accepts an
`overlay_dir` -- the persistent, fast-lane-delivered tool directory on the
agent's own volume (daedalus.agent.Agent._tool_overlay_dir). Image tools
load first, then overlay tools, and this function is never re-entered on a
delivery: a fast-delivered tool is inert in the current process no matter
what, by construction, until the next boot re-runs `load`. An overlay
filename identical to an image-bundle tool's is a shadow attempt -- kobo's
own write-time check (and this runtime's own apply_tool) already refuse
that, but a file planted directly on the volume, bypassing both entirely,
is still caught here: skipped, never loaded, with a loud log line, rather
than silently replacing or running alongside the hook it collides with."""

import asyncio
import importlib.util
import logging
from collections import namedtuple
from pathlib import Path

HOOK_TIMEOUT_S = 2.0
PreTaskResult = namedtuple("PreTaskResult", "blocked notices detail")
PostDraftResult = namedtuple("PostDraftResult", "blocked text detail")

log = logging.getLogger(__name__)


def _first_line(exc: Exception) -> str:
    text = str(exc)
    first = text.splitlines()[0] if text else exc.__class__.__name__
    return first[:200]


def _load_hook_module(path: Path, prefix: str):
    """Import `path` as a hook module named `{prefix}_{path.stem}`; return
    it if it defines at least one of the two hooks, else raise ValueError.
    Any import exception propagates too -- the caller treats both the same
    way (a load failure)."""
    spec = importlib.util.spec_from_file_location(f"{prefix}_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "pre_task", None)) and not callable(
        getattr(mod, "post_draft", None)
    ):
        raise ValueError("defines neither pre_task nor post_draft")
    return mod


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
    def load(cls, tools_dir: Path, overlay_dir: Path | None = None) -> "Enforcement":
        """Load `tools_dir` (the image bundle's `bundle/tools/`) first, then
        `overlay_dir` (the persistent, fast-lane-delivered tools on the
        agent's own volume) when given. Any load failure, in either
        directory, locks the whole instance -- the law never half-loads --
        EXCEPT an overlay filename that collides with an already-loaded
        image tool, which is not a load failure at all: it is skipped
        outright, with a loud warning log naming the file, and loading
        continues with everything else."""
        modules: list[tuple[str, object]] = []
        if tools_dir.is_dir():
            for path in sorted(tools_dir.glob("*.py")):
                try:
                    mod = _load_hook_module(path, "kobo_tool")
                except Exception as exc:  # noqa: BLE001  law never half-loads
                    return cls([], load_error=(path.name, _first_line(exc)))
                modules.append((path.name, mod))

        if overlay_dir is not None and overlay_dir.is_dir():
            image_names = {name for name, _mod in modules}
            for path in sorted(overlay_dir.glob("*.py")):
                if path.name in image_names:
                    log.warning(
                        "overlay tool %s shadows an image-bundle tool of the "
                        "same name; skipped at load",
                        path.name,
                    )
                    continue
                try:
                    mod = _load_hook_module(path, "kobo_overlay_tool")
                except Exception as exc:  # noqa: BLE001  law never half-loads
                    return cls([], load_error=(path.name, _first_line(exc)))
                modules.append((path.name, mod))

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
