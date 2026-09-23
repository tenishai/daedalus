"""colleagues-backed tool provider — a deployed agent finds and asks the
colleagues listed in a switchboard directory, over A2A.

Mirrors liminal.py's token_env soft-degrade idiom: an absent or empty
bearer env var never fails at boot, only politely at call time. HTTP
failures, malformed replies, and self-referential asks all become plain
tool-result text — the provider never raises out to the agent loop.
"""

from __future__ import annotations

import contextvars
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

_NOT_CONFIGURED = "The colleague pass is not configured."
_UNREADABLE_REPLY = "The colleague answered in a shape I could not read."
#: Task states that mean the question was refused rather than answered.
#: A guardrail refusal is one of these carrying explanatory text, which is
#: exactly what makes it readable as an answer if nobody checks (#40 r2).
_UNANSWERED_STATES = frozenset({"failed", "rejected", "canceled"})
_NO_MEMORY = "I have no memory store configured, so I cannot remember that."

# A colleague whose heartbeat is older than this is treated as unreachable
# (daedalus#38). 120s is a CHOSEN threshold, and calling it four missed beats
# against cabal's 30s HeartbeatScheduler -- as this comment did -- contradicts
# the paragraph below, which establishes that whatever serves /directory is not
# cabal. 30s is simply the only beat interval anyone has measured anywhere, so
# it is the reference we have and not the one that applies.
#
# THE RISK THAT CREATES is the same blackout as the unreadable-heartbeat case
# below: if the real directory's interval exceeds 120s, every colleague reads
# as permanently stale and the whole fleet is hidden at once. Pinned by
# test_the_staleness_threshold_is_two_minutes so that changing it is a decision
# rather than a drift -- it was unpinned, and a tenfold change left the suite
# green (#38 r1 Nit 4).
#
# WHETHER THE LIVE DIRECTORY PUBLISHES A HEARTBEAT AT ALL IS UNVERIFIED, and
# an earlier version of this comment asserted it did. What is established:
# cabal's Agent model carries status/last_heartbeat/kind/generation, and kobo
# registers agents into cabal via `cabal.graduate`. What is NOT established:
# that the `GET /directory` this module calls is served by anything in the
# tenishai tree at all -- a sweep of every repo found no such route, and cabal
# never emits the `team`/`purpose_tags`/`capability` fields `_format_colleague`
# already reads, so whatever answers /directory is not cabal's admin listing.
#
# THAT IS WHY THE FILTER IS CONDITIONAL rather than a courtesy: if NO agent
# carries a heartbeat the filter does not engage at all, and this degrades to
# today's behaviour exactly. The runtime down-memory below does not depend on
# the premise either way.
#
# WHAT THAT DOES NOT COVER, because an earlier version of this said the
# unverified premise "costs nothing" and that is broader than the code earns
# (#38 pre-pass, r2). There is a third shape between "no heartbeat field" and
# "a real one": a field named last_heartbeat carrying something that is not a
# liveness signal -- a stale snapshot, a differently-scoped timestamp, a name
# collision. The conditional cannot tell that from the real thing, so it
# engages, and a reachable colleague whose value parses as stale is hidden.
# That is strictly WORSE than today's unfiltered listing, not equal to it.
#
# The honest statement is narrower: absence is safe, presence is trusted. We
# accept that because the filter fails toward the incident's own shape -- the
# tool reports how many colleagues are listed-but-not-responding, in BOTH the
# total-blackout case and the partial one -- so a wrongly-hidden colleague is
# visible as a count rather than as silence.
#
# That "both" was itself a correction (#38 pre-pass, r3): the count originally
# fired only when every colleague was filtered, which made this very paragraph
# true of the case it was written about and false of the case it generalizes
# to. If /directory is ever identified, check what it means by last_heartbeat
# before trusting any of this.
COLLEAGUE_STALE_AFTER_S = 120.0

