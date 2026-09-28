---
card: arch-card:daedalus-bundle
updated: 2026-09-28
anchor: src/daedalus/bundle.py sha=ddd37d65370b5af2ecca5e1af754e5502726221c
anchor: docs/bundle.schema.json sha=666a904dcac6ce70789b49dcbd721ee9fcdcebfd
---
# bundle — agent.yaml schema and the bundle directory

WHAT / SEAM: `Bundle.load(path, env=None)` (bundle.py) → Bundle(path, spec: AgentSpec, card: dict, persona: str, skills: dict[str, Skill]). `AgentSpec` (pydantic) is the agent.yaml schema: name, card, persona, model (ModelSpec: id, max_tokens, api_base, api_key_env, budget.per_task_tokens), transports[], tools (ToolsSpec), memory (MemorySpec), checkpoints, approvals (ceiling, gates, escalation), engine.claude_code (ClaudeCodeSpec). `bundle_schema()` emits it as JSON Schema → docs/bundle.schema.json.
Directory: agent.yaml, persona.md, agent-card.json (required); skills/<id>/SKILL.md (frontmatter when_to_use, requires); tools/*.py (enforcement hooks, not LLM tools); knowledge/*.md and intake/* (seeded into techne by docker/seeder.py, not by the runtime).
OPS / FLOW: read agent.yaml → `interpolate` ${VAR} / ${VAR:-default} recursively (undefined without default → BundleError) → AgentSpec.model_validate → require card + persona files → strip persona comments → load_skills. Agent then builds collaborators from spec (see protocols card).
INVARIANTS:
- Unknown fields load with a WARNING, never an error and never silently (`_WarnExtra`): forward-compat with newer writers (kobo, kaminos).
- docs/bundle.schema.json is drift-pinned: tests/test_schema.py::test_committed_schema_cannot_drift_from_the_runtime. Any AgentSpec change needs `daedalus schema --out docs/bundle.schema.json` in the same commit. The schema is the contract for non-Python readers (cabal's bundle reader, editors).
- `tools.allow` empty = deny everything. `tools.provider` ∈ none|mcp|liminal|colleagues picks ONE primary; github, cloudtrail, slack_read, jira, liminal.url are SIDECARS that stack beside it (agent.py _tools_from_spec). `slack_read.channels: []` = lane ON for every invited channel; omitted = OFF (2026-09-23).
- Which engine runs a bundle is NOT in the bundle; `engine.claude_code` only configures that engine. Same bundle must run with and without Claude Code.
- `memory.revision` unknown value → warn and disable; absent = the revision module is never imported.
- A skill whose `requires:` the allowlist doesn't admit, or skills without `skills.load` allowed, warn at load (unmet_requirements); they don't fail.
- Slack transport: `channel` key present but empty → BundleError (a typo must not open the widest reach); key omitted → every invited channel. All slack blocks fold into ONE SlackTransport (Socket Mode delivers each event to one connection).
- Golden bundles tests/golden/<name>/ are fixtures AND worked examples; new format features want one.
CONSUMERS: agent.py (Agent.load, _tools_from_spec, transports()); cli.py (`schema`, `create` via scaffold.py); kobo composes bundles (kobo_api/steps.py) and pushes them as repos; cabal reads bundles via the JSON Schema; docker/seeder.py reads knowledge/, skills/, intake/.
API: Bundle, AgentSpec (+ nested *Spec classes), BundleError, bundle_schema(), interpolate(), load_skills(), unmet_requirements(), strip_persona_comments().
