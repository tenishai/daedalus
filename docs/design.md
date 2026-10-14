# Daedalus — design

**Status: IMPLEMENTED AND RELEASED.** This began as a pre-implementation design
doc and is kept as the architectural rationale, not as a plan. daedalus 0.5.2 has
shipped; §8 was written against live behaviour. Where this document and the code
disagree, the code is right — `src/daedalus/protocols.py` is the authority on the
Protocol seams.

Daedalus is an open-source agent construction kit: the assembly layer of the
tenish family. It defines a declarative **agent bundle** (a directory that *is*
an agent), a small runtime that instantiates bundles, pluggable transports so
one bundle runs in Slack, over A2A, or headless in CI, and a three-tier
approval model. A companion visual builder (a separate project, not yet released) edits bundles
the way NeXTSTEP Interface Builder edited nibs: live objects, no codegen.

## Lineage and clean-room policy

Daedalus is a clean-room design. It is informed by lessons from an internal
proprietary SDK (client_agent: Slack transport ergonomics, human-in-the-loop
approval bridging, "agent in 50 lines" DX) and by the open tenish repos
(techne, liminal, cabal, ladon), but **no code is carried over
from proprietary sources**. Where a tenish repo already solves a problem,
daedalus integrates rather than reimplements.

## Positioning: what daedalus is NOT

| Concern | Owner | Daedalus's role |
|---|---|---|
| Memory (episodic + knowledge) | techne | `MemoryStore` Protocol; techne is the default impl via extra |
| Tool gateway, credential scoping | liminal | `ToolProvider` Protocol; liminal via extra, direct MCP otherwise |
| Multi-agent orchestration, A2A broker | cabal | daedalus agents are cabal-compatible handlers; A2A transport speaks to it |
| Secrets platform | ladon | daedalus reads env/file; ladon patterns for hygiene |
| Deploy | a deployment platform | a daedalus bundle is a deployable app |

Daedalus owns: the bundle format, the runtime that loads it, transports,
approvals, and the CLI/GUI developer experience.

## 1. Data model

### 1.1 The agent bundle (the "nib")

An agent is a directory. Everything the runtime needs is data in that
directory; the GUI reads and writes the same files. No code generation.

```
prudence/
├── agent-card.json    # A2A Agent Card — identity, capabilities, skills
├── persona.md         # system prompt / SOP — the single persona source
├── agent.yaml         # wiring: model, transports, tools, memory, approvals
└── skills/            # <id>/SKILL.md — seeded as procedure memory at boot,
                       #   and indexed for the skills.load tool
```

- `agent-card.json` follows the A2A `/.well-known/agent-card.json` schema.
  Identity is the card; we do not invent a parallel identity format.
- `persona.md` is plain markdown, adapted per runtime (system prompt for the
  LLM loop, injected prompt for headless CI runs).
- `agent.yaml` is the only daedalus-specific file.

### 1.2 `agent.yaml` schema (v0)

