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

The colleague branch (belief-revision arc, colleague quorum): a survivor
fact sourced "colleague:<asserter>#team=<team>" (colleagues.remember's own
provenance format) can also promote, but only once other specialists on the
SAME team corroborate it. The asserter counts as one agree; up to
MAX_VERIFICATION_ASKS same-team colleagues (excluding the asserter and this
agent) are asked to verify the fact, and the resident model judges each
reply as agree/disagree/unclear. QUORUM total agrees promotes
(decided_by="quorum:{agrees}-{team}"); any disagree vetoes the promotion for
this pass; too few voices leaves it pending. A retiree fact the operator
taught (source "intake:chat-") is shielded from this branch: nobody but
another operator-taught correction may retire it (spec A's dual-operator
case is unaffected -- that is still operator vs operator, not the operator
outvoted by colleagues).

"Pending" here is genuinely provisional, not a dead end: techne's own
anti-re-proposal guard never re-emits an already-proposed check_hash, so a
colleague-sourced candidate left pending would otherwise sit forever even
after the switchboard directory grows a corroborating colleague. Every
call to run_revision_pass therefore also re-routes every OTHER standing
pending 'retire' candidate (not just what this call's own conflict check
just staged) through the same routing, using today's directory and
today's judge -- a candidate that could not corroborate on an earlier
pass can still graduate on a later one.

Every failure mode (a down model, a locked db, a malformed verdict, an
unreachable colleague directory) is caught and counted here;
run_revision_pass never raises. It is meant to run as a detached background
task after boot, where nothing is listening for an exception.
"""

from __future__ import annotations

import json
import logging
import time
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

#: The colleague branch's own provenance prefix (colleagues.remember writes
#: "colleague:<asserter>" or "colleague:<asserter>#team=<team>").
_COLLEAGUE_SOURCE_PREFIX = "colleague:"
_QUORUM_DECISION_REASON = "corroborated by same-team specialists"

#: Total agrees needed to promote a colleague-sourced candidate. The
#: asserter counts as one; QUORUM - 1 more corroborating colleagues close it.
QUORUM = 2

#: Hard cap on how many same-team colleagues get asked to verify a single
#: candidate in a single pass -- bounded fanout, not an exhaustive canvass.
MAX_VERIFICATION_ASKS = 3

#: How long a candidate's colleague asks stay satisfied before the standing-
#: pending rescan may ask again. Revision fires per teach, and without this
#: window one stuck candidate re-verified the same fact with the same
#: colleagues on every teach -- terminal noise and model spend, no new
#: information. In-process only (the dict lives on the Agent): a restarted
#: pod re-asks once, which is the acceptable end of this trade.
COLLEAGUE_REASK_COOLDOWN_S = 24 * 60 * 60.0


def _now() -> float:
    """Monotonic clock, module-level so tests can monkeypatch it."""
    return time.monotonic()


class ModelCall(Protocol):
    """The same seam Agent's task loop calls: messages in, LLMResponse out."""

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> Any: ...


class ColleaguesLike(Protocol):
    """The slice of ColleaguesToolProvider the colleague branch reuses:
    the same directory fetch and the same tool-call ask path the provider
    itself offers the model, so corroboration goes over the identical
    client/config (see daedalus.tools.colleagues.ColleaguesToolProvider).

    fetch_agents() is public and token-gated: it raises
    ColleaguesNotConfigured (daedalus.tools.colleagues) rather than firing
    the directory GET with no token. This branch does not catch that
    exception itself -- like every other corroboration failure here, it
    propagates to the caller's per-candidate isolation and counts as an
    error (see _run_colleague_branch's docstring)."""

    self_name: str | None

    async def fetch_agents(self) -> tuple[list[dict] | None, str | None]: ...

    async def call(self, name: str, payload: dict) -> str: ...


@dataclass
class RevisionReport:
    """Counts for one revision pass. Every field is a plain count so a
    caller (a CLI summary, a test) never has to know the internals."""

    judged: int = 0
    staged: int = 0
    promoted: int = 0
    skipped: int = 0
    errors: int = 0
    # Colleague branch (quorum corroboration) and the operator shield.
    shielded: int = 0
    disputed: int = 0
    uncorroborable: int = 0
    uncorroborated: int = 0
    quorum_promoted: int = 0
    cooled: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "judged": self.judged,
            "staged": self.staged,
            "promoted": self.promoted,
            "skipped": self.skipped,
            "errors": self.errors,
            "shielded": self.shielded,
            "disputed": self.disputed,
            "uncorroborable": self.uncorroborable,
            "uncorroborated": self.uncorroborated,
            "quorum_promoted": self.quorum_promoted,
            "cooled": self.cooled,
        }


