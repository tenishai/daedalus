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
RELEASE = WORKFLOW.with_name("release.yml")


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
        assert labels == ["ubuntu-latest"], (
            f"job {name!r} runs on {labels}; a public repo's CI must not reach a self-hosted runner"
        )


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


def _release_jobs() -> dict:
    return yaml.safe_load(RELEASE.read_text())["jobs"]


def test_release_runs_the_tests_first_and_only_on_hosted_runners():
    jobs = _release_jobs()
    assert jobs["checks"]["uses"] == "./.github/workflows/test.yml"
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = yaml.safe_load(WORKFLOW.read_text())[True]
    assert "workflow_call" in triggers, "release.yml calls test.yml: it must allow workflow_call"
    for name, job in jobs.items():
        if name == "checks":
            continue
        assert "checks" in job["needs"], f"{name} must wait for the tests"
        for label in _labels(job):
            assert label.startswith("ubuntu-"), f"{name} runs on {label!r}, not GitHub-hosted"


def test_release_actions_are_pinned_and_publish_only_from_a_tag():
    pinned = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
    steps = [step for job in _release_jobs().values() for step in job.get("steps", [])]
    unpinned = [s["uses"] for s in steps if "uses" in s and not pinned.match(s["uses"])]
    assert not unpinned, f"pin these to a full commit SHA: {unpinned}"
    pushes = [s for s in steps if s.get("with", {}).get("push") is True]
    assert pushes, "no step publishes the image"
    for s in pushes:
        assert "publish == 'true'" in s.get("if", ""), "an image is pushed only from a vX.Y.Z tag"
