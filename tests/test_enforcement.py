"""The law layer: loads bundle/tools/*.py hooks and composes them fail-closed.

House rule (same lineage as test_agent_tools.py): a bad hook, a slow hook, or
a hook that will not even import must never let a reply through unlawfully.
Real files under tmp_path exercise the real importlib.util path, not a mock.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from daedalus import enforcement
from daedalus.enforcement import Enforcement, PostDraftResult, PreTaskResult
from daedalus.task import ReasonCode


def _write_tool(tmp_path, name, body):
    d = tmp_path / "tools"
    d.mkdir(exist_ok=True)
    (d / name).write_text(body)
    return d


async def test_missing_dir_inactive(tmp_path):
    law = Enforcement.load(tmp_path / "tools")  # never created
    assert law.active is False
    assert law.locked is False
    assert law.hook_files == []
    assert law.card_stamp() == {"version": 1, "hooks": [], "locked": False}

    pre = await law.run_pre_task("hello")
    assert pre == PreTaskResult(False, [], "")

    post = await law.run_post_draft("a reply")
    assert post == PostDraftResult(False, "a reply", "")


async def test_pre_task_notices_concat_in_filename_order(tmp_path):
    d = _write_tool(
        tmp_path,
        "a_first.py",
        "def pre_task(message):\n"
        "    return {'notices': ['a saw: ' + message]}\n",
    )
    _write_tool(
        tmp_path,
        "b_second.py",
        "def pre_task(message):\n"
        "    return {'notices': ['b saw: ' + message]}\n",
    )
    law = Enforcement.load(d)
    assert law.hook_files == ["a_first.py", "b_second.py"]

    result = await law.run_pre_task("hi")
    assert result.blocked is False
    assert result.notices == ["a saw: hi", "b saw: hi"]


async def test_post_draft_chains_in_filename_order(tmp_path):
    d = _write_tool(
        tmp_path,
        "a_upper.py",
        "def post_draft(draft):\n"
        "    return draft.upper()\n",
    )
    _write_tool(
        tmp_path,
        "b_suffix.py",
        "def post_draft(draft):\n"
        "    return draft + 'z'\n",
    )
    law = Enforcement.load(d)

    result = await law.run_post_draft("hello")
    assert result.blocked is False
    assert result.text == "HELLOz"  # a_upper runs first, then b_suffix appends


async def test_hook_exception_blocks_with_filename_detail(tmp_path):
    d = _write_tool(
        tmp_path,
        "z_boom.py",
        "def pre_task(message):\n"
        "    raise ValueError('boom')\n",
    )
    law = Enforcement.load(d)

    result = await law.run_pre_task("hi")
    assert result.blocked is True
    assert "z_boom.py" in result.detail


async def test_hook_timeout_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(enforcement, "HOOK_TIMEOUT_S", 0.2)
    d = _write_tool(
        tmp_path,
        "slow.py",
        "import time\n"
        "def pre_task(message):\n"
        "    time.sleep(3)\n"
        "    return {}\n",
    )
    law = Enforcement.load(d)

    result = await law.run_pre_task("hi")
    assert result.blocked is True
    assert "slow.py" in result.detail


async def test_import_error_locks_not_raises(tmp_path):
    d = _write_tool(
        tmp_path,
        "broken.py",
        "import definitely_not_a_module\n",
    )
    law = Enforcement.load(d)

    assert law.active is True
    assert law.locked is True
    assert law.load_error[0] == "broken.py"
    assert law.hook_files == []


async def test_module_missing_both_hooks_is_load_shape_error(tmp_path):
    d = _write_tool(tmp_path, "neither.py", "x = 1\n")
    law = Enforcement.load(d)

    assert law.locked is True
    assert law.load_error == ("neither.py", "defines neither pre_task nor post_draft")


async def test_card_stamp_shapes(tmp_path):
    normal_root = tmp_path / "normal_case"
    normal_root.mkdir()
    d = _write_tool(
        normal_root,
        "a_notice.py",
        "def pre_task(message):\n"
        "    return {'notices': []}\n",
    )
    normal = Enforcement.load(d)
    assert normal.card_stamp() == {
        "version": 1,
        "hooks": ["a_notice.py"],
        "locked": False,
    }

    locked_root = tmp_path / "locked_case"
    locked_root.mkdir()
    d2 = _write_tool(locked_root, "neither.py", "x = 1\n")
    locked = Enforcement.load(d2)
    assert locked.card_stamp() == {
        "version": 1,
        "hooks": [],
        "locked": True,
        "locked_file": "neither.py",
    }


# ---- overlay directory (arc3d Task 5): image tools first, then overlay, ---
# ---- boot-only, no shadowing -----------------------------------------------


async def test_overlay_tools_load_after_image_tools_in_filename_order(tmp_path):
    image = _write_tool(tmp_path, "a_image.py", "def pre_task(message):\n    return {}\n")
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "b_overlay.py").write_text("def pre_task(message):\n    return {}\n")

    law = Enforcement.load(image, overlay)

    assert law.locked is False
    assert law.hook_files == ["a_image.py", "b_overlay.py"]


async def test_overlay_only_no_image_tools_dir_at_all(tmp_path):
    image = tmp_path / "tools"  # never created
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "solo.py").write_text("def pre_task(message):\n    return {}\n")

    law = Enforcement.load(image, overlay)

    assert law.locked is False
    assert law.hook_files == ["solo.py"]


async def test_overlay_tool_survives_being_loaded_again_after_a_simulated_restart(tmp_path):
    """"Restart" here is exactly what it is at boot in the real runtime:
    Enforcement.load called again from scratch against the same two
    directories. The overlay file the first load saw must still be there
    for the second -- load itself never mutates or clears anything."""
    image = tmp_path / "tools"  # never created: no image tools at all
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "sticky.py").write_text("def pre_task(message):\n    return {}\n")

    first = Enforcement.load(image, overlay)
    second = Enforcement.load(image, overlay)

    assert first.hook_files == ["sticky.py"]
    assert second.hook_files == ["sticky.py"]


async def test_overlay_tool_shadowing_image_tool_is_skipped_at_load_with_loud_log(
    tmp_path, caplog
):
    """A file planted directly on the overlay directory's disk (bypassing
    kobo and the runtime's own apply_tool write-time check entirely) must
    still never shadow the baseline image-bundle tool of the same name: the
    image copy is the one that loads and the one that runs, and the skip is
    logged loudly enough to notice."""
    image = _write_tool(
        tmp_path,
        "guard.py",
        "def pre_task(message):\n    return {'notices': ['image guard ran']}\n",
    )
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "guard.py").write_text(
        "def pre_task(message):\n    return {'notices': ['overlay guard ran']}\n"
    )

    with caplog.at_level("WARNING"):
        law = Enforcement.load(image, overlay)

    assert law.locked is False
    assert law.hook_files == ["guard.py"]
    result = await law.run_pre_task("hi")
    assert result.notices == ["image guard ran"]
    assert "guard.py" in caplog.text
    assert "shadow" in caplog.text.lower()


async def test_overlay_broken_hook_locks_same_as_an_image_one(tmp_path):
    image = _write_tool(tmp_path, "a_image.py", "def pre_task(message):\n    return {}\n")
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "broken.py").write_text("import definitely_not_a_module\n")

    law = Enforcement.load(image, overlay)

    assert law.locked is True
    assert law.load_error[0] == "broken.py"
    assert law.hook_files == []


def test_no_overlay_dir_is_byte_identical_to_image_only_load(tmp_path):
    image = _write_tool(tmp_path, "a_image.py", "def pre_task(message):\n    return {}\n")

    without_overlay = Enforcement.load(image)
    with_missing_overlay = Enforcement.load(image, tmp_path / "overlay-never-created")

    assert without_overlay.hook_files == with_missing_overlay.hook_files == ["a_image.py"]


def test_reason_code_tool_blocked_is_703():
    # The brief specified 702, but task.py already carries BUDGET_EXHAUSTED = 702
    # (cabal's canonical taxonomy at cabal/src/cabal/core/a2a/codes.py explicitly
    # reserves 703 as the next open, non-contiguous slot). Using 703 here to
    # avoid silently aliasing TOOL_BLOCKED onto BUDGET_EXHAUSTED. See report.
    assert ReasonCode.TOOL_BLOCKED == 703


async def test_a_hook_can_DENY_by_returning_blocked(tmp_path):
    """#45: this return used to be discarded in silence.

    `run_pre_task` read exactly one key -- `notices` -- and dropped the rest
    without a word. A hook author writing the obvious thing got no error, no
    warning, and no block: the task proceeded. In the module whose first line
    calls itself the law layer, a returned denial that does nothing is the
    worst available outcome, because the author believes they have denied.
    """
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "deny.py").write_text(
        "def pre_task(message):\n"
        "    if 'wire the money' in message:\n"
        "        return {'blocked': True, 'reason': 'financial action'}\n"
        "    return {}\n"
    )
    law = Enforcement.load(tools)

    blocked = await law.run_pre_task("please wire the money to 123")
    assert blocked.blocked is True
    assert blocked.detail == "blocked by deny.py: financial action"

    allowed = await law.run_pre_task("what time is standup?")
    assert allowed.blocked is False


async def test_a_denial_without_a_reason_still_denies_and_says_so(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "terse.py").write_text("def pre_task(message):\n    return {'blocked': True}\n")

    result = await Enforcement.load(tools).run_pre_task("hi")

    assert result.blocked is True
    assert result.detail == "blocked by terse.py: no reason given"


async def test_notices_from_an_earlier_hook_survive_a_later_denial(tmp_path):
    """A denial ends the chain, but what ran before it still happened and the
    caller is still entitled to hear about it."""
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "a_notice.py").write_text(
        "def pre_task(message):\n    return {'notices': ['scanned']}\n"
    )
    (tools / "b_deny.py").write_text(
        "def pre_task(message):\n    return {'blocked': True, 'reason': 'nope'}\n"
    )

    result = await Enforcement.load(tools).run_pre_task("hi")

    assert result.blocked is True
    assert result.notices == ["scanned"]


async def test_an_unrecognised_key_is_named_in_the_log_and_does_not_block(tmp_path, caplog):
    """Logged, not obeyed, and not refused either — #51 carries the argument
    for refusing. Silence is how `blocked` went unnoticed for as long as it
    did, so the key is at least named."""
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "future.py").write_text(
        "def pre_task(message):\n    return {'notices': ['ok'], 'blocked_maybe': True}\n"
    )

    with caplog.at_level(logging.WARNING):
        result = await Enforcement.load(tools).run_pre_task("hi")

    assert result.blocked is False
    assert result.notices == ["ok"]
    assert "blocked_maybe" in caplog.text
    assert "future.py" in caplog.text


async def test_hooks_run_on_their_OWN_pool_not_the_shared_default_executor(tmp_path):
    """#45: `asyncio.to_thread` uses the shared default executor.

    `wait_for` cancels the await and cannot cancel the thread, so every
    timed-out hook leaks a worker. On the shared pool, enough of them make
    every `to_thread` call in the process queue behind hooks that will never
    finish -- including callers with nothing to do with enforcement. Failing
    closed is right; converting one slow hook into a whole-agent outage
    through an unrelated shared resource is not.

    ASSERTED BY THREAD NAME, NOT BY STARVATION. The obvious test -- wedge
    several hooks, then show an ordinary `to_thread` still answers -- PASSES
    AGAINST THE BUG, because the default executor is min(32, cpu+4) wide and
    a handful of wedged hooks never exhausts it. Measured: that version went
    green with the fix reverted. Exhausting 32 workers honestly would cost
    ~66s of sleeping. The property is "hooks do not touch the shared pool",
    and the thread they run on states it directly."""
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "named.py").write_text(
        "import threading\n"
        "def pre_task(message):\n"
        "    return {'notices': [threading.current_thread().name]}\n"
    )

    result = await Enforcement.load(tools).run_pre_task("hi")

    assert result.notices and result.notices[0].startswith("kobo-hook"), (
        f"hook ran on {result.notices[0]!r} — not the dedicated pool"
    )


async def test_a_wedged_hook_fails_closed_every_time_and_does_not_leak_into_the_suite(
    tmp_path, monkeypatch
):
    """A hook that hangs blocks, repeatedly, and its threads are contained.

    ISOLATED POOL ON PURPOSE. The dedicated pool is a module global, and
    wedging it here wedges it for every LATER test in the session -- which is
    exactly what happened: `test_transport_a2a.py` began failing in the full
    run while passing alone, because its hook queued behind threads this test
    had abandoned. That is a real property of the design, not only of the
    test: a bounded pool means enough wedged hooks degrade enforcement for
    everyone, fail-closed. Containing it to enforcement is the point; leaking
    it into an unrelated test file is not evidence of anything."""
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="t")
    monkeypatch.setattr(enforcement, "_HOOK_POOL", pool)
    tools = tmp_path / "tools"
    tools.mkdir()
    # RELEASED ON THE WAY OUT, NOT LEFT SLEEPING. An isolated pool fixes the
    # cross-test poisoning, and the first version of this test stopped there:
    # the pool was never shut down and its `time.sleep(30)` threads were never
    # released. They are non-daemon and registered in
    # `concurrent.futures.thread._threads_queues`, so `_python_exit` joins them
    # during interpreter finalization -- AFTER pytest prints its timing.
    # Measured (#52 r1): pytest reported 6.14s, process wall clock 32.44s, a
    # 26.30s gap; the control's gap was 0.32s, and dropping the sleep to 4s
    # moved the gap to 2.29s, so it is causal rather than correlated.
    #
    # Every full-suite run paid that silently, in a test whose NAME asserts
    # the containment it did not hold. That is the third instance in this file
    # of a test asserting a property it does not check -- which is the trap
    # that made the first two versions of it wrong.
    release = threading.Event()
    (tools / "wedged.py").write_text(
        "def pre_task(message):\n    RELEASE.wait(60)\n    return {}\n"
    )
    law = Enforcement.load(tools)
    law._modules[0][1].RELEASE = release

    try:
        for _ in range(3):
            result = await law.run_pre_task("hi")
            assert result.blocked is True
            assert "timed out" in result.detail
    finally:
        release.set()
        # `wait=True` IS LOAD-BEARING; do not "simplify" it to wait=False.
        # It is what drags a future regression back INSIDE the window pytest
        # measures. The original defect hid in interpreter finalization, after
        # the timing was printed; with this, removing `release.set()` makes the
        # suite take 62s reported rather than 62s invisible (#52 r2).
        pool.shutdown(wait=True)


async def test_a_SLOW_but_finite_hook_is_not_counted_as_a_wedge(
    tmp_path, monkeypatch, caplog
):
    """#52 r2: the counter measured timeouts and the message called them wedges.

    A timeout fires on ANY call over the bound, including one whose thread
    finishes a moment later and returns to the pool. Measured with a 0.4s hook
    against a 0.2s bound: five calls, ONE thread reused throughout, and the
    line read "12 of 4 hook workers now wedged" — a number past its own
    denominator, describing a fatal state, on a pool that then answered a
    healthy hook instantly.

    The failure mode was the false positive, which is the expensive direction
    for a diagnostic read during an incident. The message now says what it
    counts, and this pins that the pool is genuinely still alive afterwards.
    """
    monkeypatch.setattr(enforcement, "HOOK_TIMEOUT_S", 0.2)
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="t")
    monkeypatch.setattr(enforcement, "_HOOK_POOL", pool)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "slow.py").write_text(
        "import time\ndef pre_task(message):\n    time.sleep(0.4)\n    return {}\n"
    )
    law = Enforcement.load(tools)

    before = enforcement._TIMEOUTS
    try:
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                assert (await law.run_pre_task("hi")).blocked is True

        # It IS five timeouts — that is what the counter now claims to count,
        # and asserting the number is what keeps the rename honest rather than
        # cosmetic.
        assert enforcement._TIMEOUTS - before == 5

        assert "wedged" not in caplog.text, (
            "a slow-but-finite hook was reported as a wedge"
        )
        assert "timeouts since boot" in caplog.text

        # THE POINT: the pool is not exhausted, because nothing was consumed.
        healthy = tmp_path / "healthy"
        healthy.mkdir()
        (healthy / "fast.py").write_text(
            "def pre_task(message):\n    return {'notices': ['ok']}\n"
        )
        result = await Enforcement.load(healthy).run_pre_task("hi")
        assert result.blocked is False and result.notices == ["ok"]
    finally:
        pool.shutdown(wait=True)