def _noop_emit(event_type: str, **fields) -> None:
    return None


def _safe_emit(emit: Callable[..., None], event_type: str, **fields) -> None:
    """Call emit with exception handling. If the callback raises, log it and
    return normally so the pass is never interrupted by a broken callback."""
    try:
        emit(event_type, **fields)
    except Exception:
        log.exception("revision: emit callback raised for event %r", event_type)


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


def _target_id_pair(target_ids_json: str | None) -> tuple[int, int] | None:
    """A retire candidate's target_ids are [retired_id, survivor_id]
    (order-significant; techne's own applier depends on it)."""
    try:
        ids = json.loads(target_ids_json or "[]")
    except (TypeError, ValueError):
        return None
    if len(ids) != 2:
        return None
    return ids[0], ids[1]


def _knowledge_fields(conn, knowledge_id: int | None) -> tuple[str | None, str | None]:
    """(source, content) for a knowledge row, or (None, None) if absent."""
    if knowledge_id is None:
        return None, None
    row = conn.execute(
        "SELECT source, content FROM knowledge WHERE id = ?", (knowledge_id,)
    ).fetchone()
    if row is None:
        return None, None
    return row["source"], row["content"]


def _parse_colleague_source(source: str) -> tuple[str, str | None]:
    """Parse colleagues.remember's own provenance format: 'colleague:<asserter>'
    or 'colleague:<asserter>#team=<team>'. Returns the asserter's name and
    their team (None when the source carries no team -- either the plain
    form, or an empty #team= suffix)."""
    rest = source[len(_COLLEAGUE_SOURCE_PREFIX) :]
    asserter, _sep, team = rest.partition("#team=")
    return asserter, (team or None)


def _verification_question(fact: str) -> str:
    return (
        f'A colleague asserted this fact: "{fact}" '
        "Do you agree or disagree, and why? Give one reason."
    )


def _judge_reply_prompt(fact: str, question: str, reply: str) -> str:
    return (
        f'A colleague was asked to verify this fact: "{fact}"\n'
        f'The question put to them was: "{question}"\n'
        f'Their reply was: "{reply}"\n\n'
        "Does their reply indicate agreement, disagreement, or is it unclear? "
        "Answer with exactly one word: agree, disagree, or unclear."
    )


def _parse_agreement(text: str) -> str:
    lowered = (text or "").strip().lower()
    if "disagree" in lowered:  # checked first: "disagree" contains "agree"
        return "disagree"
    if "agree" in lowered:
        return "agree"
    return "unclear"


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    return text if text is not None else str(response)