# After this many CONSECUTIVE transport failures against one colleague, stop
# offering it until the TTL expires. The directory filter above catches an
# agent that was already down when the directory was read; this catches one
# that dies AFTER. Two, not one, because a single failure is as likely to be a
# blip as a death.
COLLEAGUE_DOWN_AFTER_FAILURES = 2
COLLEAGUE_DOWN_FOR_S = 300.0

# The task context id currently being handled, set by Agent.handle_task.
# The switchboard's DoomLoopGuardrail is default-on but groups hops by
# contextId; without a threaded id each hop looks like a fresh conversation
# and the guard never fires. A contextvar rather than provider state: tools
# are invoked with arguments only, and an agent may handle tasks
# concurrently.
current_context_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_context_id", default=None
)

# How many delegation hops led to the task currently being handled, set by
# Agent.handle_task from the inbound task's metadata (cabal#144). Threaded
# outbound +1 so a chain that crosses agents keeps counting.
#
# WITHOUT THIS THE CROSS-AGENT CYCLE IS UNBOUNDED. cabal bounds depth in its
# router, but a colleague call that carries no depth arrives at 0 however deep
# the caller already was -- so ledger -> gemini-one -> ledger, the shape from
# the 2026-09-10 incident, resets on every hop and the bound never fires. A
# contextvar for the same reason current_context_id is one: tools are invoked
# with arguments only, and an agent may handle tasks concurrently.
current_delegation_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "current_delegation_depth", default=0
)

#: cabal's metadata key. A CROSS-REPO STRING CONTRACT, named here so a grep
#: for it finds both ends: cabal reads this in DelegationDepthGuardrail.
DEPTH_KEY = "cabal_delegation_depth"

# Colleagues successfully consulted while handling the CURRENT task, in
# consultation order -- Agent.handle_task resets this per task and reads it
# after the final draft to enforce the attribution law (a consulted
# colleague must be NAMED in the reply; observed live 2026-08-27: the
# standing instruction alone was skipped under a bare ask, so the runtime
# appends the record itself when the model omits it). A contextvar for the
# same reason current_context_id is one: concurrent tasks.
consulted_colleagues: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "consulted_colleagues", default=None
)


def _text_parts_of(message: dict) -> str:
    """Join ALL text parts, in order — the same contract as
    transports.a2a.text_of, mirrored here rather than imported because that
    module requires the a2a extra (fastapi) and this provider must not.
    Taking parts[0] alone would silently drop text the day a peer emits a
    multi-part reply."""
    parts = message.get("parts") or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("kind") == "text"]
    return "\n".join(t for t in texts if t)


class ColleaguesNotConfigured(Exception):
    """Raised by fetch_agents() when no token is configured.

    fetch_agents() is public: daedalus.memory.revision's colleague-quorum
    branch calls it directly (over the ColleaguesLike Protocol) to reuse
    this provider's exact directory fetch. This provider's own tool-facing
    callers (_list_colleagues, _remember) already check the token first and
    return their own polite not-configured text before ever calling
    fetch_agents(), so they never see this exception; a caller that skips
    that pre-check (or calls fetch_agents() directly, as revision.py does)
    gets a typed error instead of a request sent with "Authorization: Bearer
    None".
    """


def _directory_publishes_liveness(agents: list) -> bool:
    """Does this directory carry liveness at all?

    THE DISTINCTION THAT KEEPS THIS SAFE (daedalus#38). "No agent has a
    heartbeat field" means the switchboard does not publish liveness, and
    filtering on it would empty the directory. "THIS agent has no heartbeat"
    means that agent never reported in -- which is exactly the `echo`-with-no-
    deployment case from the incident.

    So the filter only engages when at least one agent proves the field is
    served. A switchboard that never publishes liveness degrades to today's
    behaviour instead of silently hiding every colleague.

    PROVES MEANS PARSES, and this used to test only for the key (#38 r1
    Important 1). There is a fourth shape between "no field" and "a real one":
    a field in a dialect this module cannot read. `_heartbeat_age_s` returns
    None for that, and `_is_reachable` reads None as "never reported in" -- so
    an unreadable heartbeat was conflated with a missing one and every agent
    in a healthy fleet was hidden at once, under the total-blackout sentence
    that tells the model retrying will not help. Epoch seconds is not an
    exotic guess: `datetime.fromisoformat(str(1757600000))` raises.

    Requiring the probe to parse makes "absence is safe" true of BOTH
    absences. The field being missing is the colleague's fact; the field being
    unreadable is ours, and we should not charge a colleague for it.
    """
    return any(
        isinstance(a, dict) and _heartbeat_age_s(a, 0.0) is not None for a in agents
    )


