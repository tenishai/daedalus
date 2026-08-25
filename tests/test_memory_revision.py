"""RED-first tests for the post-boot belief revision pass.

techne's contradiction machinery (conflict_check_emit/ingest, promote_candidate)
is exercised against a REAL temp techne db throughout -- no fakes for techne
itself, only for the model (ScriptedLLM, already used across the suite).
"""

from __future__ import annotations

import pytest

# techne is not on PyPI; local runs use the sibling checkout (PYTHONPATH=src:../techne).
techne = pytest.importorskip("techne")

import asyncio  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import warnings  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

from daedalus.agent import Agent  # noqa: E402
from daedalus.bundle import MemorySpec  # noqa: E402
from daedalus.memory.revision import RevisionReport, run_revision_pass  # noqa: E402
from daedalus.memory.techne import TechneMemoryStore  # noqa: E402
from daedalus.protocols import LLMResponse  # noqa: E402
from daedalus.tools.colleagues import ColleaguesToolProvider  # noqa: E402
from tests.fakes import ScriptedLLM  # noqa: E402

# ---------------------------------------------------------------------------
# Seeding helpers -- a real temp techne db, built through techne's own
# public knowledge.store(), not hand-rolled INSERTs.
# ---------------------------------------------------------------------------


def _seed_contradiction(
    db_path: Path,
    *,
    older_source: str,
    newer_source: str,
    tag: str = "office",
) -> tuple[int, int]:
    """Two live knowledge rows, same type and tag (so techne's structural
    selection pairs them), the second strictly newer by event_at.

    store() never takes event_at, so it is set directly afterward -- this
    keeps the supersede direction deterministic regardless of how fast the
    two store() calls land inside the same wall-clock second.
    """
    from techne.core.database import connect
    from techne.core.knowledge import store

    older_id = store(db_path, "The office is in Building A.", tags=tag, source=older_source)
    newer_id = store(db_path, "The office is in Building C.", tags=tag, source=newer_source)

    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE knowledge SET event_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00", older_id),
        )
        conn.execute(
            "UPDATE knowledge SET event_at = ? WHERE id = ?",
            ("2026-06-01T00:00:00", newer_id),
        )
        conn.commit()
    finally:
        conn.close()
    return older_id, newer_id


def _supersede_verdict(survivor_id: int, content: str, reason: str = "contradiction") -> str:
    """Exactly the shape techne.core.conflict.parse_conflict_response expects:
    a JSON array, one object per comparison id, per CONFLICT_PROMPT."""
    return json.dumps(
        [{"id": survivor_id, "verdict": "SUPERSEDE", "content": content, "reason": reason}]
    )


def _knowledge_row(db_path: Path, knowledge_id: int) -> dict | None:
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM knowledge WHERE id = ?", (knowledge_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _candidate_rows(db_path: Path) -> list[dict]:
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM knowledge_candidates").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _agent_bundle(tmp_path: Path, memory_yaml: str = "") -> Path:
    (tmp_path / "agent.yaml").write_text(f"name: x\nmodel:\n  id: m\n{memory_yaml}")
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    return tmp_path


# ---------------------------------------------------------------------------
# MemorySpec.revision -- the schema knob
# ---------------------------------------------------------------------------


def test_revision_defaults_to_none_and_is_absent_by_default():
    assert MemorySpec().revision is None


def test_revision_accepts_the_operator_policy():
    assert MemorySpec(revision="operator").revision == "operator"


def test_unknown_revision_value_warns_and_disables_rather_than_crashing():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        spec = MemorySpec(revision="always")
    assert spec.revision is None
    assert any("always" in str(w.message) for w in caught)


# ---------------------------------------------------------------------------
# Wiring: Agent._start_revision_pass -- the post-boot scheduling seam
# ---------------------------------------------------------------------------


def test_gate_off_never_constructs_the_pass(tmp_path):
    """Absent memory.revision (the default): no task, no import. Byte-identical
    to a runtime that has never heard of belief revision."""
    sys.modules.pop("daedalus.memory.revision", None)
    root = _agent_bundle(tmp_path)
    agent = Agent.load(root, llm=ScriptedLLM())
    assert agent.bundle.spec.memory.revision is None

    task = agent._start_revision_pass()

    assert task is None
    assert "daedalus.memory.revision" not in sys.modules