async def _run_colleague_branch(
    db_path: Path,
    model: ModelCall,
    colleagues: ColleaguesLike | None,
    emit: Callable[..., None],
    report: RevisionReport,
    *,
    candidate_id: int,
    fact: str,
    asserter: str,
    team: str,
    reask_cooldowns: dict[int, float] | None = None,
) -> None:
    """The colleague corroboration branch for one pending retire candidate
    whose survivor was sourced "colleague:<asserter>#team=<team>".

    Any exception here (a down directory, an unreachable colleague, a dead
    judge model) is left to propagate to the caller, which isolates it per
    candidate and counts it as an error -- this function itself never
    catches, so a caller that doesn't wrap it does not get the "never
    raises" promise, by design (see run_revision_pass's per-candidate try).

    reask_cooldowns: per-candidate ask stamps (see COLLEAGUE_REASK_COOLDOWN_S).
    The gate is checked FIRST -- a cooling candidate skips even the directory
    fetch -- but the stamp lands only when an ask round COMPLETES: an
    uncorroborable candidate (nobody to ask, no cost) keeps its per-pass
    chance to catch a newly-arrived corroborator, and a round that dies
    mid-loop (a judge model going unreachable) leaves the candidate
    unstamped and eligible on the very next pass, matching the caller's
    per-candidate error isolation. A quorum promotion evicts the stamp --
    the candidate leaves 'pending' forever, so the entry would only sit in
    the dict for the process life.
    """
    if reask_cooldowns is not None:
        last = reask_cooldowns.get(candidate_id)
        if last is not None and (_now() - last) < COLLEAGUE_REASK_COOLDOWN_S:
            report.cooled += 1
            _safe_emit(
                emit, "revision_cooled", candidate_id=candidate_id, asserter=asserter, team=team
            )
            return

    _safe_emit(
        emit, "revision_corroborating", candidate_id=candidate_id, asserter=asserter, team=team
    )

    if colleagues is None:
        raise RuntimeError("no colleagues provider is reachable to corroborate against")

    agents, error = await colleagues.fetch_agents()
    if error is not None:
        raise RuntimeError(error)

    self_name = colleagues.self_name
    same_team = [
        agent
        for agent in (agents or [])
        if isinstance(agent, dict)
        and agent.get("team") == team
        and agent.get("name") not in (asserter, self_name)
    ]
    if not same_team:
        report.uncorroborable += 1
        return

    asked = same_team[:MAX_VERIFICATION_ASKS]
    question = _verification_question(fact)

    agrees = 1  # the asserter counts as one agree
    disagreed = False
    for colleague in asked:
        name = colleague.get("name", "")
        reply = await colleagues.call(
            "colleagues.ask_colleague", {"name": name, "question": question}
        )
        judged = await model.complete(
            [{"role": "user", "content": _judge_reply_prompt(fact, question, reply)}], tools=None
        )
        verdict = _parse_agreement(_response_text(judged))
        if verdict == "disagree":
            disagreed = True
        elif verdict == "agree":
            agrees += 1

    # The round COMPLETED (every reply judged, nothing raised): stamp per
    # outcome. Stamping before the loop parked candidates for a whole window
    # on rounds a dying judge never finished (reviewer-proven), and stamping
    # before promote_candidate() did the same for an earned promotion whose
    # db write failed (the re-review's follow-up) -- so the non-terminal
    # outcomes stamp, and the terminal one never stamps at all (it only
    # evicts whatever expired stamp an earlier round may have left).
    if disagreed:
        if reask_cooldowns is not None:
            reask_cooldowns[candidate_id] = _now()
        report.disputed += 1
        _safe_emit(emit, "revision_quorum", candidate_id=candidate_id, agrees=agrees, team=team)
        return

    if agrees >= QUORUM:
        from techne.core.reflect import promote_candidate

        promote_candidate(
            db_path,
            candidate_id,
            decided_by=f"quorum:{agrees}-{team}",
            decision_reason=_QUORUM_DECISION_REASON,
        )
        report.quorum_promoted += 1
        if reask_cooldowns is not None:
            reask_cooldowns.pop(candidate_id, None)  # terminal: never routed again
        _safe_emit(emit, "revision_quorum", candidate_id=candidate_id, agrees=agrees, team=team)
    else:
        if reask_cooldowns is not None:
            reask_cooldowns[candidate_id] = _now()
        report.uncorroborated += 1
        _safe_emit(emit, "revision_quorum", candidate_id=candidate_id, agrees=agrees, team=team)