def _heartbeat_age_s(agent: dict, now: float) -> float | None:
    """Seconds since this agent last reported in, or None if it never has."""
    raw = agent.get("last_heartbeat")
    if not raw:
        return None
    try:
        beat = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=UTC)
    return now - beat.timestamp()


def _is_reachable(agent: dict, now: float) -> bool:
    """Is this colleague worth offering to the model?

    Two independent ways to be unreachable, and the incident produced both:
    a row whose status is no longer active, and a row still marked active
    whose pod is scaled to 0/0 so its heartbeat went stale. `status` alone
    would not have caught sherlock-lite; the heartbeat is what does.
    """
    if str(agent.get("status") or "active").lower() not in ("active", "available"):
        return False
    age = _heartbeat_age_s(agent, now)
    if age is None:
        return False
    return age <= COLLEAGUE_STALE_AFTER_S


def _format_colleague(agent: dict, now: float | None = None) -> str:
    name = agent.get("name", "unknown")
    team = agent.get("team") or "unspecified"
    tags = agent.get("purpose_tags") or []
    tags_text = ", ".join(str(tag) for tag in tags) if tags else "none"
    capability = agent.get("capability") or "unspecified"
    line = (
        f"{name} (team: {team})\n"
        f"purpose tags: {tags_text}\n"
        f"capability: {capability}"
    )
    # SURFACE LIVENESS, do not merely filter on it (daedalus#38). A model shown
    # only reachable colleagues cannot tell "nobody can answer this" from "I
    # have not looked hard enough" -- and the incident is what the latter
    # belief costs. When the directory publishes heartbeats, say how fresh
    # this one is.
    if now is not None:
        age = _heartbeat_age_s(agent, now)
        if age is not None:
            # CLAMPED AT ZERO (#38 r1 Nit 5). Clock skew between the directory
            # and this pod needs no misbehaviour to occur, and "last seen:
            # -599s ago" is a nonsense string to hand a model. It already
            # failed open -- a future beat reads as fresh, which is the right
            # direction -- so this is only about what the model reads.
            line += f"\nlast seen: {max(0, int(age))}s ago"
    return line


