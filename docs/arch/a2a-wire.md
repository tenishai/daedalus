---
card: arch-card:daedalus-a2a-wire
updated: 2026-10-24
anchor: src/daedalus/transports/a2a.py sha=67d3ffe317b0368d6b916baeb73f230dd7f44fb6
anchor: src/daedalus/task.py sha=e3ad7142acfa959da0577bda4d4c59478ca9df40
anchor: src/daedalus/memory/teach.py sha=4570cf9d5636cc08bddf1d3c2890d3e2a1caceae
---
# a2a-wire — the agent as a callable peer (cabal, kobo, kaminos)

WHAT / SEAM: `A2aTransport` / `build_a2a_app(handler, card, events, verifier)` (transports/a2a.py), FastAPI. Envelope = `Task` (task.py): id, context_id, state (A2A TaskState), history[Message(role user|agent, text, images, sender)], metadata.
Routes: GET /.well-known/agent-card.json, GET /healthz, GET /events (SSE: replay ≤100 then live; kaminos trace pane; behind the verifier when `auth` is set, #196), POST / (JSON-RPC message/send, tasks/get), POST /a2a/tasks (bare Task in/out; cabal DispatchRouter's federated dispatch).
OPS / FLOW: POST (and GET /events) → authorize (JwtVerifier from transport `auth` {jwks_url, issuer, audience}, a2a_auth.py; RS256 only, iss+aud pinned, 30 s leeway) → task_from_a2a (cabal puts the CURRENT message in status.message with empty history; appended unless duplicate) → stamp_verified_sub → handler.handle_task → task_to_a2a (reply mirrored into status.message AND history).
Operator lanes: metadata["system_task"] ∈ {teach, knowledge, tool, learnings} is dispatched by Agent._dispatch_system_task BEFORE enforcement and the model; any other marker → FAILED 701. teach/knowledge write techne; tool writes the enforcement overlay; learnings returns JSON {"candidates": [...]}, each with a verified `said_by` (a `<@U...>` sender or "user" for a plain user turn; a candidate that fails verification is dropped, #165). Keys: filename, delete (memory/teach.py:92-100).
INVARIANTS:
- metadata["_verified_sub"] is transport-owned: any wire value is popped unconditionally; set only from verified JWT claims. teach_authorized (memory/teach.py) trusts it and is the ONLY control over operator lanes. No verifier configured → no sub → every operator lane refused.
- teach_authorized is an ALLOWLIST (#195, closing #47): the a2a transport's `auth.operator_subs` (unioned across a2a blocks; an entry ending in `*` is a prefix; `*` alone and bare `operator` are refused at load), default `operator:*` and `service:kobo`. Why those two: kobo relays the lanes through cabal with `CABAL_OPERATOR_TOKEN`, minted as `service:kobo` per kobo docs/operating.md and the stack's dogfood manifest, and cabal's re-mint copies the caller's sub verbatim (cabal routes/_dispatch_auth.py); a human operator is `operator:<id>` (cabal routes/auth.py bootstrap). No scope claim is checked: the re-mint sets scope to ["dispatch"] for every caller. `<lane>_allowed`/`<lane>_refused` carry the sub. Keep the event names: kaminos consumes them; cli.py _OPERATOR_GATE_EVENTS matches them by exact name (#49).
- The tool lane writes Python the next boot runs as the agent; memory/tooling.py is a lint, not a sandbox (#195).
- Reason codes (ReasonCode: 700 human input, 701 policy, 702 budget, 703 tool blocked, 704 escalation) are written to BOTH metadata daedalus_reason_code and cabal_reason_code, so cabal reads them untranslated. Machines key on state + code, never on reply text.
- Metadata rides through verbatim (cabal_trace_id etc. must come back).
- Two inbound metadata keys change behaviour. `daedalus_recall` (task.RECALL_KEY) replaces the prompt as the memory-recall query when it is a non-blank string. `cabal_delegation_depth` (tools/colleagues.py DEPTH_KEY) ≥ 1 means another agent asked: Agent._loop then withholds colleagues.ask_colleague and adds a note to the prompt, so the agent cannot ask onward (#149). An unreadable depth reads as 0.
- Error bodies are readable JSON (400/401/422/500): cabal wraps >=400 text into DispatchError, so it is what its operator sees. JSON-RPC errors use -32700/-32601/-32602/-32000, never HTTP 500.
- With `auth` configured, GET /events needs the same bearer token as the POSTs (401 + `WWW-Authenticate: Bearer` without one, #196); the card and /healthz stay open. With no `auth` block every route is open, /events included. Default bind 127.0.0.1:9200.
- /events redacts on the wire, not on the bus: tool_call `arguments`, tool_result `content` and reasoning `text` go out as `<field>_chars` sizes unless DAEDALUS_TRACE_CONTENT=1 (a2a.wire_event). The in-process bus keeps content, because the Slack transport's progress lines and `run -v` read it there.
- Images are NOT carried on the wire (task_to_a2a is text only).
- The switchboard's own /a2a/tasks takes a routing envelope {target, task}; this one takes a bare Task. Same path, different dialect.
- Task store is an in-process dict: tasks/get and taskId continuation do not survive restart.
CONSUMERS: cabal (dispatch + JWT mint + reason codes), kobo (operator lanes via cabal's re-mint; mirrors teach's filename regex), kaminos (/events; its proxy, kaminos_api/agents.py event_stream, sends no token, so the trace pane only works against bundles without `auth`, the same limit its test chat already has; it reads `arguments`/`content`/`text`, which are now absent unless DAEDALUS_TRACE_CONTENT=1), tools/colleagues.py (daedalus agents asking each other through the switchboard). Tests: tests/test_transport_a2a.py, test_transport_a2a_auth.py, test_agent_teach.py (techne-gated).
API: build_a2a_app, A2aTransport(card, host, port, events, verifier), task_to_a2a, task_from_a2a, stamp_verified_sub; task.{Task, Message, TaskState, ReasonCode, REASON_KEY, CABAL_REASON_KEY, VERIFIED_SUB_KEY, RECALL_KEY}.