```yaml
name: prudence
card: agent-card.json
persona: persona.md

model:
  id: anthropic/claude-sonnet-4-5      # any LiteLLM route string, or a tier word
                                        # (fast | balanced | best, §1.2a)
  # api_base: http://10.0.0.15:8000/v1 # optional — local OpenAI-compatible servers
  max_tokens: 8192
  budget:
    per_task_tokens: 200000            # reserve/refund; exhaustion -> code 702

transports:
  - kind: slack                         # no channel = every channel the app
                                        # is invited to (see 2.4); name one
                                        # with `channel: C012ABC` to pin an
                                        # allowlist instead. Tier-2 approvals
                                        # need BOTH `channel` and
                                        # `approvers: [U012ABC]`; without them
                                        # every tier-2 action is denied
  - kind: a2a
    port: 9200
  - kind: cli                           # always available for `daedalus chat`

tools:
  provider: mcp                         # mcp | liminal | colleagues | none
  servers:                              # provider=mcp: direct MCP servers
    - command: ["npx", "@modelcontextprotocol/server-github"]
  colleagues:                           # provider=colleagues: switchboard directory
    url: http://cabal:8300              # list_colleagues + ask_colleague over A2A
    token_env: CABAL_AGENT_TOKEN        # unset/empty = degraded, never a boot failure
  builtin:                              # in-process tool groups
    files: {roots: ["."]}               # files.read/list/search, read-only, rooted
    exec:                               # exec.run: a command in the agent's own workspace;
      roots: ["."]                      # needs approvals.ceiling.exec: true (below).
      commands: [python, pytest, git]   # optional allowlist of bare program names
      env: [PIP_INDEX_URL]              # passed through on top of PATH/HOME/locale; the
      timeout: 300                      # agent's own env isn't passed (not a boundary: same user)
  allow:                                # namespaced allowlist (ceiling)
    - github.pr_view
    - files.read

memory:
  personal: techne                      # techne | none  (extra: tenish-daedalus[techne])
                                        # recalls 5 entries on the prompt, or on the
                                        # task's daedalus_recall metadata (run --recall)
  team: none                            # reserved: only `none` is accepted until the
                                        # team outbox exists (see section 7)

approvals:
  ceiling:                              # tier 1 — hard capability ceiling
    write: false
    hosts: [github.com]
    exec: true                          # required when tools.builtin.exec is set, or the
                                        # bundle won't load; commands run as the agent in
                                        # its container (not a security boundary)
  gates:                                # tier 2 — plan/diff/apply
    - match: "github.pr_merge"
      require: plan_approval
  escalation:                           # tier 3 — human in the loop
    route: slack:#pr-reviews            # or: cli, webhook:<url>
```

