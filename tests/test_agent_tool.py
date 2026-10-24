"""RED-first tests for Task 5 (arc3d): the fast tooling lane's runtime half.

TOOL_MARKER's real handler replaces the refusing stub landed alongside
KNOWLEDGE_MARKER: a tool file relayed over the system-task rail is
re-validated by THIS runtime (the fast lane skips CI by definition, so kobo's
own validation can never be the only gate) and, only once it passes, written
into a persistent overlay directory beside the techne db on the agent's own
volume (`spec.memory.home`) -- never `bundle.path / "tools"`, the image
bundle, which resets to its baked-in contents on every restart.

Deliberately independent of techne: a tool write is a filesystem operation
keyed off `memory.home`, not `memory.personal`, so these tests build bundles
with a bare `memory: home: <tmp>` and never need techne installed -- unlike
test_agent_teach.py's techne-gated fixtures.
"""

from __future__ import annotations

from pathlib import Path

from daedalus.agent import Agent
from daedalus.memory.teach import FILENAME_KEY, SYSTEM_TASK_KEY, TOOL_MARKER
from daedalus.task import VERIFIED_SUB_KEY, ReasonCode, Task, TaskState

VALID_TOOL = "def post_draft(draft):\n    return draft\n"


class _NoCallLLM:
    """Fails the test loudly if the model loop is ever reached."""

    async def complete(self, messages, tools=None):
        raise AssertionError("model must not be called")


def _bundle(tmp_path: Path, memory_yaml: str = "") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "agent.yaml").write_text(f"name: x\nmodel:\n  id: m\n{memory_yaml}")
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    return tmp_path


def _tool_agent(
    tmp_path: Path, home: Path, *, image_tools: dict[str, str] | None = None, **overrides
) -> Agent:
    root = _bundle(tmp_path, f"memory:\n  home: {home}\n")
    if image_tools:
        tools_dir = root / "tools"
        tools_dir.mkdir(exist_ok=True)
        for name, body in image_tools.items():
            (tools_dir / name).write_text(body)
    overrides.setdefault("llm", _NoCallLLM())
    return Agent.load(root, **overrides)


def _tool_task(
    content=VALID_TOOL, *, sub="service:kobo", filename="orchestrator.py"
) -> Task:
    task = Task.from_text(content)
    task.metadata[SYSTEM_TASK_KEY] = TOOL_MARKER
    task.metadata[FILENAME_KEY] = filename
    if sub is not None:
        task.metadata[VERIFIED_SUB_KEY] = sub
    return task


# ---- property 1: the runtime re-validates before writing, even when the ----
# ---- room is bypassed (the security-critical test of this task) -----------


async def test_invalid_tool_is_refused_at_the_runtime_and_nothing_is_written(tmp_path):
    """The fast lane skips CI by definition -- a tool that fails the exact
    mechanical contract kobo applies (here: a non-stdlib import) must be
    refused by THIS runtime even though nothing upstream stopped it, and the
    overlay directory must stay empty."""
    home = tmp_path / "home"
    agent = _tool_agent(tmp_path / "bundle", home)

    result = await agent.handle_task(
        _tool_task("import httpx\n\n\ndef post_draft(draft):\n    return draft\n")
    )

    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.TOOL_BLOCKED
    assert "standard library" in result.agent_text()
    overlay = home / "tools-overlay"
    assert not overlay.exists() or list(overlay.iterdir()) == []


async def test_tool_missing_entrypoint_is_refused_at_the_runtime(tmp_path):
    home = tmp_path / "home"
    agent = _tool_agent(tmp_path / "bundle", home)

    result = await agent.handle_task(_tool_task("def helper():\n    return 1\n"))

    assert result.state is TaskState.FAILED
    assert "pre_task" in result.agent_text() or "post_draft" in result.agent_text()
    overlay = home / "tools-overlay"
    assert not overlay.exists() or list(overlay.iterdir()) == []


async def test_tool_bad_filename_is_refused_at_the_runtime(tmp_path):
    home = tmp_path / "home"
    agent = _tool_agent(tmp_path / "bundle", home)

    result = await agent.handle_task(_tool_task(filename="Not-Valid.py"))

    assert result.state is TaskState.FAILED
    assert "filename" in result.agent_text()


# ---- property 2: persistence -- overlay, never bundle, survives restart ----