async def _route_pending_retire_candidate(
    db_path: Path,
    model: ModelCall,
    colleagues: ColleaguesLike | None,
    emit: Callable[..., None],
    report: RevisionReport,
    *,
    candidate_id: int,
    survivor_id: int | None,
    survivor_source: str | None,
    survivor_content: str | None,
    retiree_source: str | None,
    reask_cooldowns: dict[int, float] | None = None,
) -> None:
    """Route one pending 'retire' candidate to its outcome: the operator
    shield, spec-A promotion (survivor is operator-taught), the colleague
    quorum branch (survivor is colleague-sourced), or a plain skip.

    Shared by two callers in run_revision_pass: the freshly-staged path
    (candidates this pass's conflict_check_emit/ingest just staged) and the
    standing-pending rescan (candidates an EARLIER pass left pending,
    re-routed here in case the switchboard directory has since grown a
    corroborating colleague). Both callers isolate exceptions per candidate
    and count them as errors; this function itself does not catch, mirroring
    _run_colleague_branch (which it may call), so the caller's isolation
    boundary does the catching.
    """
    from techne.core.reflect import promote_candidate

    retiree_taught = bool(retiree_source) and retiree_source.startswith(_TAUGHT_SOURCE_PREFIX)
    survivor_taught = bool(survivor_source) and survivor_source.startswith(_TAUGHT_SOURCE_PREFIX)

    # SHIELD, checked first: an operator-taught retiree can only be retired
    # by another operator-taught fact (spec A, unchanged below). Anyone
    # else -- a colleague, or any other source -- is shielded from retiring
    # it in this branch, no matter how well corroborated.
    if retiree_taught and not survivor_taught:
        report.shielded += 1
        return

    if survivor_taught:
        # Spec A, unchanged: the operator's own correction promotes on the
        # model's verdict alone.
        promote_candidate(
            db_path,
            candidate_id,
            decided_by=_DECIDED_BY,
            decision_reason=_DECISION_REASON,
        )
        report.promoted += 1
        _safe_emit(
            emit,
            "revision_promoted",
            candidate_id=candidate_id,
            survivor_id=survivor_id,
            source=survivor_source,
        )
        return

    if survivor_source and survivor_source.startswith(_COLLEAGUE_SOURCE_PREFIX):
        asserter, team = _parse_colleague_source(survivor_source)
        if not team:
            report.uncorroborable += 1
            return
        await _run_colleague_branch(
            db_path,
            model,
            colleagues,
            emit,
            report,
            candidate_id=candidate_id,
            fact=survivor_content or "",
            asserter=asserter,
            team=team,
            reask_cooldowns=reask_cooldowns,
        )
        return

    report.skipped += 1


def _standing_pending_retire_candidates(conn, exclude_ids: set[int]) -> list[dict]:
    """Every OTHER pending 'retire' candidate in the db -- not scoped to any
    one check_hash, unlike _staged_pending_retire. techne's own anti-re-
    proposal guard (conflict_check_emit's _already_proposed) never re-emits
    a check_hash once ANY candidate row exists for it, pending or not, so a
    colleague-sourced candidate left pending by an earlier pass (uncorrob-
    orable, disputed, or short of quorum) would otherwise never surface in
    a later pass's `batch` at all. This is the query that lets run_revision_
    pass re-examine those standing candidates anyway, using today's
    directory and today's judge. exclude_ids removes candidates this SAME
    pass already routed via the freshly-staged path, so none is routed
    twice in one call."""
    rows = conn.execute(
        """SELECT id, target_ids FROM knowledge_candidates
             WHERE origin = 'conflict' AND status = 'pending' AND action = 'retire'"""
    ).fetchall()
    return [dict(r) for r in rows if r["id"] not in exclude_ids]