def test_revision_enabled_without_techne_memory_warns_and_disables(tmp_path):
    root = _agent_bundle(tmp_path, "memory:\n  revision: operator\n")
    agent = Agent.load(root, llm=ScriptedLLM())  # memory defaults to NoMemory: no db_path

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        task = agent._start_revision_pass()

    assert task is None
    assert any("techne" in str(w.message) for w in caught)


async def test_start_revision_pass_wires_the_agents_own_model_and_db(tmp_path):
    """The actual wiring: db_path comes from self.memory, the model comes from
    self.llm (the same seam the task loop calls), events land on self.events."""
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="intake:chat-x"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    root = _agent_bundle(tmp_path, "memory:\n  personal: techne\n  revision: operator\n")
    llm = ScriptedLLM(verdict)
    agent = Agent.load(root, llm=llm, memory=TechneMemoryStore(db_path=db_path))
    _, queue = agent.events.subscribe()

    task = agent._start_revision_pass()
    assert task is not None
    await task  # run_revision_pass never raises; safe to await directly

    assert _knowledge_row(db_path, older_id)["superseded_by"] == newer_id
    assert llm.tools_seen == [None]  # the judge is called with no tools offered

    seen_types = []
    while queue.qsize():
        seen_types.append(queue.get_nowait()["type"])
    assert "revision_done" in seen_types


async def test_serve_calls_the_scheduling_seam_once(tmp_path, monkeypatch):
    """The real serve() entrypoint reaches the seam -- not just a hand call."""
    root = _agent_bundle(tmp_path)
    agent = Agent.load(root, llm=ScriptedLLM())

    calls = []
    original = agent._start_revision_pass

    def spy():
        calls.append(True)
        return original()

    monkeypatch.setattr(agent, "_start_revision_pass", spy)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # cli transport sees instant EOF

    await asyncio.wait_for(agent.serve(), timeout=5)

    assert calls == [True]


# ---------------------------------------------------------------------------
# run_revision_pass -- the policy itself, against a real temp techne db
# ---------------------------------------------------------------------------


