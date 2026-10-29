---
card: arch-card:daedalus-bundle
updated: 2026-10-29
anchor: src/daedalus/bundle.py sha=bf4a8948fd19ed7c3c6db45dc50b440d0cb6ef14
anchor: docs/bundle.schema.json sha=b38a7040448a51ac31df5438a93988f0124de6b4
---
# bundle — agent.yaml schema and the bundle directory

WHAT / SEAM: `Bundle.load(path, env=None)` (bundle.py) → Bundle(path, spec: AgentSpec, card: dict, persona: str, skills: dict[str, Skill]). `AgentSpec` (pydantic) is the agent.yaml schema: name, card, persona, model (ModelSpec: id, max_tokens, request_timeout (s per model call, default 120, litellm engine only, #201), api_base, api_key_env, budget.per_task_tokens; id is required and is either a model or a tier word, fast|balanced|best), transports[], tools (ToolsSpec), memory (MemorySpec), checkpoints, approvals (ceiling, gates, escalation), engine.claude_code (ClaudeCodeSpec), task_timeout (s per engine run, default 600, #201). `bundle_schema()` emits it as JSON Schema → docs/bundle.schema.json.
Directory: agent.yaml, persona.md, agent-card.json (required); skills/<id>/SKILL.md (frontmatter when_to_use, requires); tools/*.py (enforcement hooks, not LLM tools); knowledge/*.md and intake/* (seeded into techne by docker/seeder.py, not by the runtime).
OPS / FLOW: read agent.yaml → `interpolate` ${VAR} / ${VAR:-default} recursively (undefined without default → BundleError) → AgentSpec.model_validate → require card + persona files → strip persona comments → load_skills. Agent then builds collaborators from spec (see protocols card).
INVARIANTS:
- Unknown fields load with a WARNING, never an error and never silently (`_WarnExtra`): forward-compat with newer writers (kobo, kaminos).
- docs/bundle.schema.json is drift-pinned: tests/test_schema.py::test_committed_schema_cannot_drift_from_the_runtime. Any AgentSpec change needs `daedalus schema --out docs/bundle.schema.json` in the same commit. The schema is the contract for non-Python readers (cabal's bundle reader, editors).
- `tools.allow` empty = deny everything. `tools.provider` ∈ none|mcp|liminal|colleagues picks ONE primary; github, cloudtrail, slack_read, jira, liminal.url are SIDECARS that stack beside it (agent.py _tools_from_spec). `cloudtrail` is ON when `event_data_store` plus either `role_arn` (the pod assumes) or `bab_url`+`bab_integration` (a one-hour credential leased from the room's permission service with the token at `bab_token_file`) are set; `CloudTrailSpec.on` is the one predicate (2026-10-29). `slack_read.channels: []` = lane ON for every invited channel; omitted = OFF (2026-09-23).
- Which engine runs a bundle is NOT in the bundle; `engine.claude_code` only configures that engine. Same bundle must run with and without Claude Code.
- `memory.revision` unknown value → warn and disable; absent = the revision module is never imported.
- A skill whose `requires:` the allowlist doesn't admit, or skills without `skills.load` allowed, warn at load (unmet_requirements); they don't fail.
- Slack transport: `channel` key present but empty → BundleError (a typo must not open the widest reach); key omitted → every invited channel. All slack blocks fold into ONE SlackTransport (Socket Mode delivers each event to one connection).
- model.id as a tier (#127, #129) resolves at Agent construction from the deployment's tiers file ($DAEDALUS_TIERS, tiers.py): the entry fills model.id, the endpoint fields, max_tokens/request_timeout, context_tokens/max_repeats and the claude-code model, never overriding a value the bundle set; no tiers file → BundleError. The old model.tier/min_tier keys are refused with a pointer to model.id.
- tools.builtin groups are `files` and `exec`; exec loads only with approvals.ceiling.exec: true (agent.py), anything else is a BundleError. The field stays an untyped dict (agent.py passes each block as provider kwargs); its schema `description` is where an author sees the refused files roots (tools/builtin.py _refused_root, #199). request_timeout, salvage_text_calls and task_timeout carry descriptions too.
- tools.salvage_text_calls (#200): None = on only when model.api_base is set; salvage reads only JSON inside <tool_call>/<tools> tags (engines/litellm.py).
- Fields validated at load so a typo fails the bundle: slack `approvers` (list of U…/W… user ids), slack `wake` (mention|thread|channel; every slack block must agree), github.branch_prefix (a valid git ref component, default "daedalus"), memory.team (only "none"; "outbox" is refused because it was never built).
- The schema lets `${VAR}` / `${VAR:-default}` placeholders stand in for enum, integer, number and boolean leaves (#132), so an editor accepts what interpolate() will expand.
- Golden bundles tests/golden/<name>/ are fixtures AND worked examples; new format features want one.
- Both timeouts (#201) are positive numbers with no "off" value: a bundle can raise them, never remove them. The greeter golden sets both. kobo vendors docs/bundle.schema.json, so these two fields reach it only when it re-vendors.
CONSUMERS: agent.py (Agent.load, _tools_from_spec, transports()); cli.py (`schema`, `create` via scaffold.py); kobo composes bundles (kobo_api/steps.py) and pushes them as repos; cabal reads bundles via the JSON Schema; docker/seeder.py reads knowledge/, skills/, intake/.
API: Bundle, AgentSpec (+ nested *Spec classes), BundleError, bundle_schema(), interpolate(), load_skills(), unmet_requirements(), strip_persona_comments().