async def test_valid_tool_lands_in_the_overlay_directory_not_the_image_bundle(tmp_path):
    home = tmp_path / "home"
    bundle_root = tmp_path / "bundle"
    agent = _tool_agent(bundle_root, home)

    result = await agent.handle_task(_tool_task())

    assert result.state is TaskState.COMPLETED
    assert "restart" in result.agent_text().lower()
    overlay_file = home / "tools-overlay" / "orchestrator.py"
    assert overlay_file.is_file()
    assert overlay_file.read_text() == VALID_TOOL
    # Never the image bundle: bundle.path/tools is the one directory the
    # persistence trap says a restart wipes clean.
    assert not (bundle_root / "tools" / "orchestrator.py").exists()


async def test_overlay_tool_survives_a_simulated_restart(tmp_path):
    """"Restart" here means: a brand new Agent (a fresh process, in the real
    world) is constructed against the SAME bundle path and home directory.
    Its boot-time Enforcement load must still see the file the first
    process's agent wrote."""
    home = tmp_path / "home"
    bundle_root = tmp_path / "bundle"
    first_agent = _tool_agent(bundle_root, home)
    await first_agent.handle_task(_tool_task())

    second_agent = _tool_agent(bundle_root, home)

    assert second_agent.enforcement.hook_files == ["orchestrator.py"]


async def test_tool_write_without_a_configured_home_is_refused_politely(tmp_path):
    # No memory.home key at all -- models a bundle with no persistent volume
    # configured (`_tool_overlay_dir` returns None the same way
    # `_memory_from_spec`'s own db_path guard does).
    root = _bundle(tmp_path / "bundle")
    agent = Agent.load(root, llm=_NoCallLLM())

    result = await agent.handle_task(_tool_task())

    assert result.state is TaskState.FAILED
    assert "persistent" in result.agent_text().lower()


# ---- property 3: no shadowing -- refused at write ---------------------------


async def test_tool_filename_colliding_with_an_image_tool_is_refused_at_write(tmp_path):
    home = tmp_path / "home"
    bundle_root = tmp_path / "bundle"
    agent = _tool_agent(
        bundle_root,
        home,
        image_tools={"guard.py": "def pre_task(message):\n    return {}\n"},
    )

    result = await agent.handle_task(_tool_task(filename="guard.py"))

    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.TOOL_BLOCKED
    assert "guard.py" in result.agent_text()
    overlay = home / "tools-overlay"
    assert not overlay.exists() or list(overlay.iterdir()) == []
    # The image copy itself is of course untouched.
    assert (bundle_root / "tools" / "guard.py").read_text() == (
        "def pre_task(message):\n    return {}\n"
    )


# ---- authorization: operator-only, same gate as teach/knowledge ------------


async def test_tool_refused_for_a_prefixed_agent_sub(tmp_path):
    home = tmp_path / "home"
    agent = _tool_agent(tmp_path / "bundle", home)

    result = await agent.handle_task(_tool_task(sub="agent:ward"))

    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.POLICY_VIOLATION
    overlay = home / "tools-overlay"
    assert not overlay.exists() or list(overlay.iterdir()) == []


async def test_tool_refused_for_a_PERSONA_sub_which_is_what_cabal_MINTS(tmp_path):
    """THE #43 REGRESSION, PINNED WHERE CI CAN SEE IT (#49 r3).

    Every other test of this fix lives behind a techne `importorskip`, so
    reverting the gate to refuse only `agent:` — the exact regression this
    PR exists to prevent — leaves CI green. (The gate is an allowlist since
    #195; a `persona:` sub matches none of its entries.) With techne supplied the same
    mutant fails tests in two gated modules, `test_agent_teach.py` and
    `test_memory_teach.py`, and CI executes neither.

    THE COUNT IS DELIBERATELY ABSENT. An earlier version said "fails four
    tests, and all four are in the one module that skips" — made false by an
    edit in this very commit, which added a `persona:` assertion to a second
    gated module and took it to five across two. A number in a docstring about
    test coverage is a measurement with no invalidation, and this branch has
    had one wrong three times.

    That is daedalus#48 as a cause, disclosed in this PR's body; the finding
    here is narrower and about this diff. `teach_authorized` needs nothing to
    test — a pure function over `task.metadata`, string comparisons only — so
    the security-critical assertion belongs in a module CI actually executes,
    and this one does.
    """
    home = tmp_path / "home"
    agent = _tool_agent(tmp_path / "bundle", home)

    result = await agent.handle_task(_tool_task(sub="persona:scout"))

    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.POLICY_VIOLATION
    overlay = home / "tools-overlay"
    assert not overlay.exists() or list(overlay.iterdir()) == []


