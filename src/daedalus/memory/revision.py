"""Post-boot belief revision: the agent's own model judges staged contradictions.

techne HAS the whole contradiction pipeline (conflict_check_emit/ingest stage
'retire' candidates against pending live facts; promote_candidate applies a
pending candidate) but by deliberate design nothing calls it automatically -
an operator reviews every candidate by hand. This module is the first
automated caller, and it stays narrow on purpose: it judges every staged
contradiction with the agent's own resident model, but it only PROMOTES the
slice an operator explicitly taught (a fact whose source starts with
"intake:chat-"). Everything else lands exactly where techne already put it:
pending, for a human.

Policy v1 ("operator"): after a check comes back SUPERSEDE or UPDATE, techne
stages a 'retire' candidate whose target_ids are [retired_id, survivor_id].
If the surviving fact's source starts with "intake:chat-", the candidate is
promoted with decided_by="operator" and decision_reason="taught correction".
Any other source stays pending.

Every failure mode (a down model, a locked db, a malformed verdict) is caught
and counted here; run_revision_pass never raises. It is meant to run as a
detached background task after boot, where nothing is listening for an
exception.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: The only revision policy implemented today. Anything else is a
#: misconfiguration the wiring layer should already have disabled; this
#: module treats it as just another swallowed error.
OPERATOR_POLICY = "operator"
KNOWN_POLICIES = (OPERATOR_POLICY,)

_TAUGHT_SOURCE_PREFIX = "intake:chat-"
_DECIDED_BY = "operator"
_DECISION_REASON = "taught correction"


class ModelCall(Protocol):
    """The same seam Agent's task loop calls: messages in, LLMResponse out."""

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> Any: ...


@dataclass
class RevisionReport:
    """Counts for one revision pass. Every field is a plain count so a
    caller (a CLI summary, a test) never has to know the internals."""

    judged: int = 0
    staged: int = 0
    promoted: int = 0
    skipped: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "judged": self.judged,
            "staged": self.staged,
            "promoted": self.promoted,
            "skipped": self.skipped,
            "errors": self.errors,
        }


def _noop_emit(event_type: str, **fields) -> None:
    return None


def _staged_pending_retire(conn, check_hash: str) -> list[dict]:
    """Candidate rows THIS check_hash just staged: pending, action='retire',
    origin='conflict'. Scoped by check_hash so a second run (whose emit
    already skipped the hash as already-proposed) never re-touches it."""
    rows = conn.execute(
        """SELECT id, target_ids FROM knowledge_candidates
             WHERE prompt_hash = ? AND origin = 'conflict'
               AND status = 'pending' AND action = 'retire'""",
        (check_hash,),
    ).fetchall()
    return [dict(r) for r in rows]


def _survivor_id(target_ids_json: str | None) -> int | None:
    """A retire candidate's target_ids are [retired_id, survivor_id]
    (order-significant; techne's own applier depends on it)."""
    try:
        ids = json.loads(target_ids_json or "[]")
    except (TypeError, ValueError):
        return None
    if len(ids) != 2:
        return None
    return ids[1]


def _knowledge_source(conn, knowledge_id: int) -> str | None:
    row = conn.execute(
        "SELECT source FROM knowledge WHERE id = ?", (knowledge_id,)
    ).fetchone()
    return row["source"] if row else None


async def run_revision_pass(
    db_path: str | Path,
    model: ModelCall,
    *,
    policy: str = OPERATOR_POLICY,
    emit: Callable[..., None] | None = None,
) -> RevisionReport:
    """Judge every staged contradiction, promote only what the operator taught.

    db_path: the agent's techne.db.
    model: the SAME completion seam Agent's task loop calls (an LLM with
        .complete(messages, tools=None) -> response.text). No tools are
        offered; the judge answers a plain prompt with a verdict.
    policy: the only supported value is "operator" (see module docstring).
        Anything else is treated as a misconfiguration and counted as an
        error without touching the db.
    emit: called as emit(event_type, **fields), matching Agent._emit's
        signature. Defaults to a no-op so this function is usable standalone.

    Never raises: every failure (bad policy, an unreachable model, a locked
    db, a malformed verdict) is caught, logged, and folded into the report.
    """
    emit = emit or _noop_emit
    report = RevisionReport()
    emit("revision_start")

    try:
        if policy not in KNOWN_POLICIES:
            log.error("revision: unknown policy %r; skipping the pass", policy)
            report.errors += 1
            return report

        from techne.config import Config
        from techne.core.conflict import conflict_check_emit, conflict_check_ingest
        from techne.core.database import connect
        from techne.core.reflect import promote_candidate

        config = Config(db_path=Path(db_path))

        try:
            batch = conflict_check_emit(config)
        except Exception:
            log.exception("revision: conflict_check_emit failed")
            report.errors += 1
            return report

        for item in batch:
            check_hash = item["check_hash"]
            try:
                response = await model.complete(
                    [{"role": "user", "content": item["prompt"]}], tools=None
                )
                verdict_text = getattr(response, "text", None)
                if verdict_text is None:
                    verdict_text = str(response)

                counts = conflict_check_ingest(
                    config, item["focus_id"], item["comparison_ids"], check_hash, verdict_text
                )
                report.errors += counts.get("errors", 0)
                report.skipped += counts.get("skipped", 0)

                conn = connect(Path(db_path))
                try:
                    staged = _staged_pending_retire(conn, check_hash)
                    decisions = []
                    for row in staged:
                        report.staged += 1
                        survivor_id = _survivor_id(row["target_ids"])
                        source = (
                            _knowledge_source(conn, survivor_id)
                            if survivor_id is not None
                            else None
                        )
                        decisions.append((row["id"], survivor_id, source))
                finally:
                    conn.close()

                report.judged += 1
                emit(
                    "revision_judged",
                    check_hash=check_hash,
                    focus_id=item["focus_id"],
                    comparison_ids=list(item["comparison_ids"]),
                    counts=dict(counts),
                )

                for candidate_id, survivor_id, source in decisions:
                    taught = bool(source) and source.startswith(_TAUGHT_SOURCE_PREFIX)
                    if not taught:
                        report.skipped += 1
                        continue
                    try:
                        promote_candidate(
                            Path(db_path),
                            candidate_id,
                            decided_by=_DECIDED_BY,
                            decision_reason=_DECISION_REASON,
                        )
                        report.promoted += 1
                        emit(
                            "revision_promoted",
                            candidate_id=candidate_id,
                            survivor_id=survivor_id,
                            source=source,
                        )
                    except Exception:
                        log.exception(
                            "revision: promote_candidate failed for candidate %s", candidate_id
                        )
                        report.errors += 1
            except Exception:
                log.exception("revision: check failed (check_hash=%s)", check_hash)
                report.errors += 1
    except Exception:
        log.exception("revision: pass failed")
        report.errors += 1
    finally:
        emit("revision_done", **report.as_dict())

    return report
