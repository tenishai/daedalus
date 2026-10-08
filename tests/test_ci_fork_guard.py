"""Every self-hosted CI job keeps its fork guard (daedalus#78).

The jobs in `.github/workflows/test.yml` that run on the self-hosted pool carry
an `if:` that excludes pull requests from forks, and `fork-pr` runs on a hosted
runner under the literal negation of that guard, so a fork PR still gets a run.
A job copied from `test` can keep the runner and lose the guard; techne caught
that twice, which is why it has the same check (tests/test_ci_workflow_triggers.py
in tenishai/techne).

The guard is defence in depth only: for `pull_request` GitHub runs the PR's own
copy of the workflow, so a fork can delete it. This test keeps honest edits from
dropping it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "test.yml"

# The allow-list: pushes, manual runs, and pull requests from this repository.
TRUSTED = (
    "github.event_name == 'push' || github.event_name == 'workflow_dispatch' || "
    "(github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name == github.repository)"
)
# Stricter than TRUSTED: a fork cannot trigger workflow_dispatch.
DISPATCH_ONLY = "github.event_name == 'workflow_dispatch'"
SAFE_GUARDS = {TRUSTED, DISPATCH_ONLY}


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _guard(job: dict) -> str:
    """The job's `if:` with any `${{ }}` wrapper removed, or "" when it has none."""
    expr = str(job.get("if", "")).strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    return expr


def _self_hosted(job: dict) -> bool:
    runs_on = job.get("runs-on", [])
    labels = [runs_on] if isinstance(runs_on, str) else list(runs_on)
    return "self-hosted" in labels


def test_every_self_hosted_job_is_guarded():
    jobs = _jobs()
    self_hosted = {name: job for name, job in jobs.items() if _self_hosted(job)}
    assert self_hosted, "no self-hosted job found; did the runner labels change?"
    for name, job in self_hosted.items():
        assert _guard(job) in SAFE_GUARDS, (
            f"job {name!r} runs on the self-hosted pool with if: {_guard(job)!r}. "
            "Use the allow-list guard from the `test` job, or run it on a hosted runner."
        )


def test_fork_pr_is_the_literal_negation_of_the_guard():
    fork_pr = _jobs()["fork-pr"]
    assert not _self_hosted(fork_pr), "fork-pr must run on a hosted runner"
    assert _guard(fork_pr) == f"!({TRUSTED})", (
        "fork-pr's if: must be the exact negation of the self-hosted guard, so "
        "every event the guard excludes still gets a hosted run"
    )