async def test_tool_refused_for_a_sub_nobody_planned_for(tmp_path):
    """#195: the gate used to be a blocklist, so a verified sub outside
    `persona:`/`agent:`/`operator` could deliver code the next boot runs.
    These are the reviewer's two (`svc-account-42`, an operator of another
    shape) plus `kobo-room`, a sub kobo really mints (its gateway admin
    token) for a different purpose."""
    home = tmp_path / "home"
    agent = _tool_agent(tmp_path / "bundle", home)

    for sub in ("svc-account-42", "kobo-room", "service:liminal", "operatorx", "Operator:x"):
        result = await agent.handle_task(_tool_task(sub=sub))
        assert result.state is TaskState.FAILED, sub
        assert result.reason_code is ReasonCode.POLICY_VIOLATION, sub
    overlay = home / "tools-overlay"
    assert not overlay.exists() or list(overlay.iterdir()) == []


async def test_tool_admitted_for_kobo_and_a_human_operator_by_default(tmp_path):
    """The allowlist must not close kobo's own door: kobo relays as
    `service:kobo` (its deploy docs mint CABAL_OPERATOR_TOKEN with that sub,
    and cabal's re-mint keeps it), and a human operator is `operator:<id>`."""
    home = tmp_path / "home"
    agent = _tool_agent(tmp_path / "bundle", home)

    for sub, name in (("service:kobo", "from_kobo.py"), ("operator:richard", "from_op.py")):
        result = await agent.handle_task(_tool_task(sub=sub, filename=name))
        assert result.state is not TaskState.FAILED, sub
    assert sorted(p.name for p in (home / "tools-overlay").iterdir()) == [
        "from_kobo.py",
        "from_op.py",
    ]


def _a2a_bundle(tmp_path: Path, home: Path, operator_subs: str) -> Path:
    return _bundle(
        tmp_path,
        f"memory:\n  home: {home}\n"
        "transports:\n  - kind: a2a\n    auth:\n"
        "      jwks_url: http://cabal/.well-known/jwks.json\n"
        "      issuer: cabal-core\n      audience: x\n"
        f"      operator_subs: {operator_subs}\n",
    )


async def test_bundle_operator_subs_replace_the_default(tmp_path):
    """A deployment whose kobo token has another sub names it; the list
    replaces the default, and a trailing `*` is a prefix match."""
    home = tmp_path / "home"
    root = _a2a_bundle(tmp_path / "bundle", home, '["service:room-7", "ops:*"]')
    agent = Agent.load(root, llm=_NoCallLLM())
    assert agent.operator_subs == ("service:room-7", "ops:*")

    for sub, admitted in (
        ("service:room-7", True),
        ("ops:alice", True),
        ("service:kobo", False),
        ("operator:richard", False),
        ("service:room-77", False),
    ):
        result = await agent.handle_task(_tool_task(sub=sub, filename="t.py"))
        assert (result.state is not TaskState.FAILED) is admitted, sub


def test_bundle_operator_subs_refuse_entries_that_reopen_the_gate(tmp_path):
    import pytest

    from daedalus.bundle import BundleError

    for bad in ('["*"]', '["operator"]', "[]", '"service:kobo"', '[""]'):
        root = _a2a_bundle(tmp_path / f"b{abs(hash(bad))}", tmp_path / "home", bad)
        with pytest.raises(BundleError, match="operator_subs"):
            Agent.load(root, llm=_NoCallLLM())


async def test_tool_refused_without_verified_sub(tmp_path):
    home = tmp_path / "home"
    agent = _tool_agent(tmp_path / "bundle", home)

    result = await agent.handle_task(_tool_task(sub=None))

    assert result.state is TaskState.FAILED
    assert result.reason_code is ReasonCode.POLICY_VIOLATION


# ---- never reaches the model or enforcement ---------------------------------


class _BlockingEnforcement:
    locked = True
    active = True
    load_error = ("blocked.py", "boom")

    async def run_pre_task(self, message):
        raise AssertionError("enforcement must not run for tool tasks")

    async def run_post_draft(self, draft):
        raise AssertionError("enforcement must not run for tool tasks")


async def test_tool_task_never_reaches_model_or_enforcement(tmp_path):
    home = tmp_path / "home"
    agent = _tool_agent(tmp_path / "bundle", home)
    agent.enforcement = _BlockingEnforcement()

    result = await agent.handle_task(_tool_task())

    assert result.state is TaskState.COMPLETED