class ColleaguesToolProvider:
    def __init__(
        self,
        url: str,
        token_env: str | None = None,
        self_name: str | None = None,
        client=None,
        db_path: str | Path | None = None,
    ):
        self.url = url.rstrip("/")
        self.token_env = token_env
        self.self_name = self_name
        self._client = client  # injectable: tests pass a fake get/post client
        # The agent's techne db, threaded in at wiring time (Agent._tools_from_spec,
        # from the memory store's own db_path). None = no techne memory configured;
        # remember() then degrades politely instead of failing at boot.
        self.db_path = Path(db_path) if db_path is not None else None
        # target -> [consecutive_failures, down_until_epoch,
        # last_failure_epoch]. The third element is what makes the count decay:
        # _record_failure starts a fresh count when the previous failure is
        # older than the ban window, so a blip cannot wait a day and be
        # completed by a second one. (_is_marked_down only REPORTS; the r2
        # round moved the forgetting to the increment, because the guard reads
        # the clock before a request and the count is stamped after it.) The
        # directory
        # filter catches a colleague that was already down when the directory
        # was read; this catches one that dies AFTER (daedalus#38). Process-
        # local and deliberately not persisted: it is a circuit breaker, and a
        # restart SHOULD retry rather than inherit a stale verdict.
        self._failures: dict[str, list[float]] = {}

    def _ensure_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=180.0)
        return self._client

    def _token(self) -> str | None:
        if not self.token_env:
            return None
        token = os.environ.get(self.token_env, "")
        return token or None

    async def tools(self) -> list[dict]:
        return [
            {
                "name": "colleagues.list_colleagues",
                "description": (
                    "List your colleagues from the switchboard directory: "
                    "their name, team, purpose tags, and capability."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "colleagues.ask_colleague",
                "description": "Ask a named colleague a question over A2A and get their reply.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The colleague's name, as listed in the directory",
                        },
                        "question": {
                            "type": "string",
                            "description": "The question to ask them",
                        },
                    },
                    "required": ["name", "question"],
                },
            },
            {
                "name": "colleagues.remember",
                "description": (
                    "Remember a fact a colleague told you. Stored at low confidence, "
                    "with the colleague credited as its source."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fact": {
                            "type": "string",
                            "description": "The fact to remember, verbatim",
                        },
                        "learned_from": {
                            "type": "string",
                            "description": "The colleague's name, as listed in the directory",
                        },
                    },
                    "required": ["fact", "learned_from"],
                },
            },
        ]

    async def call(self, name: str, payload: dict) -> str:
        if name == "colleagues.list_colleagues":
            return await self._list_colleagues()
        if name == "colleagues.ask_colleague":
            return await self._ask_colleague(payload.get("name", ""), payload.get("question", ""))
        if name == "colleagues.remember":
            return await self._remember(payload.get("fact", ""), payload.get("learned_from", ""))
        raise LookupError(f"unknown tool {name!r}")

    async def fetch_agents(self) -> tuple[list[dict] | None, str | None]:
        """GET the switchboard directory. (agents, None) on success, or
        (None, plain-text error) on any failure -- the one request shape
        both list_colleagues and remember validate names against.

        Public and token-gated: raises ColleaguesNotConfigured before ever
        touching the network if no token is configured, the same gate every
        other network-touching call on this provider applies. Public
        because daedalus.memory.revision's colleague-quorum branch reuses
        this exact fetch (see the ColleaguesLike Protocol there) to
        corroborate colleague-sourced candidates over the same client and
        config the model's own colleagues.* tools use.
        """
        token = self._token()
        if token is None:
            raise ColleaguesNotConfigured("the colleague pass is not configured")
        client = self._ensure_client()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            response = await client.get(f"{self.url}/directory", headers=headers)
        except Exception as exc:
            return None, f"Could not reach the colleague directory: {exc}"
        if response.status_code != 200:
            return None, f"The colleague directory returned an error: HTTP {response.status_code}."
        try:
            data = response.json()
            agents = data.get("agents", []) if isinstance(data, dict) else []
        except Exception:
            return None, _UNREADABLE_REPLY
        return agents, None

    async def _list_colleagues(self) -> str:
        token = self._token()
        if token is None:
            return _NOT_CONFIGURED
        agents, error = await self.fetch_agents()
        if error is not None:
            return error
        now = time.time()
        publishes_liveness = _directory_publishes_liveness(agents)
        offered = []
        hidden = 0
        for agent in agents:
            if not isinstance(agent, dict) or agent.get("name") == self.self_name:
                continue
            name = str(agent.get("name") or "")
            # Two gates. The directory's own liveness, and what THIS process has
            # learned by failing to reach the target since the directory was read.
            if publishes_liveness and not _is_reachable(agent, now):
                hidden += 1
                continue
            if self._is_marked_down(name, now):
                hidden += 1
                continue
            offered.append(_format_colleague(agent, now if publishes_liveness else None))
        lines = offered
        if not lines:
            # DISTINGUISH EMPTY FROM HIDDEN, because the incident is what the
            # ambiguity costs. An agent told "no colleagues are listed" while
            # three unreachable ones sit behind the filter has been handed the
            # same dead end in nicer words -- and the lesson from that storm is
            # that a terminal error closes one affordance without terminating
            # the task. Say the door is shut and that retrying will not open it.
            if hidden:
                return (
                    f"No colleagues are reachable right now. {hidden} "
                    f"{'is' if hidden == 1 else 'are'} listed but not "
                    "responding, so asking them would fail. This will not "
                    "change by retrying; answer from what you have, or say you "
                    "cannot."
                )
            return "No colleagues are listed yet."
        if hidden:
            # THE PARTIAL CASE, WHICH THIS USED TO SWALLOW. The sentence above
            # fires only when EVERY colleague is filtered; a directory with one
            # reachable colleague and three hidden ones printed the one and said
            # nothing about the rest, which is the same ambiguity in a smaller
            # room. It also falsified the argument the liveness filter rests on
            # -- that a wrongly-hidden colleague surfaces as a count rather than
            # as silence (#38 pre-pass, r3, which found that claim true only of
            # the total-blackout case it was written to generalize).
            #
            # No names, deliberately: naming a colleague the tool has just
            # judged unreachable invites the ask it exists to prevent.
            lines = lines + [
                f"({hidden} further {'colleague is' if hidden == 1 else 'colleagues are'} "
                "listed but not responding, so they are not offered here.)"
            ]
        return "\n\n".join(lines)

    def _is_marked_down(self, name: str, now: float) -> bool:
        """Has this process given up on `name` for now?"""
        state = self._failures.get(name)
        if state is None:
            return False
        _failures, down_until, _last_failure_at = state
        # THIS REPORTS; IT DOES NOT FORGET. The forgetting lives in
        # _record_failure, which is the only place that can do it correctly:
        # this reads the clock BEFORE the request and the count is stamped
        # AFTER it, so a hung request opens a gap between the two and a check
        # here is a check against a stale `now` (#38 r2).
        #
        # THIS FUNCTION HAS NOW HAD THE FORGET RULE ADDED AND REMOVED ONCE
        # EACH, so the reasoning is worth keeping. r1 put a decay in both
        # places; both mutants survived and I read that as redundancy and
        # collapsed it to here. r2 measured that the collapse reopened the hole
        # -- the mutants had survived for want of a clock that advances DURING
        # a request, not because one mechanism was spare. With the decay back
        # in _record_failure, a pop here is genuinely redundant: it can only
        # ever run on an ask, and on an ask _record_failure resets a stale
        # record anyway (or _record_success drops it). Removing it changes
        # nothing any test or probe can see, so it goes -- a second definition
        # of a rule is worse than none, and this one already cost two rounds.
        return bool(down_until) and now < down_until

    def _record_failure(self, name: str, now: float) -> None:
        """Count a transport failure; ban after two that are near each other.

        THE COUNT DECAYS, and it did not (#38 r1 Important 2). A bare-count
        record carried no timestamp and was never evicted -- only a successful
        ask to the same colleague cleared it -- so two failures a full day
        apart banned a healthy colleague, and a colleague the agent asks rarely
        is exactly the one that accumulates. The constant's own comment
        justifies two-not-one because "a single failure is as likely to be a
        blip as a death"; across time the code gave none of that protection.

        A failure older than the ban window is a forgotten blip, so a later one
        starts the count again rather than completing it.

        AND THE CHECK HAS TO BE HERE, not only in `_is_marked_down` (#38 r2).
        An earlier version removed this branch as redundant, on the grounds
        that every path to this method passes through that one first. True
        STRUCTURALLY, false TEMPORALLY, and temporal is what a count needs:
        `_ask_colleague` reads the clock before the POST and stamps the failure
        with a fresh one after it, so between the two sits a request that can
        hang for its full timeout. The record is "absent or recent" as of the
        GUARD, not as of the increment. Measured: two failures 380s apart
        completed a ban under a 300s window, because the second request hung
        for 180s after a guard that saw only 200s of gap.

        That is why the two mutants both survived when this was written as one
        rule -- not because two mechanisms implemented one rule, but because no
        test advanced a clock DURING a request. One now does.
        """
        state = self._failures.get(name)
        if state is not None and now - state[2] > COLLEAGUE_DOWN_FOR_S:
            state = None
        if state is None:
            state = [0.0, 0.0, now]
            self._failures[name] = state
        state[0] += 1
        state[2] = now
        if state[0] >= COLLEAGUE_DOWN_AFTER_FAILURES:
            state[1] = now + COLLEAGUE_DOWN_FOR_S

    def _record_success(self, name: str) -> None:
        """One success clears the record. CONSECUTIVE failures are what matter
        -- a colleague that answers between two blips is not failing."""
        self._failures.pop(name, None)

    async def _ask_colleague(self, target: str, question: str) -> str:
        token = self._token()
        if token is None:
            return _NOT_CONFIGURED
        if target == self.self_name:
            return "I cannot ask myself a question. Try asking one of my colleagues instead."
        now = time.time()
        if self._is_marked_down(target, now):
            # A TERMINAL STATE, not another transient-looking failure
            # (daedalus#38). In the incident an agent read "could not reach X"
            # as a reason to try again; it was permanent and unconditional and
            # nothing said so. After COLLEAGUE_DOWN_AFTER_FAILURES consecutive
            # failures we stop dressing a closed door as a bad day.
            return (
                f"{target} is not responding and I have stopped trying. This is "
                "not a transient failure -- retrying it will fail the same way. "
                "Ask a different colleague, answer from what you have, or say "
                "you cannot."
            )
        client = self._ensure_client()
        headers = {"Authorization": f"Bearer {token}"}
        # Dialect note: this is the SWITCHBOARD's inbound routing envelope
        # ({target, task}), POSTed to the switchboard's /a2a/tasks. Daedalus's
        # own peer surface serves the same path but takes the bare Task
        # (design.md §8) — same route string, two different callers.
        envelope = {
            "target": target,
            "task": {
                "kind": "task",
                "id": f"ask-{secrets.token_hex(8)}",
                "status": {
                    "state": "submitted",
                    "message": {
                        "kind": "message",
                        "role": "user",
                        "parts": [{"kind": "text", "text": question}],
                    },
                },
                "history": [],
            },
        }
        context_id = current_context_id.get()
        if context_id is not None:
            envelope["task"]["contextId"] = context_id
        # +1: THIS ASK IS A HOP, so the minimum value ever sent is 1 and the
        # key is always present.
        #
        # AN EARLIER COMMENT JUSTIFIED THAT BY SAYING THE RECEIVER DISTINGUISHES
        # zero from absent. It does not (#40 pre-pass): cabal reads
        # `metadata.get(DEPTH_KEY, 0)`, so a missing key and an explicit 0
        # collapse onto the same value through the same path. The real reason
        # to always send is narrower and survives that: a receiver CAN only
        # draw the distinction if the sender is unconditional about it, and we
        # never send 0 anyway, so there is nothing to lose by being explicit
        # and one contract to gain.
        envelope["task"]["metadata"] = {
            **envelope["task"].get("metadata", {}),
            DEPTH_KEY: current_delegation_depth.get() + 1,
        }
        try:
            response = await client.post(
                f"{self.url}/a2a/tasks", json=envelope, headers=headers, timeout=180.0
            )
        except Exception as exc:
            # time.time() AGAIN, not the `now` captured before the POST (#38 r1
            # Nit 6). down_until is stamped from whatever is passed, so a
            # request that hangs to its 180s timeout bought a 120s ban instead
            # of 300 -- and a pod scaled to 0/0 behind a proxy fails BY
            # timeout, which is the incident's own shape and the case where the
            # ban shrank most.
            self._record_failure(target, time.time())
            return f"Could not reach {target}: {exc}"
        if response.status_code != 200:
            self._record_failure(target, time.time())
            return f"Asking {target} failed: HTTP {response.status_code}."
        # CLEARED HERE, ON THE SAME EVENT CLASS THE COUNTER COUNTS (#38 r1
        # Important 2). It used to clear after the parse and the empty-text
        # check, so a colleague answering a perfectly good 200 with no text
        # part cleared nothing -- and blip, real round trip, blip banned it.
        # The counter counts TRANSPORT events; the clear must too, or
        # "consecutive" is not the predicate this implements.
        self._record_success(target)
        try:
            data = response.json()
            text = _text_parts_of(data["status"]["message"])
        except Exception:
            return _UNREADABLE_REPLY
        if not text:
            return _UNREADABLE_REPLY
        # A REFUSAL ARRIVES AS HTTP 200 (#40 r2 Important). cabal turns a
        # guardrail BLOCK into an annotated Task -- state="failed", the refusal
        # as the single agent text part -- and `routes/tasks.py` returns it
        # through a `response_model=Task` route. So the transport says 200, the
        # TASK says failed, and reading only `status.message` turns a refusal
        # into an answer.
        #
        # THE LEDGER IS WHY THIS IS NOT COSMETIC. Appending here credits the
        # colleague, and the attribution law in agent.py fires deterministically
        # after post-draft enforcement so the credit cannot be stripped -- the
        # agent tells its caller it consulted someone who never saw the
        # question, in the one mechanism whose stated purpose is truthful
        # credit.
        #
        # THE READER GAP PRE-DATES THIS BRANCH and is LIVE TODAY through the
        # doom-loop and budget guardrails, which produce the identical shape --
        # that half needs no depth guard to matter and is why this fix is not
        # merely forward-looking.
        #
        # WHAT THIS BRANCH OWNS is the depth door specifically, and stated in
        # the right tense (#40 r2 pre-pass): cabal's DelegationDepthGuardrail is
        # not on cabal main, so "before this branch, 0 < 8 always passed"
        # describes a comparison that is not running anywhere yet. It becomes
        # true when cabal#144 merges. What is true NOW is narrower -- sending a
        # depth is what gives that guard anything to refuse, so this PR and
        # cabal#144 together make a depth refusal reachable, and this reader
        # must be ready before either lands.
        state = (data.get("status") or {}).get("state")
        if state in _UNANSWERED_STATES:
            return f"{target} did not answer: {text}"
        ledger = consulted_colleagues.get()
        if ledger is not None and target not in ledger:
            ledger.append(target)
        return text

    async def _remember(self, fact: str, learned_from: str) -> str:
        """Remember a fact a named colleague told the agent: low confidence,
        provenance stamped to the colleague (and their team, when known).

        Refuses two shapes without ever touching techne: crediting itself
        (self_name), and crediting a name the switchboard directory doesn't
        list. Every other failure -- a down directory, a missing techne db,
        a store() that raises -- degrades to plain text, same as its sibling
        tools; this provider never raises out to the agent loop.
        """
        token = self._token()
        if token is None:
            return _NOT_CONFIGURED
        if learned_from == self.self_name:
            return "I cannot credit myself with teaching me something. Ask a colleague instead."
        if self.db_path is None:
            return _NO_MEMORY
        agents, error = await self.fetch_agents()
        if error is not None:
            return error
        match = next(
            (
                agent
                for agent in agents
                if isinstance(agent, dict) and agent.get("name") == learned_from
            ),
            None,
        )
        if match is None:
            return (
                f"I don't have a colleague named {learned_from} in the directory, "
                "so I cannot credit them."
            )
        team = match.get("team") or ""
        source = f"colleague:{learned_from}#team={team}" if team else f"colleague:{learned_from}"
        try:
            from techne.core import knowledge

            knowledge.store(self.db_path, fact, type="fact", confidence="low", source=source)
        except Exception as exc:
            return f"I could not remember that: {exc}"
        return f"Remembered, crediting {learned_from}."

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
