"""The law layer: loads bundle/tools/*.py hooks and composes them fail-closed.

House rule (same lineage as test_agent_tools.py): a bad hook, a slow hook, or
a hook that will not even import must never let a reply through unlawfully.
Real files under tmp_path exercise the real importlib.util path, not a mock.
"""

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
