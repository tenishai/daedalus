"""RED-first tests for the post-boot belief revision pass.

techne's contradiction machinery (conflict_check_emit/ingest, promote_candidate)
is exercised against a REAL temp techne db throughout -- no fakes for techne
itself, only for the model (ScriptedLLM, already used across the suite).
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import sys
import warnings
from pathlib import Path
from unittest.mock import patch

from daedalus.agent import Agent
from daedalus.bundle import MemorySpec
from daedalus.memory.revision import RevisionReport, run_revision_pass
from daedalus.memory.techne import TechneMemoryStore
from tests.fakes import ScriptedLLM

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