Loaded into Pydantic models with `extra="allow"` on unknown sections (warn,
don't fail) so old runtimes tolerate new GUI-written fields. `${VAR}` /
`${VAR:-default}` interpolation on load; secrets never appear in the file.

### 1.2a Model tiers (#103, #129)

A bundle can say how much model it needs instead of naming one: `model.id` is either a
concrete model, or (after `${VAR}` expansion) one of the tier words `fast`, `balanced`,
`best`. Each deployment maps tiers to models once, in a YAML file named by
`DAEDALUS_TIERS`, so the same bundle runs on Claude at one site and on a local model at
another without an edit — `id: "${PRUDENCE_MODEL:-best}"` lets an operator still pin a
model while defaulting to the deployment's tier. Examples are in `examples/tiers/`.

```yaml
tiers:
  best:     {engine: claude-code, id: claude-opus-5-5}
  balanced: {engine: claude-code, id: claude-sonnet-5-5}
  fast:
    engine: litellm
    id: openai/<model>
    api_base: http://<host>:8000/v1
    api_key_env: LOCAL_API_KEY
    context_tokens: 45000              # this model on this hardware
    max_repeats: 3
  # a tier can alias another (balanced: best), or be null: not offered here
```

The rules (`src/daedalus/tiers.py`, `daedalus.agent._resolve_model`):
- **A concrete `model.id` is only recorded.** Any id that isn't `fast`, `balanced` or
  `best` is a model, unchanged.
- **The tier's engine is the default.** An explicit `--engine` or `DAEDALUS_ENGINE` that
  disagrees is refused at load, not silently obeyed.
- **The entry fills what the bundle left unset**: `id`, `api_base`, `api_key_env`,
  `max_tokens`, `tools.context_tokens`, `tools.max_repeats`, and the Claude Code model.
  A value the bundle set wins.
- **A tier word never reaches LiteLLM as a model name.** With no tiers file named, load
  refuses naming the tier and `DAEDALUS_TIERS`; with a tiers file that doesn't offer the
  tier, load refuses naming the tier and the tiers the file does offer.
- **The resolved model is always on the record**, because a tier floats the way the
  `opus` alias did (an alias can start resolving to a different model with no edit on
  your side): logged at load, emitted as the `model_resolved`
  event (shown by `run -v`, replayed on `/events`), and served on the a2a agent card
  as `daedalus_model`.
- `model.tier` and `model.min_tier` (PR 127) are removed: a bundle that still sets
  either fails at load, pointing at `model.id`. The tiers *file* format is unchanged.

### 1.3 Task envelope

The unit of work is the **A2A Task** (Pydantic models mirroring A2A v1.0
Task/Message/Part). Daedalus adds no wire format. Domain semantics ride in
`task.metadata` as **reason codes** — adopting cabal's taxonomy so the two
systems interoperate without translation:

| Code | Meaning | Caller should |
|---|---|---|
| 700 | human input required | prompt the operator |
| 701 | policy violation (ceiling/gate refused) | don't retry; surface |
| 702 | budget exhausted | surface; maybe re-fund |
| 704 | escalation requested | route to a human |

In-progress/complete/failed are plain A2A Task states, not codes. This
taxonomy has already survived one protocol-generation migration (bespoke
CHP envelope → standard A2A): the reason codes are the durable part; the
envelope should be someone else's standard.

### 1.4 Correlation

Every external conversation (Slack thread ts, A2A task id, CI run id) binds
1:1 to a runtime session via an idempotent `external_id`, mirroring techne's
`init_session_for_external_id` so memory attribution is free when techne is
wired.

## 2. Runtime API

### 2.1 The agent contract is one method

```python
class Handler(Protocol):
    async def handle_task(self, task: Task) -> Task: ...
```

No god-object base class. The batteries-included path is a small composed
runner, not inheritance:

```python
from daedalus import Agent

agent = Agent.load("./prudence")     # reads the bundle
agent.serve()                        # starts declared transports
```

`Agent` wires an LLM loop (LiteLLM), the persona, tools, and collaborators.
A deterministic (non-LLM) agent implements `Handler` directly and still gets
transports, approvals, and the bundle format.

### 2.2 Collaborators: no-op defaults, swapped at wiring

Every cross-cutting concern is a Protocol with a no-op default, injected by
the loader — never `None`-checked in the hot path:

```python
class MemoryStore(Protocol):
    async def recall(self, query: str, k: int = 5) -> list[str]: ...
    async def commit(self, content: str, kind: str = "fact") -> None: ...

class ToolProvider(Protocol):
    async def tools(self) -> list[dict]: ...
    async def call(self, name: str, payload: dict) -> str: ...

class CheckpointStore(Protocol):     # save/load/drop; enables crash-resume
class Budget(Protocol):              # reserve/refund; trips 702
class Approver(Protocol):            # tier 2/3 decisions (see §3)
class Transport(Protocol):           # start/stop; delivers Tasks to the handler
```

Default implementations ship in-tree (in-memory/no-op/SQLite). Real backends
arrive via extras and entry points (`daedalus.memory`, `daedalus.tools`,
`daedalus.transports`): `tenish-daedalus[techne]`, `tenish-daedalus[liminal]`,
`tenish-daedalus[slack]`. Third parties add backends without touching core.

### 2.3 LLM invocation

LiteLLM route strings only — no provider SDK imports in core. Per-task token
budget (reserve → refund), and a client-side circuit breaker so a flaky
provider degrades all loops together rather than thundering.

Reasoning-model think traces are split out of the reply into
`LLMResponse.reasoning` — never shown as the answer (first dogfood finding,
DeepSeek R1 on vLLM). They will feed the visual builder's trace pane over SSE.

### 2.4 Transports

A transport turns an external medium into Tasks and renders Task updates
back. Persona constant, transport variable:

- **cli** — `daedalus chat ./prudence` REPL; also `daedalus run` one-shot for CI.
- **a2a** — FastAPI: serves `/.well-known/agent-card.json`, accepts
  `message/send`, streams updates. This is the cabal-facing face.
- **slack** — Socket Mode. Carries the hard-won lessons as *requirements*:
  dedupe on `(channel, ts)`, thread-state store, reaction lifecycle
  (👀 → ✅/❌), graceful degradation when tokens are absent, reconnect
  supervision. Clean-room implementation.

  Every `kind: slack` block folds into **one** transport: Socket Mode
  load-balances each event to exactly one open connection per app, so a
  second transport on the same app token is a coin flip, not a second
  channel. Its reach is the union of the `channel` values those blocks
  name — **and naming none is the default**: an empty allowlist answers in
  every channel the app has been invited to, because inviting the app is
  already the human act that grants the reach and re-typing the ID is the
  same permission asked for twice (2026-09-23). Boot says which regime is
  in force, with the wake mode (`(slack listening in C1, C2, on @-mention)` vs
  `(slack listening in every channel the app is invited to, on every message)`). In channels and private groups the
  block's `wake` mode decides what is answered (2026-10-06): `mention`, the
  default and the 2026-08-31 rule (only an `app_mention`, or a plain message
  that mentions this bot); `thread` (a mention, then every reply in a thread
  the bot was asked in at the top, so a conversation continues without
  re-tagging, while a mention buried in someone else's thread adopts
  nothing); or `channel` (every message, for an agent whose job is the
  channel). `SLACK_WAKE_MODE` in the environment overrides the bundle, which
  is how a room sets it per agent. The mode is stamped on each Slack-born
  task and becomes a prompt notice, so the model knows which messages reach
  it instead of guessing (it cannot see the app's event subscriptions, so the
  manifest has to match the mode). Followed threads live in memory: after a
  restart, a `thread`-mode agent answers an old thread only when mentioned,
  and only a new thread started with a mention is followed again. DMs
  keep full delivery in every mode, and
  bots/subtypes/duplicates are still dropped. A reply is a streamed message
  (`chat.startStream`): the agent's tool calls and recalls appear as progress
  lines under it while it works, and it becomes the answer in place; where
  streaming is refused, the hourglass placeholder path remains. Widening *where* does change
  where an approval could be asked for: tier-2 reaction approvals follow the
  channel of the latest event. So an open transport keeps tier-2 **closed**
  (`DenyAll`) and says so at boot (2026-09-24). Who may approve is a second,
  separate setting: `approvers: [U012ABC, ...]` (Slack user ids) on a
  `kind: slack` block, unioned across blocks. Reaction approvals are wired
  only when the transport has both a `channel` allowlist and `approvers`;
  with either missing, tier-2 stays `DenyAll` and boot says which. `approvers` is
  transport-wide, not per channel: the lists are unioned and apply in every
  channel the approver follows. The requester does not approve their own
  request, even when listed, so a gate needs a second person, unless the
  requester is the only listed approver: a solo operator approves their own
  actions, as `daedalus chat` does at the terminal. The requester is carried
  per task (a contextvar set when a Slack message starts the task), so this
  self-exclusion covers Slack-originated requests only: a gate reached from
  a2a, the cli or a colleague agent has no known requester, nobody is
  excluded, and any listed approver may approve. An empty `approvers: []` is
  the same as none.
  Denials (a ❌) count from anyone in the channel. DMs are admitted without
  regard to the channel allowlist, and the approver retargets to the DM. In a
  1:1 DM (`im`) only the requester is present, so a gate there is denied in
  practice unless the requester is the only listed approver; a group DM
  (`mpim`) can include another listed approver, who may approve there.
  A `channel` key whose value is empty
  (a `${VAR}` set but empty; an unset one already fails in `Bundle.load`)
  fails boot rather than reading as open. Mixed blocks
  resolve open. Tier-3 escalations go wherever `approvals.escalation.route`
  says (`slack:<channel>` for Slack), never to the first named channel.

  **Direct messages bypass the channel allowlist, by design.** An `im`/`mpim`
  event is admitted whatever `channel` lists: DMs are full-delivery (the
  tagged-only rule is for channels), and Slack delivers them only to an app a
  user chose to message. The consequence for a bundle author: with DMs on,
  any workspace member who can DM the app reaches the model and the tools the
  bundle allows, and the tool ceiling is the only limit. There is no per-user
  allowlist (tenishai/daedalus#87 left that as a feature decision). The shipped
  `docs/slack-app-manifest.yaml` does not subscribe to DM events, so this is
  opt-in: it applies only when the Slack app is given `message.im` or
  `message.mpim` and the matching history scopes.

  The read-only companion, `tools.slack_read`, takes the same three-state
  shape: the block absent is **off**, `channels: []` is **on with no
  allowlist** (every invited channel, with Slack's own `not_in_channel` as
  the refusal), and `channels: [C012ABC]` is on and exactly those. Boot
  prints a line when the lane is open, because a bundle that wrote
  `channels: []` before 2026-09-23 got the lane off and now gets it on.

FastAPI stays at the edge: nothing below a transport imports it.

## 3. Approvals — three tiers

1. **Ceiling** (static, tier 1): the `tools.allow` list is enforced at both
   tool-listing and tool-call time — the model never sees tools it can't call,
   and a hallucinated name can't bypass scope. Runtime context may *narrow*
   the ceiling, never widen it. Malformed policy fails closed.
   `approvals.ceiling.exec` is checked once, at load: without `true`, a bundle
   that configures `tools.builtin.exec` fails to load. `approvals.ceiling.write`
   and `approvals.ceiling.hosts` are parsed but not yet enforced anywhere.
2. **Gates** (plan/diff/apply, tier 2): matched operations must first render
   a value-free plan (the intended action as reviewable data). The `Approver`
   presents it (CLI prompt, Slack message, or PR when the effect targets a
   repo — "the PR is the approval") and only then executes. Dry-run is the
   default posture for anything matched.
3. **Escalation** (tier 3): the agent itself, or a tripped gate/budget, emits
   700/704 and the configured route carries it to a human; the task parks in
   `input-required` until answered.

**Approver routing is process-wide, by design.** The approver is an agent
collaborator, not a transport property: when a Slack transport is serving
**and has both a `channel` allowlist and `approvers`** (§2.4), it upgrades a
still-default (DenyAll) approver to reaction approvals — so a gated call
arriving via a2a or cli also posts its plan to Slack. A bare `- kind: slack`
block, or one without `approvers`, leaves tier-2 denied.
*Approvals flow to where the humans are.* An explicitly wired approver is
always respected; headless agents with no human-bearing transport keep
DenyAll and fail closed. (Decided 2026-07-06 after the visual builder's canvas
surfaced the interplay; documented rather than "fixed" — it is the correct
behavior, arrived at by accident.)

Secrets hygiene primitives (mask, placeholder-refusal) ship in core and are
applied to anything approval-bound or logged.

## 4. The visual builder (companion project)

Interface Builder for agents. Svelte 5 frontend, FastAPI backend, separate
project, talks to daedalus over a local API.

| NeXTSTEP concept | Visual builder |
|---|---|
| Palette | personas, models, transports, tool handlers, memory stores, approval policies, sibling agents |
| Canvas + outlets/actions | the agent's object graph; drag connections (memory → store, escalation → channel, tool → allowlist) |
| Inspector | attribute panel per component: model params, budgets, TTLs, gate patterns |
| The nib | the agent bundle — GUI reads/writes `agent.yaml` + card + persona; runtime "freeze-dries" it |
| Test mode (⌘R) | run the bundle in-place: chat pane + live trace pane fed by the runtime's SSE event stream |
| Live wiring (later) | connect to a running cabal, introspect its registered agents, edit and hot-reload |

The GUI writes nothing but bundle files — every canvas is reproducible from
the directory, diffable in git, reviewable in a PR. That is the whole trick.

## 5. Testing strategy

- **Fakes for every Protocol** (`tests/fakes.py`): scripted LLM, in-memory
  transports, recording approvers. No network in unit tests.
- **Golden bundles**: example bundles under `tests/golden/` loaded and
  round-tripped (load → serialize → identical), doubling as docs.
- **Contract tests** pin the public API surface.
- **Approval-path tests are adversarial**: hallucinated tool names, ceiling
  widen attempts, malformed policies — all must fail closed.

## 6. Phasing

| Phase | Deliverable |
|---|---|
| 0 | this doc reviewed; repo created (tenishai/daedalus) |
| 1 (v0.1) | bundle spec + loader, core runtime, LiteLLM loop, cli transport, `daedalus create/run/chat`, fakes + goldens |
| 2a (v0.2) | tool loop, direct MCP `ToolProvider`, approvals tiers 1–2 |
| 2b (v0.2) | techne extra, checkpoints + budget |
| 3 (v0.3) | a2a transport + card serving, slack transport + tier-3 escalation, liminal extra |
| 4 (v0.4) | visual builder: bundle editor (palette/canvas/inspector) + test mode over SSE |
| 5 (v0.5) | visual builder live wiring against a running cabal |

## 7. Open questions — resolved 2026-07-02

- **Team memory: half now, half later.** techne 0.8.0 ships the egress half —
  the sync outbox with a pluggable `SyncAdapter` Protocol (entry-point
  extensible). The v0.2 techne extra therefore wires personal memory *and*
  optionally enables the outbox so curated knowledge ships to a team store.
  Read-through team recall waits for techne to grow that seam
  (the `teammem` relocation) rather than daedalus guessing it.
- **Card signing: deferred to v0.3**, when the a2a transport lands. Identity
  without dispatch doesn't need signatures; adopt cabal's JWS approach then.
- **Visual-builder test mode: spawn-and-attach.** The GUI spawns `daedalus run` and
  attaches to its SSE stream — what you test is exactly what ships.

## 8. The cabal wire contract — discovered 2026-07-06 (v0.5 scoping)

Read from cabal @ `c8614d9`; what §7 deferred to "the cabal integration
round" resolved differently than assumed:

- **Cabal dispatches to peers via `POST <endpoint>/a2a/tasks` with a bare
  A2A Task envelope** (`broker/router.py _dispatch_remote`) — not JSON-RPC
  `message/send`, which is inbound-only on cabal's side. The a2a transport
  therefore serves both surfaces: JSON-RPC for spec callers, `/a2a/tasks`
  for a dispatching cabal. Metadata (`cabal_trace_id`, ...) rides through
  verbatim and returns in the response envelope.
- **Card signing is NOT on cabal's dispatch trust path.** Graduation
  (`POST /admin/persona/{name}/graduate`) accepts an empty card, and the
  router never verifies cards when dispatching. Trust is the **caller's
  bearer JWT**: RS256, `iss=cabal-core`, `aud=` the peer's federation
  audience, verified against cabal's JWKS. The a2a transport's opt-in
  `auth` block (jwks_url / issuer / audience) implements the peer's half.
  JWS card signing stays deferred until something actually verifies it.
- **Reason codes are dual-keyed.** Cabal reads
  `metadata["cabal_reason_code"]` (`core/a2a/codes.py`) — same 700/701/702/
  704 taxonomy, 703 absent in both (shared CHP lineage). `Task.reply`
  writes `daedalus_reason_code` and `cabal_reason_code`; the reader prefers
  ours and falls back to cabal's.
- **The bundle shape is published.** `docs/bundle.schema.json` is the
  canonical agent.yaml contract for non-Python consumers (cabal's bundle
  reader, editors), emitted from `AgentSpec` via `daedalus schema` and
  drift-pinned by tests — the runtime controls the shape. the visual builder keeps
  validating through `AgentSpec` directly; per-transport extras (port,
  channel, auth) stay open, and unknown top-level fields load with a
  warning, never silently.
