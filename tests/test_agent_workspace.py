"""Per-task workspaces and metadata-bound tool arguments (#247).

The adversarial cases come first: a workspace name is input from the wire, so a name with
a slash, a `..`, a symlink out of the parent or a missing folder must each fail the task,
never widen the tools. A bound argument is overwritten whatever the model sends.
"""

import asyncio
import dataclasses

import pytest

from daedalus.agent import Agent
from daedalus.bundle import Bundle, BundleError
from daedalus.engines.claude_code import ClaudeCodeEngine
from daedalus.protocols import LLMResponse, ToolCall
from daedalus.task import ReasonCode, Task, TaskState
from daedalus.tools import workspace
from tests.fakes import FakeTools, ScriptedLLM


def tool_turn(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(text="", tool_calls=list(calls))


def write_bundle(root, tools_yaml: str):
    root.mkdir(exist_ok=True)
    (root / "agent.yaml").write_text("name: x\nmodel:\n  id: m\ntools:\n" + tools_yaml)
    (root / "agent-card.json").write_text("{}")
    (root / "persona.md").write_text("You are x.")
    return Bundle.load(root)


@pytest.fixture
def parent(tmp_path):
    p = tmp_path / "work"
    for name in ("a", "b"):
        (p / name).mkdir(parents=True)
        (p / name / "f.txt").write_text(f"in {name}")
    return p


def files_bundle(tmp_path, parent, roots="['{workspace}']"):
    return write_bundle(
        tmp_path / "bundle",
        f"  workspace:\n    parent: '{parent}'\n"
        f"  builtin:\n    files:\n      roots: {roots}\n"
        "  allow: ['files.*']\n",
    )


def task_in(name, text="read", **extra) -> Task:
    task = Task.from_text(text)
    if name is not None:
        task.metadata[workspace.WORKSPACE_KEY] = name
    task.metadata.update(extra)
    return task


# -- resolving the name ------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "../a", "a/../b", "/etc", "a/b", ".", "..", ".hidden"])
def test_a_name_that_is_not_one_plain_folder_is_refused(parent, name):
    with pytest.raises(workspace.WorkspaceError):
        workspace.resolve(parent, name)


def test_a_missing_folder_is_refused(parent):
    with pytest.raises(workspace.WorkspaceError, match="not a directory"):
        workspace.resolve(parent, "nope")


def test_a_symlink_out_of_the_parent_is_refused(parent, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (parent / "link").symlink_to(outside)
    with pytest.raises(workspace.WorkspaceError, match="outside"):
        workspace.resolve(parent, "link")


def test_a_valid_name_resolves_under_the_parent(parent):
    assert workspace.resolve(parent, "a") == (parent / "a").resolve()


def test_a_sub_root_stays_inside_the_workspace(parent):
    ws = (parent / "a").resolve()
    assert workspace.expand(["{workspace}/gh", "/fixed"], ws) == [str(ws / "gh"), "/fixed"]
    for bad in ("{workspace}/../b", "{workspace}//etc", "{workspace}/"):
        with pytest.raises(workspace.WorkspaceError):
            workspace.expand([bad], ws)


# -- load-time checks ----------------------------------------------------------------


def test_the_token_without_a_parent_fails_at_load(tmp_path):
    bundle = write_bundle(
        tmp_path / "bundle",
        "  builtin:\n    files:\n      roots: ['{workspace}']\n  allow: ['files.*']\n",
    )
    with pytest.raises(BundleError, match="tools.workspace.parent"):
        Agent(bundle, llm=ScriptedLLM("x"))


def test_a_parent_that_is_not_a_directory_fails_at_load(tmp_path):
    with pytest.raises(BundleError, match="not a directory"):
        Agent(files_bundle(tmp_path, tmp_path / "missing"), llm=ScriptedLLM("x"))


def test_a_sub_root_that_climbs_out_fails_at_load(tmp_path, parent):
    with pytest.raises(BundleError):
        Agent(files_bundle(tmp_path, parent, "['{workspace}/../x']"), llm=ScriptedLLM("x"))


# -- tasks -----------------------------------------------------------------------------


async def test_the_files_tools_read_this_tasks_workspace_only(tmp_path, parent):
    read_b = ToolCall(id="1", name="files__read", arguments={"path": str(parent / "b/f.txt")})
    read_a = ToolCall(id="2", name="files__read", arguments={"path": str(parent / "a/f.txt")})
    llm = ScriptedLLM(tool_turn(read_b), tool_turn(read_a), "done")
    agent = Agent(files_bundle(tmp_path, parent), llm=llm)
    task = await agent.handle_task(task_in("a"))
    assert task.state is TaskState.COMPLETED
    results = [m["content"] for m in llm.calls[-1] if m["role"] == "tool"]
    assert "outside the allowed roots" in results[0]  # b is refused
    assert results[1] == "in a"


@pytest.mark.parametrize("name", [None, "../b", "nope"])
async def test_a_task_without_a_valid_workspace_fails_before_any_tool(tmp_path, parent, name):
    llm = ScriptedLLM("should not run")
    agent = Agent(files_bundle(tmp_path, parent), llm=llm)
    task = await agent.handle_task(task_in(name))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.POLICY_VIOLATION
    assert "workspace refused" in task.agent_text()
    assert llm.calls == []


async def test_two_concurrent_tasks_each_see_their_own_workspace(tmp_path, parent):
    seen: dict[str, str] = {}

    class Both:
        """An LLM that reads f.txt in whichever workspace its task is in."""

        def __init__(self):
            self.step: dict[str, int] = {}

        async def complete(self, messages, tools=None):
            user = next(m["content"] for m in messages if m["role"] == "user")
            name = user.split()[-1]
            n = self.step.get(name, 0)
            self.step[name] = n + 1
            if n == 0:
                await asyncio.sleep(0.01)  # interleave the two tasks
                path = str(parent / name / "f.txt")
                return tool_turn(ToolCall(id=name, name="files__read", arguments={"path": path}))
            seen[name] = messages[-1]["content"]
            return LLMResponse(text="ok")

    agent = Agent(files_bundle(tmp_path, parent), llm=Both())
    a, b = await asyncio.gather(
        agent.handle_task(task_in("a", "read a")), agent.handle_task(task_in("b", "read b"))
    )
    assert a.state is b.state is TaskState.COMPLETED
    assert "in a" in seen["a"] and "in b" in seen["b"]


async def test_a_bundle_without_a_workspace_is_unchanged(tmp_path):
    (tmp_path / "n.txt").write_text("plain")
    bundle = write_bundle(
        tmp_path / "bundle",
        f"  builtin:\n    files:\n      roots: ['{tmp_path}']\n  allow: ['files.*']\n",
    )
    call = ToolCall(id="1", name="files__read", arguments={"path": str(tmp_path / "n.txt")})
    llm = ScriptedLLM(tool_turn(call), "done")
    task = await Agent(bundle, llm=llm).handle_task(Task.from_text("read"))
    assert task.state is TaskState.COMPLETED
    assert "plain" in llm.calls[1][-1]["content"]


# -- bind ------------------------------------------------------------------------------

POST = {
    "name": "review.post_review",
    "description": "post",
    "parameters": {
        "type": "object",
        "properties": {"body": {"type": "string"}, "repo": {"type": "string"},
                       "pr": {"type": "string"}},
        "required": ["body", "repo", "pr"],
    },
}


def bind_agent(tmp_path, llm, tools):
    bundle = write_bundle(
        tmp_path / "bundle",
        "  bind:\n    review.post_review: {repo: review.repo, pr: review.pr}\n"
        "  allow: ['review.*']\n",
    )
    return Agent(bundle, llm=llm, tools=tools)


async def test_bound_arguments_are_hidden_and_overwritten(tmp_path):
    sneaky = {"body": "LGTM", "repo": "evil/repo", "pr": "1"}
    llm = ScriptedLLM(tool_turn(ToolCall(id="1", name="review__post_review", arguments=sneaky)),
                      "posted")
    tools = FakeTools([POST])
    agent = bind_agent(tmp_path, llm, tools)
    task = await agent.handle_task(
        task_in(None, review={"repo": "tenishai/agents", "pr": 171})
    )
    assert task.state is TaskState.COMPLETED
    assert tools.calls == [("review.post_review",
                            {"body": "LGTM", "repo": "tenishai/agents", "pr": 171})]
    offered = llm.tools_seen[0][0]["function"]["parameters"]
    assert set(offered["properties"]) == {"body"}
    assert offered["required"] == ["body"]


async def test_a_missing_bound_value_fails_the_call_not_falls_back(tmp_path):
    call = ToolCall(id="1", name="review__post_review",
                    arguments={"body": "x", "repo": "evil/repo", "pr": "1"})
    llm = ScriptedLLM(tool_turn(call), "gave up")
    tools = FakeTools([POST])
    agent = bind_agent(tmp_path, llm, tools)
    await agent.handle_task(task_in(None, review={"repo": "tenishai/agents"}))
    assert tools.calls == []
    assert "no 'review.pr'" in llm.calls[1][-1]["content"]


# -- engines ---------------------------------------------------------------------------


def test_claude_code_runs_in_the_tasks_workspace(tmp_path, parent):
    """The task's workspace wins over the bundle's engine.claude_code.cwd."""
    from daedalus.bundle import ClaudeCodeSpec
    from tests.test_engine_claude_code import _turn, sdk

    turn = dataclasses.replace(_turn(tmp_path), workspace=parent / "a")
    engine = ClaudeCodeEngine(ClaudeCodeSpec(cwd=str(tmp_path)))
    assert str(engine.options(turn, sdk, "p").cwd) == str(parent / "a")


async def test_a_tool_call_dispatched_in_a_fresh_context_still_has_the_workspace(
    tmp_path, parent, monkeypatch
):
    """An engine may run a tool call on a task created outside _run_engine's context (the
    Claude Code SDK's in-process MCP server); the workspace must reach it anyway."""
    import contextvars

    from daedalus.task import TaskState as State

    seen = []

    class FreshContextEngine:
        name = "fresh"

        async def run(self, turn):
            call = turn.call_tool("files.read", {"path": str(parent / "a" / "f.txt")})
            seen.append(await asyncio.create_task(call, context=contextvars.Context()))
            return turn.task.reply("done", state=State.COMPLETED)

    agent = Agent(files_bundle(tmp_path, parent), llm=ScriptedLLM("x"))
    monkeypatch.setattr(agent, "_engine", lambda: FreshContextEngine())
    task = await agent.handle_task(task_in("a"))
    assert task.state is TaskState.COMPLETED
    assert seen == ["in a"]
