"""Enforcement hooks: the law layer around every reply.

Loads bundle/tools/*.py (kobo's staged Tooling door) and runs their
pre_task/post_draft hooks around the agent loop. Distinct from
daedalus.tools (LLM function-calling providers). Fail-closed everywhere:
an exception, a timeout, or a load failure never lets an unlawful reply
through. When locked, blocking every task is the Agent layer's responsibility;
this module only reports its locked state.

A pre_task hook denies in either of two ways: by RAISING, or by returning
`{"blocked": True, "reason": "..."}`. The second used to be read as an
ordinary result and discarded (#45), so raising was the only mechanism and
nothing said so -- a hook author who returned a denial was not denying and
was not told. Unrecognised keys are logged and ignored; #51 argues for
refusing them instead.

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOOK_TIMEOUT_S = 2.0

#: Keys a `pre_task` hook may return. Anything else is logged, not obeyed.
#:
#: `blocked` USED TO BE SILENTLY DISCARDED (#45). This function read exactly
#: one key -- `notices` -- and dropped the rest without a word, so a hook
#: author writing the obvious `{"blocked": True, "reason": ...}` got no error,
#: no warning, and no block: the task proceeded. In a module whose first line
#: calls itself the law layer, a returned denial that does nothing is the
#: worst available outcome, because the author believes they have denied.
_PRE_TASK_KEYS = frozenset({"notices", "blocked", "reason"})

#: Hooks run on their own pool so a wedged one cannot starve the process.
#:
#: `asyncio.to_thread` USES THE SHARED DEFAULT EXECUTOR, bounded at
#: min(32, cpu+4). `wait_for` cancels the AWAIT and cannot cancel the thread,
#: so a hook that hangs leaks one worker per task. Leak enough and every
#: `to_thread` call in the process queues behind them -- including callers
#: with nothing to do with enforcement -- and the agent stops. Failing closed
#: the whole way down is correct, but one slow hook should not be able to
#: convert itself into a whole-agent outage through an unrelated shared
#: resource.
#:
#: FOUR IS A LIFETIME BUDGET, NOT A CONCURRENCY ONE, and the difference is the
#: steady state this pool actually produces (#52 r1). A wedged thread never
#: returns, so the count only ratchets: four hooks that have EVER wedged over
#: the life of the process, not four at once. At the fourth, the agent stops
#: answering -- on purpose, fail-closed -- and STAYS STOPPED until restart.
#: That is the intended outcome and it is better than what it replaced, where
#: a 32-wide per-loop pool both took longer to exhaust and reset itself, which
#: is what made it a slow silent poisoning of unrelated callers rather than a
#: visible stop. "Contained" is a thinner word for it than it deserves.
#:
#: Four occurrences is a generous allowance for a thing that should happen
#: zero times; a hook that wedges is already a bug.
HOOK_POOL_WORKERS = 4
_HOOK_POOL = ThreadPoolExecutor(
    max_workers=HOOK_POOL_WORKERS, thread_name_prefix="kobo-hook"
)

#: How many hook threads have wedged and never come back.
#:
#: RATCHETS, NEVER DECREMENTS, because neither does the thing it counts. This
#: exists so "is four the right number" is answerable from real traffic rather
#: than from argument (#52 r1) -- the same move as the unknown-key log, and
#: the reason that decision is defensible rather than merely convenient.
_WEDGED = 0
PreTaskResult = namedtuple("PreTaskResult", "blocked notices detail")
PostDraftResult = namedtuple("PostDraftResult", "blocked text detail")

log = logging.getLogger(__name__)


def _wedged() -> int:
    """Record a hook thread that timed out and will never be returned.

    A COUNT, NOT THE POOL'S WIDTH. The log used to report
    `_HOOK_POOL._max_workers` -- a private attribute, and a constant: it said
    "the pool has 4 workers" identically on the first wedge and on the fatal
    fourth. At the moment a reader needs that line, the number they want is
    how many are already gone (#52 r1 nit)."""
    global _WEDGED
    _WEDGED += 1
    return _WEDGED


async def _call_hook(hook, argument):
    """Run `hook(argument)` on the hook pool under HOOK_TIMEOUT_S.

    The thread is NOT cancelled on timeout -- nothing can cancel a running
    thread in Python -- so the hook body runs to completion on a pool that
    exists to contain exactly that."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_HOOK_POOL, hook, argument), HOOK_TIMEOUT_S
    )


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
                result = await _call_hook(hook, message)
            except TimeoutError:
                _wedged()
                log.warning(
                    "pre_task hook %s exceeded %.1fs and was abandoned; its thread "
                    "runs to completion (%d of %d hook workers now wedged)",
                    filename,
                    HOOK_TIMEOUT_S,
                    _WEDGED,
                    HOOK_POOL_WORKERS,
                )
                return PreTaskResult(True, notices, f"blocked by {filename}: timed out")
            except Exception as exc:  # noqa: BLE001  fail closed on any hook error
                return PreTaskResult(True, notices, f"blocked by {filename}: {_first_line(exc)}")
            if not isinstance(result, dict):
                return PreTaskResult(
                    True, notices, f"blocked by {filename}: returned an unexpected shape"
                )
            # LOGGED, NOT OBEYED, AND NOT REFUSED EITHER (#45, and #51 carries
            # the argument for refusing). A hook returning a key this module
            # does not know is telling us something, and silence is how
            # `blocked` went unnoticed in the first place. Blocking on it
            # instead would fail closed on a kobo tool that merely returned an
            # extra field, which is a fleet outage to fix a spelling mistake.
            unknown = set(result) - _PRE_TASK_KEYS
            if unknown:
                log.warning(
                    "pre_task hook %s returned unrecognised key(s) %s; ignored. "
                    "A pre_task hook may return notices, blocked and reason.",
                    filename,
                    ", ".join(sorted(unknown)),
                )
            notices.extend(result.get("notices", []))
            if result.get("blocked"):
                reason = result.get("reason") or "no reason given"
                return PreTaskResult(True, notices, f"blocked by {filename}: {reason}")
        return PreTaskResult(False, notices, "")

    async def run_post_draft(self, draft: str) -> PostDraftResult:
        text = draft
        for filename, mod in self._modules:
            hook = getattr(mod, "post_draft", None)
            if not callable(hook):
                continue
            try:
                result = await _call_hook(hook, text)
            except TimeoutError:
                _wedged()
                log.warning(
                    "post_draft hook %s exceeded %.1fs and was abandoned; its thread "
                    "runs to completion (%d of %d hook workers now wedged)",
                    filename,
                    HOOK_TIMEOUT_S,
                    _WEDGED,
                    HOOK_POOL_WORKERS,
                )
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
