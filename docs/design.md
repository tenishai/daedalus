# Daedalus — design

**Status: draft for review — no implementation until approved**

Daedalus is an open-source agent construction kit: the assembly layer of the
tenish family. It defines a declarative **agent bundle** (a directory that *is*
an agent), a small runtime that instantiates bundles, pluggable transports so
one bundle runs in Slack, over A2A, or headless in CI, and a three-tier
approval model. A companion GUI (**kaminos**, Greek for furnace) edits bundles
the way NeXTSTEP Interface Builder edited nibs: live objects, no codegen.

## Lineage and clean-room policy

Daedalus is a clean-room design. It is informed by lessons from an internal
proprietary SDK (client_agent: Slack transport ergonomics, human-in-the-loop
approval bridging, "agent in 50 lines" DX) and by the open tenish repos
(techne, limenal, cabal, ladon, gojira, agents), but **no code is carried over
from proprietary sources**. Where a tenish repo already solves a problem,
daedalus integrates rather than reimplements.

## Positioning: what daedalus is NOT

| Concern | Owner | Daedalus's role |
|---|---|---|
| Memory (episodic + knowledge) | techne | `MemoryStore` Protocol; techne is the default impl via extra |
| Tool gateway, credential scoping | limenal | `ToolProvider` Protocol; limenal via extra, direct MCP otherwise |
| Multi-agent orchestration, A2A broker | cabal | daedalus agents are cabal-compatible handlers; A2A transport speaks to it |
| Secrets platform | ladon | daedalus reads env/file; ladon patterns for hygiene |
| Deploy | gojira | a daedalus bundle is a deployable app |

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
└── skills/            # optional SKILL.md reference material
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
  id: anthropic/claude-sonnet-4-5      # any LiteLLM route string
  # api_base: http://10.0.0.15:8000/v1 # optional — local OpenAI-compatible servers
  max_tokens: 8192
  budget:
    per_task_tokens: 200000            # reserve/refund; exhaustion -> code 702

transports:
  - kind: slack
    channel: "#pr-reviews"
  - kind: a2a
    port: 9200
  - kind: cli                           # always available for `daedalus chat`

tools:
  provider: mcp                         # mcp | limenal | none
  servers:                              # provider=mcp: direct MCP servers
    - command: ["npx", "@modelcontextprotocol/server-github"]
  allow:                                # namespaced allowlist (ceiling)
    - github.pr_view
    - files.read

memory:
  personal: techne                      # techne | none  (extra: daedalus[techne])
  team: none                            # git-backed shared store, later

approvals:
  ceiling:                              # tier 1 — hard capability ceiling
    write: false
    hosts: [github.com]
  gates:                                # tier 2 — plan/diff/apply
    - match: "github.pr_merge"
      require: plan_approval
  escalation:                           # tier 3 — human in the loop
    route: slack:#pr-reviews            # or: cli, webhook:<url>
```

Loaded into Pydantic models with `extra="allow"` on unknown sections (warn,
don't fail) so old runtimes tolerate new GUI-written fields. `${VAR}` /
`${VAR:-default}` interpolation on load; secrets never appear in the file.

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
    async def recall(self, query: str, k: int = 5) -> list[Memory]: ...
    async def commit(self, content: str, kind: str) -> None: ...

class ToolProvider(Protocol):
    async def tools(self) -> list[ToolSpec]: ...
    async def call(self, name: str, payload: dict, ctx: Context) -> ToolResult: ...

class CheckpointStore(Protocol):     # save/load/drop; enables crash-resume
class Budget(Protocol):              # reserve/refund; trips 702
class Approver(Protocol):            # tier 2/3 decisions (see §3)
class Transport(Protocol):           # start/stop; delivers Tasks to the handler
```

Default implementations ship in-tree (in-memory/no-op/SQLite). Real backends
arrive via extras and entry points (`daedalus.memory`, `daedalus.tools`,
`daedalus.transports`): `daedalus[techne]`, `daedalus[limenal]`,
`daedalus[slack]`. Third parties add backends without touching core.

### 2.3 LLM invocation

LiteLLM route strings only — no provider SDK imports in core. Per-task token
budget (reserve → refund), and a client-side circuit breaker so a flaky
provider degrades all loops together rather than thundering.

Reasoning-model think traces are split out of the reply into
`LLMResponse.reasoning` — never shown as the answer (first dogfood finding,
DeepSeek R1 on vLLM). They will feed the kaminos trace pane over SSE.

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

FastAPI stays at the edge: nothing below a transport imports it.

## 3. Approvals — three tiers

1. **Ceiling** (static, tier 1): the `tools.allow` list and
   `approvals.ceiling` are enforced at both tool-listing and tool-call time —
   the model never sees tools it can't call, and a hallucinated name can't
   bypass scope. Runtime context may *narrow* the ceiling, never widen it.
   Malformed policy fails closed.
2. **Gates** (plan/diff/apply, tier 2): matched operations must first render
   a value-free plan (the intended action as reviewable data). The `Approver`
   presents it (CLI prompt, Slack message, or PR when the effect targets a
   repo — "the PR is the approval") and only then executes. Dry-run is the
   default posture for anything matched.
3. **Escalation** (tier 3): the agent itself, or a tripped gate/budget, emits
   700/704 and the configured route carries it to a human; the task parks in
   `input-required` until answered.

**Approver routing is process-wide, by design.** The approver is an agent
collaborator, not a transport property: when a Slack transport is serving,
it upgrades a still-default (DenyAll) approver to reaction approvals — so a
gated call arriving via a2a or cli also posts its plan to Slack.
*Approvals flow to where the humans are.* An explicitly wired approver is
always respected; headless agents with no human-bearing transport keep
DenyAll and fail closed. (Decided 2026-07-06 after the kaminos canvas
surfaced the interplay; documented rather than "fixed" — it is the correct
behavior, arrived at by accident.)

Secrets hygiene primitives (mask, placeholder-refusal) ship in core and are
applied to anything approval-bound or logged.

## 4. kaminos — the builder GUI (companion repo)

Interface Builder for agents. Svelte 5 frontend, FastAPI backend, separate
repo, talks to daedalus over a local API.

| NeXTSTEP concept | kaminos |
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
| 0 | this doc reviewed; repos created (tenishai/daedalus, tenishai/kaminos) |
| 1 (v0.1) | bundle spec + loader, core runtime, LiteLLM loop, cli transport, `daedalus create/run/chat`, fakes + goldens |
| 2a (v0.2) | tool loop, direct MCP `ToolProvider`, approvals tiers 1–2 |
| 2b (v0.2) | techne extra, checkpoints + budget |
| 3 (v0.3) | a2a transport + card serving, slack transport + tier-3 escalation, limenal extra |
| 4 (v0.4) | kaminos: bundle editor (palette/canvas/inspector) + test mode over SSE |
| 5 (v0.5) | kaminos live wiring against a running cabal |

## 7. Open questions — resolved 2026-07-02

- **Team memory: half now, half later.** techne 0.8.0 ships the egress half —
  the sync outbox with a pluggable `SyncAdapter` Protocol (entry-point
  extensible). The v0.2 techne extra therefore wires personal memory *and*
  optionally enables the outbox so curated knowledge ships to a team store.
  Read-through team recall waits for techne to grow that seam
  (the `teammem` relocation) rather than daedalus guessing it.
- **Card signing: deferred to v0.3**, when the a2a transport lands. Identity
  without dispatch doesn't need signatures; adopt cabal's JWS approach then.
- **kaminos test mode: spawn-and-attach.** The GUI spawns `daedalus run` and
  attaches to its SSE stream — what you test is exactly what ships.
