---
card: arch-card:daedalus-protocols
updated: 2026-11-21
anchor: src/daedalus/protocols.py sha=da94bab9e7e465aefc17a8ff3282a6166fd57144
---
# protocols — the collaborator seams optional extras implement

WHAT / SEAM: src/daedalus/protocols.py, all `runtime_checkable` Protocols:
- `LLM.complete(messages, tools=None) -> LLMResponse` (tools OpenAI function-shaped; model/max_tokens are construction config, not call args)
- `Handler.handle_task(Task) -> Task` — the entire agent contract; transports only see this
- `Transport.serve(handler)`
- `MemoryStore.recall(query, k=5) -> list[str]`, `.commit(content, kind="fact", tags="")`; optional, unannotated: `.recall_hits(query, k) -> list[{content, source}]` and `.publish() -> dict`, called once at task end, from handle_task or resume, by Agent._publish_memory (#228)
- `ToolProvider.tools() -> list[dict]`, `.call(name, payload) -> str`; `.call` may return a `ToolResult` (a str subclass with `.outcome`: ok, empty, error, blocked) to say how the call ended, and may raise `ToolCallBlocked` when a server refuses that one call (a policy refusal, or for liminal also a token that did not authenticate); Agent._call_tool turns both into the result's outcome (#268). `tools()` raises `ToolProviderRefused` when the provider's server answered and refused this caller (#156), as distinct from an outage. Optional class attribute `knowledge_only = True` (read with getattr, not a Protocol member): the provider answers from the agent's own material with no credential, so its tools stay offered while answering a colleague (kobo#310); default credentialed, so a new provider is withheld there until it opts in. Only SkillsToolProvider sets it.
- `CheckpointStore.save/load/drop(task_id)`; `Budget.spend(tokens)`, `.remaining() -> int|None` (None = unlimited)
- `Approver.approve(action, plan) -> bool`
Data: `ToolCall(id, name, arguments)` (name = exactly what the model said), `LLMResponse(text, reasoning, tool_calls, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, finish_reason)`; every count is `int | None`, None = the provider did not report it (#268). `tokens_spent()` counts None as 0 for a budget; LiteLLM's Anthropic input_tokens already includes the cache counts.
Defaults: NoMemory, NoTools (call raises LookupError), NoCheckpoints, NoBudget, AllowAll (explicit opt-out only), DenyAll (THE default approver).
OPS / FLOW: Agent.__init__ (agent.py) takes each collaborator as a kwarg; None → built from the bundle spec (_memory_from_spec, _tools_from_spec, _checkpoints_from_spec, _budget) or the no-op default. Implementations: LiteLLMClient (llm.py), TechneMemoryStore (memory/techne.py), SqliteCheckpoints (checkpoints.py), TokenBudget (budget.py), tool providers (tools/*.py, composed by tools/compose.py MultiToolProvider), CliTransport/A2aTransport/SlackTransport, Slack reaction approver (transports/slack.py).
INVARIANTS:
- A missing backend is a no-op default injected at wiring, never None-checked in the hot path.
- DenyAll is the default approver: gates fail closed until something wires a real one (CLI chat at construction; SlackTransport._wire_approver). `isinstance(approver, DenyAll)` is how the Agent decides a gated tool is unapprovable (agent.py _unapprovable) — a custom always-False approver is NOT detected.
- An extra's backend is imported lazily at the point of use, and a configured-but-missing extra raises BundleError at load, not mid-conversation.
- Memory recall failure degrades to [] (memory/techne.py); a broken memory backend never takes the agent down.
- Tool provider errors become tool-result text (Agent._call_tool "tool error: …"); a down provider at listing time → FAILED 704, not a transport 500.
CONSUMERS: agent.py (all of them); engines/base.py (Budget, CheckpointStore in EngineTurn); transports/* (Handler); memory/revision.py and memory/learnings (LLM); cabal/kobo reach these only through the a2a wire, not by import.
API: the Protocols above plus the no-op classes; AGENTS.md names this file the authority on seams.
