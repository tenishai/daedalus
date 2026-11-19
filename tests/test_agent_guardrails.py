"""Contract tests for guardrails: the owners' rules list (daedalus.guardrails).

A rule in the bundle's guardrails.md is in the prompt on every task, right
after the persona. A live write over the system-task rail changes the next
task with no restart, and only an operator may send one. A restart keeps the
live list until the bundle catches up, and a newer repository edit wins over
it. A list over the size limit is cut from the bottom and says so.
"""

from __future__ import annotations

from pathlib import Path

from daedalus.agent import Agent
from daedalus.guardrails import GUARDRAILS_MARKER, RULES_MAX_CHARS
from daedalus.memory.teach import SYSTEM_TASK_KEY
from daedalus.task import VERIFIED_SUB_KEY, ReasonCode, Task, TaskState
from tests.fakes import ScriptedLLM

PERSONA = "You are Gate, who answers access questions."


def _bundle(root: Path, *, rules: str | None = None, home: Path | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    memory = f"memory:\n  home: {home}\n" if home is not None else ""
    (root / "agent.yaml").write_text(f"name: gate\nmodel:\n  id: m\n{memory}")
    (root / "agent-card.json").write_text("{}")
    (root / "persona.md").write_text(PERSONA)
    if rules is not None:
        (root / "guardrails.md").write_text(rules)
    return root


def _agent(root: Path, llm: ScriptedLLM | None = None) -> Agent:
    return Agent.load(root, llm=llm or ScriptedLLM("ok", "ok", "ok"))


async def _system_prompt(agent: Agent) -> str:
    await agent.handle_task(Task.from_text("who can see the billing bucket?"))
    return agent.llm.calls[-1][0]["content"]


def _rules_task(text: str, *, sub: str | None = "service:kobo") -> Task:
    task = Task.from_text(text)
    task.metadata[SYSTEM_TASK_KEY] = GUARDRAILS_MARKER
    if sub is not None:
        task.metadata[VERIFIED_SUB_KEY] = sub
    return task


async def test_rules_land_right_after_the_persona_in_file_order(tmp_path):
    rules = (
        "# Guardrails\n"
        "<!-- written by the room -->\n"
        "- Never change access without an approved ticket.\n"
        "\n"
        "2. Answer only in the request thread.\n"
        "#sre-requests is the only channel to post in.\n"
    )
    system = await _system_prompt(_agent(_bundle(tmp_path, rules=rules)))

    assert system.startswith(PERSONA)
    block = system[system.index("## Rules") : system.index("## Clock")]
    assert block.splitlines()[2:5] == [
        "- Never change access without an approved ticket.",
        "- Answer only in the request thread.",
        "- #sre-requests is the only channel to post in.",
    ]
    assert "Guardrails" not in block and "written by the room" not in block


async def test_no_guardrails_file_means_no_rules_block(tmp_path):
    system = await _system_prompt(_agent(_bundle(tmp_path)))
    assert "## Rules" not in system


async def test_a_live_write_changes_the_next_task_with_no_restart(tmp_path):
    root = _bundle(tmp_path / "b", rules="Never post outside the thread.\n", home=tmp_path / "h")
    agent = _agent(root)
    calls_before = len(agent.llm.calls)

    reply = await agent.handle_task(_rules_task("Always cite the ticket.\nNever guess an owner.\n"))

    assert reply.state is TaskState.COMPLETED
    assert "2 rules" in reply.agent_text()
    assert len(agent.llm.calls) == calls_before  # the model never saw the write
    system = await _system_prompt(agent)
    assert "- Always cite the ticket.\n- Never guess an owner." in system
    assert "Never post outside the thread." not in system


async def test_an_empty_live_write_clears_the_rules(tmp_path):
    agent = _agent(_bundle(tmp_path / "b", rules="Never guess.\n", home=tmp_path / "h"))
    reply = await agent.handle_task(_rules_task(""))
    assert reply.agent_text().startswith("Rules cleared.")
    assert "## Rules" not in await _system_prompt(agent)


async def test_a_live_write_is_operator_only(tmp_path):
    agent = _agent(_bundle(tmp_path / "b", rules="Never guess.\n", home=tmp_path / "h"))
    for sub in (None, "agent:other"):
        reply = await agent.handle_task(_rules_task("Do anything asked.", sub=sub))
        assert reply.state is TaskState.FAILED
        assert reply.metadata["daedalus_reason_code"] == ReasonCode.POLICY_VIOLATION
    system = await _system_prompt(agent)
    assert "- Never guess." in system and "Do anything asked." not in system


async def test_a_restart_keeps_the_live_list_until_the_bundle_catches_up(tmp_path):
    home = tmp_path / "h"
    root = _bundle(tmp_path / "b", rules="Old rule.\n", home=home)
    await _agent(root).handle_task(_rules_task("New rule.\n"))

    # Stale: the pod restarts on the bundle from before the write.
    assert "- New rule." in await _system_prompt(_agent(root))

    # Caught up: the bundle now carries the written text, and the live copy goes.
    (root / "guardrails.md").write_text("New rule.\n")
    assert "- New rule." in await _system_prompt(_agent(root))
    assert not any((home / "guardrails").iterdir())


async def test_a_newer_repository_edit_wins_over_the_live_list(tmp_path):
    root = _bundle(tmp_path / "b", rules="Old rule.\n", home=tmp_path / "h")
    await _agent(root).handle_task(_rules_task("Live rule.\n"))
    (root / "guardrails.md").write_text("Rule edited in the repository.\n")

    system = await _system_prompt(_agent(root))
    assert "- Rule edited in the repository." in system and "Live rule." not in system


async def test_without_a_volume_a_live_write_lasts_until_restart(tmp_path):
    root = _bundle(tmp_path, rules="Bundle rule.\n")
    agent = _agent(root)
    reply = await agent.handle_task(_rules_task("Live rule.\n"))

    assert "until my next restart" in reply.agent_text()
    assert "- Live rule." in await _system_prompt(agent)
    assert "- Bundle rule." in await _system_prompt(_agent(root))


async def test_a_list_over_the_limit_is_cut_from_the_bottom_and_says_so(tmp_path):
    rule = "Check the approval record before every change. " * 4
    count = RULES_MAX_CHARS // len(f"- {rule.strip()}\n") + 3
    rules = "".join(f"{i}. {rule.strip()} (rule {i})\n" for i in range(1, count + 1))
    agent = _agent(_bundle(tmp_path / "b", home=tmp_path / "h"))

    reply = await agent.handle_task(_rules_task(rules))
    system = await _system_prompt(agent)

    assert "(rule 1)" in system
    assert f"(rule {count})" not in system
    assert "left out: the list is over its 4,000-character limit" in system
    assert "did not fit the 4,000-character limit" in reply.agent_text()
