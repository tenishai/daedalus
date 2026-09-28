---
card: repo-card:daedalus
updated: 2026-09-28
anchor: pyproject.toml sha=d3ceb61a719ae99dccc4d2d1074fd77aa16470e7
anchor: src/daedalus/agent.py sha=33ead63ad6346314bb61b03203c17ef3b8d7e262
---
# daedalus — repo card

WHAT: the agent runtime. An agent is a directory (bundle: agent.yaml + persona.md + agent-card.json, optional skills/, tools/, knowledge/); daedalus loads it and serves it over CLI / A2A / Slack, with tool allowlists, approval gates, enforcement hooks, and optional techne memory.
STATE: main, v0.4.0 (version lives in src/daedalus/__init__.py; pyproject reads it via [tool.hatch.version]). Active: the ENGINE seam (PRs #104 LiteLLMEngine, #105 withhold unapprovable tools, #107 loopback MCP bridge, #108 Claude Code engine on claude-agent-sdk, all merged 2026-09-27). Before that: agent-loop limits (#99 wrap_up, #100 max_repeats; 2026-09-26).
STACK: Python ≥3.11, hatchling. Base deps ONLY pydantic, pyyaml, click, litellm. Extras: mcp, claude-code (mcp≥2.2, uvicorn, claude-agent-sdk≥0.2.160), a2a (fastapi, uvicorn, joserfc, httpx), slack, aws (boto3), techne (install via requirements-sources.txt; PyPI "techne" is unrelated), dev. Runtime image: docker/Dockerfile (techne + liminal as named build contexts; extras a2a,slack,aws — NOT claude-code).
ENTRY:
- CLI `daedalus` → src/daedalus/cli.py:main (version, schema, create, run, chat; run/chat take --engine)
- library → daedalus.Agent (src/daedalus/agent.py), Agent.load(path) / handle_task / serve
- container → docker/entrypoint.sh: docker/seeder.py seeds bundle knowledge+skills into techne, then `daedalus run /bundle -v`

SEAMS → card:
- engines     src/daedalus/engines/          (Engine/EngineTurn; litellm loop, claude-code, MCP bridge) → docs/arch/engines.md
- protocols   src/daedalus/protocols.py      (LLM/MemoryStore/ToolProvider/... + no-op defaults)     → docs/arch/protocols.md
- approvals   agent.py _call_tool + enforcement.py (tier-1 allowlist, tier-2 gates, law hooks)    → docs/arch/approvals.md
- a2a-wire    src/daedalus/transports/a2a.py + task.py (cabal/kobo/kaminos wire, operator lanes)   → docs/arch/a2a-wire.md
- bundle      src/daedalus/bundle.py         (AgentSpec = agent.yaml schema, drift-pinned JSON Schema) → docs/arch/bundle.md
- (uncarded) transports/slack.py (Socket Mode; reaction approver; one transport for all slack blocks); transports/cli.py
- (uncarded) tools/*: builtin files.*, skills.load, mcp (stdio), liminal (gateway), colleagues (switchboard A2A asks), github/jira (room brokers, mint-per-call tokens), cloudtrail, slack_read; compose.py MultiToolProvider (first name wins)
- (uncarded) memory/: techne.py (MemoryStore over techne), teach.py (operator lanes), journal.py (7-day journal.db), learnings.py, revision.py (opt-in belief revision), tooling.py (port of kobo's tool-file validator)
- (uncarded) events.py EventBus (trace stream; -v and /events SSE); checkpoints.py SqliteCheckpoints; llm.py LiteLLMClient; budget.py; scaffold.py

DEPS: no hard runtime dep on any sibling. Optional: techne (memory.personal: techne; docker/seeder.py), liminal (tools.provider liminal or tools.liminal.url). Called by cabal (POST /a2a/tasks, JWT from cabal JWKS, reason codes in metadata.cabal_reason_code), kobo (composes bundles, drives teach/knowledge/tool/learnings lanes through cabal), kaminos (GET /events SSE). tenishai/agents#51 is the engine-seam design issue.
HEALTH: 40 test files (tests/ + docker/test_seeder.py); 710 collected. Local run 2026-09-28: 698 passed, 15 skipped (techne absent). CI .github/workflows/test.yml: `test` on a SELF-HOSTED runner (3.11 dev; 3.12 dev,claude-code), ruff + pytest; `fork-pr` hosted complement; `techne` leg workflow_dispatch only, DAEDALUS_REQUIRE_TECHNE=1. No branch protection: CI reports, never blocks.
GOTCHAS:
- Never add a module-scope import of an optional extra (fastapi, slack_sdk, mcp, claude_agent_sdk, techne, liminal). Dev env has them all, so CI won't catch it.
- The three techne-path test files (92 tests) collapse to 3 `s` under importorskip. A green default CI run did NOT run them. Use `pytest -rs`.
- `enforcement.py` is the hook LAW layer (bundle/tools/*.py pre_task/post_draft), NOT the three-tier approval model and NOT LLM tools. Tiers 1-2 live in agent.py. AGENTS.md's directory map says otherwise; the code wins.
- `bundle/tools/` = enforcement hooks; `tools:` in agent.yaml = LLM tool providers. Same word, unrelated.
- Engine choice is the OPERATOR's (--engine / DAEDALUS_ENGINE), never the bundle's; the bundle only carries engine.claude_code settings.
- `approvals.ceiling` (write/hosts) is parsed but read nowhere; docs/design.md §3 says it is enforced. It is not.
- No code sets TaskState.INPUT_REQUIRED; design.md's "task parks in input-required" is not implemented.
- AGENTS.md cites `_OPERATOR_GATE_EVENTS` at cli.py:85; it is at cli.py:92.
- `make image` passes only `--build-context techne`; docker/Dockerfile also needs `liminal`, so the target as written lacks a context the build uses.
- docs/bundle.schema.json must be regenerated (`daedalus schema --out docs/bundle.schema.json`) with any AgentSpec change, or test_schema fails.
RELATED DOCS: AGENTS.md (traps, build/test), docs/design.md (rationale; predates the code), docs/proposals/capability-seam.md (proposal, not behaviour), SECURITY.md.