async def run_revision_pass(
    db_path: str | Path,
    model: ModelCall,
    *,
    policy: str = OPERATOR_POLICY,
    emit: Callable[..., None] | None = None,
    colleagues: ColleaguesLike | None = None,
    reask_cooldowns: dict[int, float] | None = None,
) -> RevisionReport:
    """Judge every staged contradiction, promote what the operator taught or
    what a same-team quorum of colleagues corroborates.

    db_path: the agent's techne.db.
    model: the SAME completion seam Agent's task loop calls (an LLM with
        .complete(messages, tools=None) -> response.text). No tools are
        offered; the judge answers a plain prompt with a verdict.
    policy: the only supported value is "operator" (see module docstring).
        Anything else is treated as a misconfiguration and counted as an
        error without touching the db.
    emit: called as emit(event_type, **fields), matching Agent._emit's
        signature. Defaults to a no-op so this function is usable standalone.
    colleagues: the client the colleague branch corroborates through (same
        directory fetch + ask-colleague call the ColleaguesToolProvider
        offers the model). None disables the colleague branch -- a
        colleague-sourced candidate then fails corroboration (isolated,
        counted as an error) rather than being silently skipped, since a
        bundle with colleague-sourced facts staged is expected to have one.
    reask_cooldowns: per-candidate ask stamps, or None (the default) to
        re-ask every pass, the pre-cooldown behavior. Pass the SAME dict on
        every call (the Agent owns one for its process life): a candidate
        whose colleague ask round completed within COLLEAGUE_REASK_COOLDOWN_S
        is counted as report.cooled and skipped -- no directory fetch, no
        asks -- instead of re-verifying the same fact on every teach. Rounds
        that fail mid-loop never stamp; quorum promotions evict their stamp.

    Never raises: every failure (bad policy, an unreachable model, a locked
    db, a malformed verdict, an unreachable colleague or directory) is
    caught, logged, and folded into the report.
    """
    emit = emit or _noop_emit
    report = RevisionReport()
    _safe_emit(emit, "revision_start")

    try:
        if policy not in KNOWN_POLICIES:
            log.error("revision: unknown policy %r; skipping the pass", policy)
            report.errors += 1
            return report

        from techne.config import Config
        from techne.core.conflict import conflict_check_emit, conflict_check_ingest
        from techne.core.database import connect

        config = Config(db_path=Path(db_path))

        try:
            batch = conflict_check_emit(config)
        except Exception:
            log.exception("revision: conflict_check_emit failed")
            report.errors += 1
            return report

        # Candidate ids routed this pass via the freshly-staged path below --
        # excluded from the standing-pending rescan afterward so none is
        # routed twice in the same call.
        handled_ids: set[int] = set()

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
                        ids = _target_id_pair(row["target_ids"])
                        retiree_id, survivor_id = ids if ids is not None else (None, None)
                        survivor_source, survivor_content = _knowledge_fields(conn, survivor_id)
                        retiree_source, _retiree_content = _knowledge_fields(conn, retiree_id)
                        decisions.append(
                            {
                                "candidate_id": row["id"],
                                "survivor_id": survivor_id,
                                "survivor_source": survivor_source,
                                "survivor_content": survivor_content,
                                "retiree_source": retiree_source,
                            }
                        )
                finally:
                    conn.close()

                report.judged += 1
                _safe_emit(
                    emit,
                    "revision_judged",
                    check_hash=check_hash,
                    focus_id=item["focus_id"],
                    comparison_ids=list(item["comparison_ids"]),
                    counts=dict(counts),
                )

                for decision in decisions:
                    candidate_id = decision["candidate_id"]
                    handled_ids.add(candidate_id)
                    try:
                        await _route_pending_retire_candidate(
                            Path(db_path),
                            model,
                            colleagues,
                            emit,
                            report,
                            candidate_id=candidate_id,
                            survivor_id=decision["survivor_id"],
                            survivor_source=decision["survivor_source"],
                            survivor_content=decision["survivor_content"],
                            retiree_source=decision["retiree_source"],
                            reask_cooldowns=reask_cooldowns,
                        )
                    except Exception:
                        log.exception(
                            "revision: candidate decision failed (candidate_id=%s)", candidate_id
                        )
                        report.errors += 1
            except Exception:
                log.exception("revision: check failed (check_hash=%s)", check_hash)
                report.errors += 1

        # Re-examine every standing pending 'retire' candidate NOT already
        # handled above (see _standing_pending_retire_candidates): a
        # colleague-sourced candidate an earlier pass left pending
        # (uncorroborable, disputed, or short of quorum) is retried here
        # against today's directory, so it can still graduate once the
        # switchboard grows a same-team colleague able to corroborate it.
        # Not counted in report.staged -- these were staged in an earlier
        # pass, not this one.
        conn = connect(Path(db_path))
        try:
            standing_rows = _standing_pending_retire_candidates(conn, handled_ids)
            standing = []
            for row in standing_rows:
                ids = _target_id_pair(row["target_ids"])
                retiree_id, survivor_id = ids if ids is not None else (None, None)
                survivor_source, survivor_content = _knowledge_fields(conn, survivor_id)
                retiree_source, _retiree_content = _knowledge_fields(conn, retiree_id)
                standing.append(
                    {
                        "candidate_id": row["id"],
                        "survivor_id": survivor_id,
                        "survivor_source": survivor_source,
                        "survivor_content": survivor_content,
                        "retiree_source": retiree_source,
                    }
                )
        finally:
            conn.close()

        for decision in standing:
            candidate_id = decision["candidate_id"]
            try:
                await _route_pending_retire_candidate(
                    Path(db_path),
                    model,
                    colleagues,
                    emit,
                    report,
                    candidate_id=candidate_id,
                    survivor_id=decision["survivor_id"],
                    survivor_source=decision["survivor_source"],
                    survivor_content=decision["survivor_content"],
                    retiree_source=decision["retiree_source"],
                    reask_cooldowns=reask_cooldowns,
                )
            except Exception:
                log.exception(
                    "revision: standing candidate re-check failed (candidate_id=%s)", candidate_id
                )
                report.errors += 1
    except Exception:
        log.exception("revision: pass failed")
        report.errors += 1
    finally:
        _safe_emit(emit, "revision_done", **report.as_dict())

    return report
