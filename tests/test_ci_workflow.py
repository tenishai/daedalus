"""The repo's own CI: GitHub-hosted runners only, and one `ci` summary job that
is green only when every test leg is (agora discussion 71, rule 4).

Until 2026-11-23 this file was test_ci_fork_guard.py (daedalus#78): the self-
hosted jobs had to keep a same-repo guard and `fork-pr` had to be its literal
negation. With no self-hosted job left there is nothing to guard; what matters
now is that no job can reach one, and that the ruleset's required check cannot
pass on a skipped or failed leg.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "test.yml"


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _labels(job: dict) -> list[str]:
    runs_on = job.get("runs-on", [])
    return [runs_on] if isinstance(runs_on, str) else list(runs_on)


def test_every_job_runs_on_a_github_hosted_runner():
    jobs = _jobs()
    assert jobs
    for name, job in jobs.items():
        labels = _labels(job)
        assert labels == ["ubuntu-latest"], f"job {name!r} runs on {labels}; a public repo's CI must not reach a self-hosted runner"


def test_the_ci_job_needs_test_and_fails_unless_it_succeeded():
    ci = _jobs()["ci"]
    assert ci["needs"] == ["test"]
    assert ci["if"] == "always()", "a skipped `ci` would let a failed leg pass the ruleset"
    assert any('needs.test.result }}" = "success"' in step.get("run", "") for step in ci["steps"])


def test_the_techne_leg_is_not_a_pull_request_gate():
    """It needs a repo secret a fork's PR cannot have, so it must not run on
    pull_request (it would fail) and `ci` must not need it (it would skip)."""
    techne = _jobs()["techne"]
    assert "pull_request" not in techne["if"]
    assert "techne" not in _jobs()["ci"]["needs"]


def test_every_action_is_pinned_to_a_commit_sha():
    pinned = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
    unpinned = [
        step["uses"]
        for job in _jobs().values()
        for step in job.get("steps", [])
        if "uses" in step and not pinned.match(step["uses"])
    ]
    assert not unpinned, f"pin these to a full commit SHA: {unpinned}"
