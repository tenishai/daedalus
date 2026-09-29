"""exec.run through LocalWorkspace: real processes, no stubs (tenishai/agents#51)."""

import os
import sys
import time

import pytest

from daedalus.bundle import Bundle, BundleError
from daedalus.tools.exec import ExecToolProvider

PY = sys.executable


@pytest.fixture
def root(tmp_path):
    (tmp_path / "sub").mkdir()
    return tmp_path


async def run(provider, **payload):
    return await provider.call("exec.run", payload)


async def test_runs_a_command_and_reports_exit_and_output(root):
    out = await run(ExecToolProvider([str(root)]), command=[PY, "-c", "print('hi')"])
    assert out.startswith("exit 0 (")
    assert "--- stdout ---\nhi\n" in out


async def test_a_failing_command_reports_its_code_and_stderr(root):
    out = await run(
        ExecToolProvider([str(root)]),
        command=[PY, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"],
    )
    assert out.startswith("exit 3 (")
    assert "--- stderr ---\nbad" in out


async def test_runs_in_the_first_root_by_default_and_in_a_cwd_under_it(root):
    provider = ExecToolProvider([str(root)])
    here = [PY, "-c", "import os; print(os.getcwd())"]
    assert str(root.resolve()) in await run(provider, command=here)
    assert str((root / "sub").resolve()) in await run(provider, command=here, cwd=str(root / "sub"))


@pytest.mark.parametrize("command", ["echo hi", [], ["echo", 3], None])
async def test_the_command_must_be_a_non_empty_argv_list(root, command):
    with pytest.raises(ValueError, match="non-empty list of strings"):
        await run(ExecToolProvider([str(root)]), command=command)


async def test_a_cwd_outside_the_roots_is_refused(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (root / "escape").symlink_to(outside)
    provider = ExecToolProvider([str(root)])
    for cwd in (str(outside), str(root / "escape"), str(root / ".." )):
        with pytest.raises(PermissionError, match="outside the allowed roots"):
            await run(provider, command=[PY, "-c", "pass"], cwd=cwd)


async def test_the_commands_allowlist_is_enforced(root):
    provider = ExecToolProvider([str(root)], commands=["echo"])
    assert "--- stdout ---\nok\n" in await run(provider, command=["echo", "ok"])
    with pytest.raises(PermissionError, match="not an allowed program"):
        await run(provider, command=[PY, "-c", "pass"])


@pytest.mark.parametrize("bad", ["/bin/echo", "./echo", ""])
def test_the_allowlist_takes_bare_names_only(root, bad):
    with pytest.raises(ValueError, match="bare program names"):
        ExecToolProvider([str(root)], commands=[bad])


async def test_the_agents_environment_does_not_reach_the_command(root, monkeypatch):
    monkeypatch.setenv("AGENT_SECRET", "s3cret")
    monkeypatch.setenv("PASSED_ON", "yes")
    provider = ExecToolProvider([str(root)], env=["PASSED_ON"])
    out = await run(provider, command=[PY, "-c", "import os; print(sorted(os.environ))"])
    assert "AGENT_SECRET" not in out
    assert "PASSED_ON" in out and "PATH" in out


async def test_a_timeout_kills_the_whole_process_group(root):
    provider = ExecToolProvider([str(root)], timeout=30)
    # The child starts a grandchild, prints its pid, then both sleep.
    script = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "print(p.pid, flush=True); time.sleep(60)"
    )
    started = time.monotonic()
    out = await run(provider, command=[PY, "-c", script], timeout=1)
    assert time.monotonic() - started < 15
    assert out.startswith("timed out after 1s; the process group was killed")
    grandchild = int(out.split("--- stdout ---\n")[1].split()[0])
    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the grandchild outlived the timeout")


async def test_a_requested_timeout_is_capped_at_the_configured_one(root):
    provider = ExecToolProvider([str(root)], timeout=1)
    out = await run(provider, command=[PY, "-c", "import time; time.sleep(30)"], timeout=600)
    assert out.startswith("timed out after 1s")


async def test_long_output_keeps_its_start_and_its_end(root):
    provider = ExecToolProvider([str(root)], max_output=1000)
    script = "print('START' + 'x' * 100000 + 'END')"
    out = await run(provider, command=[PY, "-c", script])
    stdout = out.split("--- stdout ---\n")[1]
    assert stdout.startswith("START") and "END" in stdout
    assert "bytes elided" in stdout and len(stdout) < 2000


async def test_an_unknown_program_is_a_plain_error(root):
    with pytest.raises(ValueError, match="program not found"):
        await run(ExecToolProvider([str(root)]), command=["no-such-program-xyz"])


@pytest.mark.parametrize("config", [{"timeout": "0"}, {"max_output": "lots"}, {"backend": "e2b"}])
def test_bad_config_is_refused_at_load(root, config):
    with pytest.raises(ValueError):
        ExecToolProvider([str(root)], **config)


def _bundle(tmp_path, ceiling: str):
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.\n")
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "tools:\n  builtin:\n    exec:\n      roots: ['.']\n      timeout: '${EXEC_TIMEOUT:-60}'\n"
        "  allow: ['exec.run']\n"
        f"approvals:\n  ceiling:\n{ceiling}"
    )
    return Bundle.load(tmp_path)


def test_exec_without_the_ceiling_consent_is_a_bundle_error(tmp_path):
    from daedalus.agent import Agent

    with pytest.raises(BundleError, match="approvals.ceiling.exec: true"):
        Agent(_bundle(tmp_path, "    write: false\n"))


async def test_exec_with_the_ceiling_consent_is_offered(tmp_path):
    from daedalus.agent import Agent

    agent = Agent(_bundle(tmp_path, "    exec: true\n"))
    try:
        assert "exec.run" in {t["name"] for t in await agent.tools.tools()}
    finally:
        await agent.aclose()