async def test_operator_taught_correction_retires_the_stale_fact(tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="intake:chat-x"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    report = await run_revision_pass(db_path, ScriptedLLM(verdict), policy="operator")

    assert report == RevisionReport(judged=1, staged=1, promoted=1, skipped=0, errors=0)
    assert _knowledge_row(db_path, older_id)["superseded_by"] == newer_id

    from techne.core.search import search

    results = search(db_path, "office building", table="knowledge", use_vectors=False)
    assert older_id not in [r["id"] for r in results]
    assert newer_id in [r["id"] for r in results]

    [candidate] = [c for c in _candidate_rows(db_path) if json.loads(c["target_ids"]) == [
        older_id,
        newer_id,
    ]]
    assert candidate["status"] == "promoted"
    assert candidate["decided_by"] == "operator"
    assert candidate["decision_reason"] == "taught correction"


async def test_non_operator_source_stays_pending(tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="seed:facilities", tag="printer"
    )
    verdict = _supersede_verdict(newer_id, "The printer is on floor 4.")

    report = await run_revision_pass(db_path, ScriptedLLM(verdict), policy="operator")

    assert report == RevisionReport(judged=1, staged=1, promoted=0, skipped=1, errors=0)
    assert _knowledge_row(db_path, older_id)["superseded_by"] is None
    [candidate] = _candidate_rows(db_path)
    assert candidate["status"] == "pending"
    assert candidate["decided_by"] is None


async def test_judge_failure_is_isolated_and_counted(tmp_path):
    db_path = tmp_path / "techne.db"
    _seed_contradiction(db_path, older_source="seed:onboarding", newer_source="intake:chat-x")

    class RaisingModel:
        async def complete(self, messages, tools=None):
            raise RuntimeError("model unreachable")

    events = []
    report = await run_revision_pass(
        db_path, RaisingModel(), policy="operator", emit=lambda t, **f: events.append(t)
    )

    assert report.judged == 0
    assert report.errors == 1
    assert events == ["revision_start", "revision_done"]  # no judged/promoted for a failed check
    assert not _candidate_rows(db_path)  # nothing staged when the judge never answered


async def test_db_failure_is_isolated_and_counted(tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="intake:chat-x"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    with patch(
        "techne.core.conflict.conflict_check_ingest",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        report = await run_revision_pass(db_path, ScriptedLLM(verdict), policy="operator")

    assert report.errors == 1
    assert report.promoted == 0
    assert _knowledge_row(db_path, older_id)["superseded_by"] is None  # untouched


async def test_second_run_is_idempotent(tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="intake:chat-x"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    first = await run_revision_pass(db_path, ScriptedLLM(verdict), policy="operator")
    assert first.promoted == 1

    second = await run_revision_pass(db_path, ScriptedLLM(verdict), policy="operator")

    assert second == RevisionReport(judged=0, staged=0, promoted=0, skipped=0, errors=0)
    assert len(_candidate_rows(db_path)) == 1  # no duplicate candidate from the re-run


async def test_events_emitted_in_order():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "techne.db"
        older_id, newer_id = _seed_contradiction(
            db_path, older_source="seed:onboarding", newer_source="intake:chat-x"
        )
        verdict = _supersede_verdict(newer_id, "The office is in Building C.")

        events: list[str] = []
        report = await run_revision_pass(
            db_path,
            ScriptedLLM(verdict),
            policy="operator",
            emit=lambda t, **f: events.append(t),
        )

        assert events == [
            "revision_start",
            "revision_judged",
            "revision_promoted",
            "revision_done",
        ]
        assert report.promoted == 1


async def test_unknown_policy_is_a_swallowed_error(tmp_path):
    db_path = tmp_path / "techne.db"
    _seed_contradiction(db_path, older_source="seed:onboarding", newer_source="intake:chat-x")

    report = await run_revision_pass(db_path, ScriptedLLM(), policy="always")

    assert report == RevisionReport(judged=0, staged=0, promoted=0, skipped=0, errors=1)


async def test_emit_callback_that_raises_is_caught_and_pass_still_completes(tmp_path):
    """An emit callback that raises on every call does not prevent the pass
    from completing and returning a valid report. The docstring promise
    'Never raises' must hold even if the callback itself is broken."""
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="intake:chat-x"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    def breaking_emit(event_type: str, **fields) -> None:
        raise RuntimeError(f"emit callback broken: {event_type}")

    report = await run_revision_pass(
        db_path, ScriptedLLM(verdict), policy="operator", emit=breaking_emit
    )

    # The pass completes successfully despite emit raising on every call.
    assert report.promoted == 1
    assert report.judged == 1
    assert report.errors == 0  # emit failures are not counted as pass errors
    # The promotion actually happens even though emit is broken.
    assert _knowledge_row(db_path, older_id)["superseded_by"] == newer_id


async def test_dual_operator_taught_both_facts_supersede_scenario(tmp_path):
    """Regression test: seed TWO contradicting facts BOTH with operator-taught
    sources (intake:chat-...), script a SUPERSEDE verdict, verify the candidate
    promotes, exactly one row gets superseded_by set, and the survivor matches
    techne's direction logic (not a re-implemented copy)."""
    from techne.core.database import connect
    from techne.core.knowledge import store

    db_path = tmp_path / "techne.db"

    # Seed two operator-taught facts that conflict (same tag, different claim).
    older_id = store(
        db_path, "The office is in Building A.", tags="office", source="intake:chat-older"
    )
    newer_id = store(
        db_path, "The office is in Building C.", tags="office", source="intake:chat-newer"
    )

    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE knowledge SET event_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00", older_id),
        )
        conn.execute(
            "UPDATE knowledge SET event_at = ? WHERE id = ?",
            ("2026-06-01T00:00:00", newer_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Script the verdict to supersede (newer_id survives per techne's direction logic).
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    report = await run_revision_pass(db_path, ScriptedLLM(verdict), policy="operator")

    # Both are operator-taught, so the candidate should promote.
    assert report.promoted == 1
    assert report.judged == 1

    # Exactly one row has superseded_by set (the survivor is the one techne chose).
    older_row = _knowledge_row(db_path, older_id)
    newer_row = _knowledge_row(db_path, newer_id)
    assert older_row["superseded_by"] == newer_id
    assert newer_row["superseded_by"] is None

    # Verify via candidate: target_ids are [retired_id, survivor_id] in order.
    [candidate] = [c for c in _candidate_rows(db_path) if c["action"] == "retire"]
    target_ids = json.loads(candidate["target_ids"])
    assert target_ids == [older_id, newer_id]  # survivor is the one techne's logic chose
    assert candidate["status"] == "promoted"


# ---------------------------------------------------------------------------
# The colleague branch -- quorum corroboration, and the operator shield.
#
# A fake colleagues client, wired into a REAL ColleaguesToolProvider (same
# idiom as tests/test_tools_colleagues.py's FakeHTTPClient): the directory
# GET and ask-colleague POST the pass corroborates through are exactly the
# ones the model's own colleagues.* tools would use.
# ---------------------------------------------------------------------------


class _FakeCorroborationResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _FakeColleagueClient:
    """Serves a fixed directory over GET; POST replies are scripted per
    colleague name (a list popped in call order), or raise an injected
    exception -- a genuine network failure, not the provider's own
    soft-degrade text -- when that name is in `raises`."""

    def __init__(self, agents, replies=None, raises=None):
        self.agents = agents
        self.replies = {name: list(texts) for name, texts in (replies or {}).items()}
        self.raises = dict(raises or {})
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict, dict]] = []

    async def get(self, url, headers=None):
        self.get_calls.append((url, headers or {}))
        return _FakeCorroborationResponse({"agents": self.agents})

    async def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append((url, json or {}, headers or {}))
        target = json["target"]
        if target in self.raises:
            raise self.raises[target]
        text = self.replies[target].pop(0)
        return _FakeCorroborationResponse(
            {"status": {"message": {"parts": [{"text": text}]}}}
        )


def _colleagues_provider(
    monkeypatch, agents, *, replies=None, raises=None, self_name="me"
) -> tuple[ColleaguesToolProvider, _FakeColleagueClient]:
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = _FakeColleagueClient(agents, replies=replies, raises=raises)
    provider = ColleaguesToolProvider(
        "http://switchboard.test",
        token_env="COLLEAGUE_TOKEN",
        self_name=self_name,
        client=client,
    )
    return provider, client


async def test_colleague_survivor_with_quorum_promotes_with_exact_decided_by(monkeypatch, tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    agents = [
        {"name": "me", "team": "billing"},
        {"name": "ana", "team": "billing"},
        {"name": "rin", "team": "billing"},
    ]
    provider, client = _colleagues_provider(
        monkeypatch, agents, replies={"rin": ["yes, that matches what I know"]}
    )

    report = await run_revision_pass(
        db_path, ScriptedLLM(verdict, "agree"), policy="operator", colleagues=provider
    )

    assert report.quorum_promoted == 1
    assert report.promoted == 0  # colleague branch, not spec A
    assert _knowledge_row(db_path, older_id)["superseded_by"] == newer_id
    [candidate] = [c for c in _candidate_rows(db_path) if c["action"] == "retire"]
    assert candidate["status"] == "promoted"
    assert candidate["decided_by"] == "quorum:2-billing"
    assert candidate["decision_reason"] == "corroborated by same-team specialists"
    assert client.post_calls  # someone was actually asked


async def test_single_same_team_reply_that_does_not_agree_stays_pending(monkeypatch, tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    agents = [{"name": "ana", "team": "billing"}, {"name": "rin", "team": "billing"}]
    provider, client = _colleagues_provider(
        monkeypatch, agents, replies={"rin": ["not sure, could be either building"]}
    )

    report = await run_revision_pass(
        db_path, ScriptedLLM(verdict, "unclear"), policy="operator", colleagues=provider
    )

    assert report.uncorroborated == 1
    assert report.quorum_promoted == 0
    assert report.disputed == 0
    assert _knowledge_row(db_path, older_id)["superseded_by"] is None
    [candidate] = _candidate_rows(db_path)
    assert candidate["status"] == "pending"
    assert candidate["decided_by"] is None


async def test_any_disagree_vetoes_promotion_even_with_quorum_worth_of_agrees(
    monkeypatch, tmp_path
):
    """Two agrees plus one disagree is >= QUORUM by count, but any disagree
    vetoes promotion this pass -- that is the brief's rule, pinned here."""
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    agents = [
        {"name": "ana", "team": "billing"},
        {"name": "rin", "team": "billing"},
        {"name": "zeb", "team": "billing"},
        {"name": "tom", "team": "billing"},
    ]
    provider, client = _colleagues_provider(
        monkeypatch,
        agents,
        replies={
            "rin": ["yes, that's right"],
            "zeb": ["yes, agreed"],
            "tom": ["no, that's wrong, it moved back to Building A"],
        },
    )

    report = await run_revision_pass(
        db_path,
        ScriptedLLM(verdict, "agree", "agree", "disagree"),
        policy="operator",
        colleagues=provider,
    )

    assert report.disputed == 1
    assert report.quorum_promoted == 0
    assert _knowledge_row(db_path, older_id)["superseded_by"] is None
    [candidate] = _candidate_rows(db_path)
    assert candidate["status"] == "pending"


async def test_no_same_team_colleagues_stays_pending_as_uncorroborable(monkeypatch, tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    agents = [{"name": "ana", "team": "billing"}, {"name": "someone-else", "team": "ops"}]
    provider, client = _colleagues_provider(monkeypatch, agents)

    report = await run_revision_pass(
        db_path, ScriptedLLM(verdict), policy="operator", colleagues=provider
    )

    assert report.uncorroborable == 1
    assert client.post_calls == []  # nobody was asked
    assert _knowledge_row(db_path, older_id)["superseded_by"] is None
    [candidate] = _candidate_rows(db_path)
    assert candidate["status"] == "pending"


async def test_colleague_source_without_team_stays_pending_as_uncorroborable(monkeypatch, tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    provider, client = _colleagues_provider(monkeypatch, agents=[])

    report = await run_revision_pass(
        db_path, ScriptedLLM(verdict), policy="operator", colleagues=provider
    )

    assert report.uncorroborable == 1
    assert client.get_calls == []  # no team to corroborate against; directory never fetched
    assert client.post_calls == []
    assert _knowledge_row(db_path, older_id)["superseded_by"] is None


async def test_shield_blocks_colleague_survivor_from_retiring_an_operator_taught_fact(
    monkeypatch, tmp_path
):
    """The shield precedes everything: an operator-taught retiree cannot be
    retired by a colleague-sourced survivor, even if quorum would otherwise
    succeed. No corroboration is even attempted."""
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path,
        older_source="intake:chat-shielded",
        newer_source="colleague:ana#team=billing",
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    agents = [
        {"name": "ana", "team": "billing"},
        {"name": "rin", "team": "billing"},
        {"name": "zeb", "team": "billing"},
    ]
    # Both would agree if asked -- quorum would otherwise succeed handily.
    provider, client = _colleagues_provider(
        monkeypatch, agents, replies={"rin": ["agreed"], "zeb": ["agreed"]}
    )

    report = await run_revision_pass(
        db_path,
        ScriptedLLM(verdict, "agree", "agree"),
        policy="operator",
        colleagues=provider,
    )

    assert report.shielded == 1
    assert report.quorum_promoted == 0
    assert report.promoted == 0
    assert client.get_calls == []  # shield short-circuits before any corroboration attempt
    assert client.post_calls == []
    assert _knowledge_row(db_path, older_id)["superseded_by"] is None
    [candidate] = _candidate_rows(db_path)
    assert candidate["status"] == "pending"
    assert candidate["decided_by"] is None


async def test_shield_does_not_block_an_operator_survivor_retiring_an_operator_fact(tmp_path):
    """Regression pin for the shield/spec-A interaction: the shield only
    protects an operator-taught fact from anyone OTHER than the operator.
    Two operator-taught facts in conflict (the dual-operator scenario) must
    still promote through the existing spec-A path, untouched -- the shield
    must never fire here."""
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="intake:chat-older", newer_source="intake:chat-newer"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    report = await run_revision_pass(db_path, ScriptedLLM(verdict), policy="operator")

    assert report.shielded == 0
    assert report.promoted == 1
    assert _knowledge_row(db_path, older_id)["superseded_by"] == newer_id


async def test_ask_bound_caps_at_three_even_with_four_same_team_colleagues(monkeypatch, tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    agents = [
        {"name": "ana", "team": "billing"},
        {"name": "rin", "team": "billing"},
        {"name": "zeb", "team": "billing"},
        {"name": "tom", "team": "billing"},
        {"name": "kai", "team": "billing"},
    ]
    provider, client = _colleagues_provider(
        monkeypatch,
        agents,
        replies={"rin": ["agreed"], "zeb": ["agreed"], "tom": ["agreed"]},
    )

    report = await run_revision_pass(
        db_path,
        ScriptedLLM(verdict, "agree", "agree", "agree"),
        policy="operator",
        colleagues=provider,
    )

    assert len(client.post_calls) == 3
    asked_names = {call[1]["target"] for call in client.post_calls}
    assert asked_names == {"rin", "zeb", "tom"}
    assert "kai" not in asked_names
    assert report.quorum_promoted == 1  # 1 (asserter) + 3 agrees = 4 >= QUORUM


async def test_directory_fetch_failure_during_corroboration_is_isolated_and_counted(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")

    class _BrokenGetClient:
        async def get(self, url, headers=None):
            raise RuntimeError("switchboard unreachable")

        async def post(self, url, json=None, headers=None, timeout=None):
            raise AssertionError("should never ask when the directory can't be fetched")

    provider = ColleaguesToolProvider(
        "http://switchboard.test",
        token_env="COLLEAGUE_TOKEN",
        self_name="me",
        client=_BrokenGetClient(),
    )

    report = await run_revision_pass(
        db_path, ScriptedLLM(verdict), policy="operator", colleagues=provider
    )

    assert report.errors == 1
    assert report.quorum_promoted == 0
    assert report.uncorroborable == 0
    assert _knowledge_row(db_path, older_id)["superseded_by"] is None


async def test_judge_failure_during_corroboration_is_isolated_and_counted(monkeypatch, tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    agents = [{"name": "ana", "team": "billing"}, {"name": "rin", "team": "billing"}]
    provider, client = _colleagues_provider(monkeypatch, agents, replies={"rin": ["yes"]})

    class _RaisesOnSecondCall:
        """Answers the conflict-check verdict fine; the colleague-reply judge
        call (the second .complete()) is where the model goes unreachable."""

        def __init__(self, first_reply: str):
            self.first_reply = first_reply
            self.calls = 0

        async def complete(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(text=self.first_reply, input_tokens=1, output_tokens=1)
            raise RuntimeError("judge model unreachable")

    report = await run_revision_pass(
        db_path, _RaisesOnSecondCall(verdict), policy="operator", colleagues=provider
    )

    assert report.errors == 1
    assert report.quorum_promoted == 0
    assert _knowledge_row(db_path, older_id)["superseded_by"] is None


async def test_second_run_is_idempotent_for_a_quorum_promoted_candidate(monkeypatch, tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    agents = [{"name": "ana", "team": "billing"}, {"name": "rin", "team": "billing"}]
    provider, client = _colleagues_provider(monkeypatch, agents, replies={"rin": ["agreed"]})

    first = await run_revision_pass(
        db_path, ScriptedLLM(verdict, "agree"), policy="operator", colleagues=provider
    )
    assert first.quorum_promoted == 1

    provider2, client2 = _colleagues_provider(monkeypatch, agents, replies={"rin": ["agreed"]})
    second = await run_revision_pass(
        db_path, ScriptedLLM(), policy="operator", colleagues=provider2
    )

    assert second == RevisionReport()
    assert len(_candidate_rows(db_path)) == 1  # no duplicate candidate from the re-run
    assert client2.get_calls == []  # nothing pending left to corroborate


async def test_events_emitted_in_order_for_quorum_promotion(monkeypatch, tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    agents = [{"name": "ana", "team": "billing"}, {"name": "rin", "team": "billing"}]
    provider, client = _colleagues_provider(monkeypatch, agents, replies={"rin": ["agreed"]})

    events: list[str] = []
    report = await run_revision_pass(
        db_path,
        ScriptedLLM(verdict, "agree"),
        policy="operator",
        colleagues=provider,
        emit=lambda t, **f: events.append(t),
    )

    assert events == [
        "revision_start",
        "revision_judged",
        "revision_corroborating",
        "revision_quorum",
        "revision_done",
    ]
    assert report.quorum_promoted == 1


async def test_revision_corroborating_and_quorum_events_carry_expected_fields(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    agents = [{"name": "ana", "team": "billing"}, {"name": "rin", "team": "billing"}]
    provider, client = _colleagues_provider(monkeypatch, agents, replies={"rin": ["agreed"]})

    events: list[dict] = []
    await run_revision_pass(
        db_path,
        ScriptedLLM(verdict, "agree"),
        policy="operator",
        colleagues=provider,
        emit=lambda t, **f: events.append({"type": t, **f}),
    )

    corroborating = next(e for e in events if e["type"] == "revision_corroborating")
    assert corroborating["asserter"] == "ana"
    assert corroborating["team"] == "billing"

    quorum = next(e for e in events if e["type"] == "revision_quorum")
    assert quorum["agrees"] == 2
    assert quorum["team"] == "billing"


async def test_revision_done_payload_includes_the_new_counters(monkeypatch, tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    agents = [{"name": "ana", "team": "billing"}, {"name": "rin", "team": "billing"}]
    provider, client = _colleagues_provider(monkeypatch, agents, replies={"rin": ["agreed"]})

    events: list[dict] = []
    await run_revision_pass(
        db_path,
        ScriptedLLM(verdict, "agree"),
        policy="operator",
        colleagues=provider,
        emit=lambda t, **f: events.append({"type": t, **f}),
    )

    done = next(e for e in events if e["type"] == "revision_done")
    assert done["quorum_promoted"] == 1
    assert set(done) >= {
        "judged",
        "staged",
        "promoted",
        "skipped",
        "errors",
        "shielded",
        "disputed",
        "uncorroborable",
        "uncorroborated",
        "quorum_promoted",
    }


# ---------------------------------------------------------------------------
# fetch_agents() token-gating (fix round): a colleagues provider with no
# token configured used to fire the directory GET anyway, sending
# "Authorization: Bearer None". fetch_agents() now raises
# ColleaguesNotConfigured before ever touching the network; the colleague
# branch does not catch it specially, so it propagates to the same
# per-candidate isolation every other corroboration failure here uses,
# landing in report.errors -- the sentinel-vs-error choice made explicit.
# ---------------------------------------------------------------------------


async def test_colleagues_without_a_token_during_corroboration_lands_in_errors(tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    class _NeverCalledClient:
        async def get(self, url, headers=None):
            raise AssertionError("fetch_agents must token-gate before any network call")

        async def post(self, url, json=None, headers=None, timeout=None):
            raise AssertionError("should never ask when no token is configured")

    provider = ColleaguesToolProvider(
        "http://switchboard.test",
        token_env=None,  # no token configured at all
        self_name="me",
        client=_NeverCalledClient(),
    )

    report = await run_revision_pass(
        db_path, ScriptedLLM(verdict), policy="operator", colleagues=provider
    )

    assert report.errors == 1
    assert report.quorum_promoted == 0
    assert report.uncorroborable == 0
    assert _knowledge_row(db_path, older_id)["superseded_by"] is None
    [candidate] = _candidate_rows(db_path)
    assert candidate["status"] == "pending"


# ---------------------------------------------------------------------------
# Ask-HTTP-failure path: _ask_colleague swallows a transport error into reply
# text ("Could not reach <target>: ..."), which the judge then classifies.
# REAL provider-over-fake-HTTP idiom (client.post raises) so the swallow
# actually happens, end to end -- not simulated by scripting the text.
# ---------------------------------------------------------------------------


async def test_ask_transport_failure_is_swallowed_and_lands_uncorroborated_not_errors(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    agents = [{"name": "ana", "team": "billing"}, {"name": "rin", "team": "billing"}]
    provider, client = _colleagues_provider(
        monkeypatch, agents, raises={"rin": RuntimeError("switchboard timeout")}
    )

    # The judge only ever sees the swallowed "Could not reach rin: ..." reply
    # text, never rin's actual words -- it reasonably calls that unclear.
    report = await run_revision_pass(
        db_path, ScriptedLLM(verdict, "unclear"), policy="operator", colleagues=provider
    )

    assert client.post_calls  # the ask was actually attempted
    assert report.uncorroborated == 1
    assert report.quorum_promoted == 0
    assert report.disputed == 0
    assert report.errors == 0  # NOT the error path -- _ask_colleague swallowed it
    assert _knowledge_row(db_path, older_id)["superseded_by"] is None
    [candidate] = _candidate_rows(db_path)
    assert candidate["status"] == "pending"


# ---------------------------------------------------------------------------
# colleagues=None for a colleague-sourced candidate: the controller-
# adjudicated behavior is report.errors, never a raised exception.
# ---------------------------------------------------------------------------


async def test_colleagues_none_for_colleague_sourced_candidate_lands_in_errors(tmp_path):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    report = await run_revision_pass(
        db_path, ScriptedLLM(verdict), policy="operator", colleagues=None
    )

    assert report.errors == 1
    assert report.quorum_promoted == 0
    assert report.uncorroborable == 0
    assert _knowledge_row(db_path, older_id)["superseded_by"] is None
    [candidate] = _candidate_rows(db_path)
    assert candidate["status"] == "pending"


# ---------------------------------------------------------------------------
# Pending candidates are genuinely re-examined across passes: techne's own
# anti-re-proposal guard (conflict_check_emit's _already_proposed) never
# re-emits a check_hash once ANY candidate row exists for it, so a colleague-
# sourced candidate left pending is not naturally revisited by the batch
# loop alone. Pass 1: a lone asserter, no same-team others -- uncorroborable,
# pending. Pass 2: a NEW same-team colleague now in the directory -- asked,
# agrees, PROMOTES with quorum:2-<team>.
# ---------------------------------------------------------------------------


async def test_pending_colleague_candidate_is_re_examined_and_can_promote_on_a_later_pass(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "techne.db"
    older_id, newer_id = _seed_contradiction(
        db_path, older_source="seed:onboarding", newer_source="colleague:ana#team=billing"
    )
    verdict = _supersede_verdict(newer_id, "The office is in Building C.")

    # Pass 1: only the asserter is in the directory. Nobody else to ask.
    lone_agents = [{"name": "ana", "team": "billing"}]
    provider1, client1 = _colleagues_provider(monkeypatch, lone_agents)

    first = await run_revision_pass(
        db_path, ScriptedLLM(verdict), policy="operator", colleagues=provider1
    )

    assert first.uncorroborable == 1
    assert first.quorum_promoted == 0
    assert client1.post_calls == []  # nobody to ask yet
    [candidate] = _candidate_rows(db_path)
    assert candidate["status"] == "pending"

    # Pass 2: the directory grew a same-team colleague. techne never
    # re-emits this already-proposed check_hash (nothing freshly staged),
    # but the standing pending candidate is re-routed through corroboration
    # with today's directory.
    grown_agents = [{"name": "ana", "team": "billing"}, {"name": "rin", "team": "billing"}]
    provider2, client2 = _colleagues_provider(
        monkeypatch, grown_agents, replies={"rin": ["yes, that matches what I know"]}
    )

    second = await run_revision_pass(
        db_path, ScriptedLLM("agree"), policy="operator", colleagues=provider2
    )

    assert second.staged == 0  # not freshly staged this pass
    assert second.quorum_promoted == 1
    assert client2.post_calls  # rin was actually asked this time
    assert _knowledge_row(db_path, older_id)["superseded_by"] == newer_id
    [candidate] = _candidate_rows(db_path)
    assert candidate["status"] == "promoted"
    assert candidate["decided_by"] == "quorum:2-billing"


# ---------------------------------------------------------------------------
# Agent wiring: _colleagues_for_revision -- reuse the wired provider, else
# construct one from the same bundle spec, else None.
# ---------------------------------------------------------------------------


def test_colleagues_for_revision_reuses_the_agents_wired_provider(tmp_path):
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "tools:\n  provider: colleagues\n  colleagues:\n"
        "    url: http://switchboard.test\n"
        "    token_env: CABAL_COLLEAGUE_TOKEN\n"
        "    self: x\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")

    agent = Agent.load(tmp_path, llm=ScriptedLLM())

    assert isinstance(agent.tools, ColleaguesToolProvider)
    assert agent._colleagues_for_revision() is agent.tools


def test_colleagues_for_revision_constructs_from_spec_when_no_provider_is_wired(tmp_path):
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "tools:\n  colleagues:\n"
        "    url: http://switchboard.test\n"
        "    token_env: CABAL_COLLEAGUE_TOKEN\n"
        "    self: x\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")

    agent = Agent.load(tmp_path, llm=ScriptedLLM())
    assert not isinstance(agent.tools, ColleaguesToolProvider)  # tools.provider defaults to none

    colleagues = agent._colleagues_for_revision()

    assert isinstance(colleagues, ColleaguesToolProvider)
    assert colleagues.url == "http://switchboard.test"
    assert colleagues.self_name == "x"


def test_colleagues_for_revision_is_none_when_nothing_configured(tmp_path):
    root = _agent_bundle(tmp_path)
    agent = Agent.load(root, llm=ScriptedLLM())

    assert agent._colleagues_for_revision() is None


async def test_start_revision_pass_threads_the_colleagues_provider_through(tmp_path, monkeypatch):
    root = _agent_bundle(
        tmp_path,
        f"memory:\n  personal: techne\n  home: {tmp_path}\n  revision: operator\n"
        "tools:\n  provider: colleagues\n  colleagues:\n"
        "    url: http://switchboard.test\n"
        "    token_env: CABAL_COLLEAGUE_TOKEN\n"
        "    self: x\n",
    )
    agent = Agent.load(root, llm=ScriptedLLM())

    calls = []

    async def fake_run_revision_pass(db_path, model, *, policy, emit, colleagues=None):
        calls.append(colleagues)
        return RevisionReport()

    monkeypatch.setattr("daedalus.memory.revision.run_revision_pass", fake_run_revision_pass)

    task = agent._start_revision_pass()
    assert task is not None
    await task

    assert calls == [agent.tools]
