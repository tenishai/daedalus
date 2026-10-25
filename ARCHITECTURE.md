---
card: repo-card:daedalus
updated: 2026-10-24
anchor: pyproject.toml sha=fafaad90863050c4ab8cf5f4c033cbc8d0a633b7
anchor: src/daedalus/agent.py sha=679dccfa7d5aac1e84b93fa23c54cd06c4ae33b5
---
# daedalus — repo card

WHAT: the agent runtime. An agent is a directory (bundle: agent.yaml + persona.md + agent-card.json, optional skills/, tools/, knowledge/); daedalus loads it and serves it over CLI / A2A / Slack, with tool allowlists, approval gates, enforcement hooks, and optional techne memory.
STATE: main, v0.5.5 (2026-10-15; version lives in src/daedalus/__init__.py; pyproject reads it via [tool.hatch.version]); everything since is under CHANGELOG [Unreleased]. 0.5.5 (#185) ends the `## Clock` section with an instruction to state today's date before reasoning about a relative date; 0.5.4 (#182) adds `Yesterday:` and `Past week:` lines to the `## Clock` section; 0.5.3 (#176) names the weekday in that clock line and gives it its own `## Clock` heading ahead of the skills index; 0.5.2 (#172) put the current UTC time in every task's system prompt; 0.5.1 (#166) made learnings candidates from the room's chat carry `said_by: "user"` and dropped unverified attributions. 0.5.0 renamed the distribution to tenish-daedalus (it shipped as `daedalus` through v0.4.0). In 0.5.0, since 2026-09-28: model tiers (#127, #129, #132: model.id may name fast|balanced|best, resolved from $DAEDALUS_TIERS), the exec builtin (#121), Claude Code engine in the runtime image (#112), go-public prep (#138 community files; DCO check in CI), Slack approver allowlist (#141) and wake modes (#147), per-answer colleague ask limits (#150), colleague and liminal refusals reported as refusals (#157, #158, #159), Slack progress lines (#160). Before that, the ENGINE seam (#104, #105, #107, #108; 2026-09-27).
STACK: Python ≥3.11, hatchling; the distribution is `tenish-daedalus`, the import package and CLI are `daedalus`. Base deps ONLY pydantic, pyyaml, click, litellm (pinned exactly, ==1.104.2, after a compromised litellm release on PyPI; #193). Extras: mcp, claude-code (mcp≥2.2, uvicorn, claude-agent-sdk≥0.2.160), a2a (fastapi, uvicorn, joserfc, httpx), slack, aws (boto3), techne (`tenish-techne[vectors]>=0.18.2`; install via requirements-sources.txt, which pins the v0.18.2 tag), dev. Runtime image: docker/Dockerfile (techne + liminal, as tenish-techne and tenish-liminal >=0.4.0, from sibling build contexts or a package index, #128; extras a2a,slack,aws,claude-code).
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
- (uncarded) tiers.py (the deployment's tiers file: tier word → model, endpoint, engine, limits)
- (uncarded) tools/*: builtin files.* and exec.run (exec.py; a command in the agent's workspace, needs approvals.ceiling.exec: true), skills.load, mcp (stdio), liminal (gateway), colleagues (switchboard A2A asks; per-answer limits and no onward asks when answering a colleague, #149), github/jira (room brokers, mint-per-call tokens), cloudtrail, slack_read; compose.py MultiToolProvider (first name wins; a provider raising ToolProviderRefused is skipped for the turn, #156)
- (uncarded) memory/: techne.py (MemoryStore over techne), teach.py (operator lanes; teach_authorized is an allowlist, `auth.operator_subs`, #195), journal.py (7-day journal.db, one row per (context_id, verified sub), #212), learnings.py, revision.py (opt-in belief revision), tooling.py (port of kobo's tool-file lint; not a sandbox: a delivered file runs as the agent at next boot, #195)
- (uncarded) events.py EventBus (trace stream; -v and /events SSE); checkpoints.py SqliteCheckpoints; llm.py LiteLLMClient; budget.py; scaffold.py

DEPS: no hard runtime dep on any sibling. Optional: techne (memory.personal: techne; docker/seeder.py), liminal (tools.provider liminal or tools.liminal.url). Called by cabal (POST /a2a/tasks, JWT from cabal JWKS, reason codes in metadata.cabal_reason_code), kobo (composes bundles, drives teach/knowledge/tool/learnings lanes through cabal), kaminos (GET /events SSE; bearer token required when the a2a transport has `auth`, content fields redacted unless DAEDALUS_TRACE_CONTENT=1, #196). tenishai/agents#51 is the engine-seam design issue.
HEALTH: 47 test files (tests/ + docker/test_seeder.py); 926 collected. mash1 run 2026-10-24: 912 passed, 20 skipped (techne absent). CI .github/workflows/test.yml: `test` on a SELF-HOSTED runner pool `daedalus-ci` (3.11 dev; 3.12 dev,claude-code), ruff + scripts/check_arch_cards.py + pytest (#206); `fork-pr` hosted complement, same steps; `dco` (every commit a PR adds is signed off, hosted); `techne` leg workflow_dispatch only, DAEDALUS_REQUIRE_TECHNE=1. No branch protection: CI reports, never blocks.
GOTCHAS:
- Never add a module-scope import of an optional extra (fastapi, slack_sdk, mcp, claude_agent_sdk, techne, liminal). Dev env has them all, so CI won't catch it.
- The three techne-path test files (92 tests) collapse to 3 `s` under importorskip. A green default CI run did NOT run them. Use `pytest -rs`.
- `enforcement.py` is the hook LAW layer (bundle/tools/*.py pre_task/post_draft), NOT the three-tier approval model and NOT LLM tools. Tiers 1-2 live in agent.py. AGENTS.md's directory map says otherwise; the code wins.
- `bundle/tools/` = enforcement hooks; `tools:` in agent.yaml = LLM tool providers. Same word, unrelated.
- Engine choice is the OPERATOR's (--engine / DAEDALUS_ENGINE), never the bundle's; the bundle only carries engine.claude_code settings.
- Every engine run has a deadline (#201): Agent._loop wraps it in asyncio.timeout(spec.task_timeout, default 600 s) and a run past it fails 704; the litellm engine also passes model.request_timeout (default 120 s) to litellm, whose own default is 6000 s. The cancellation reaches a running tool call only because nothing on the call path catches BaseException: keep Agent._call_tool's `except Exception`, and don't add `except BaseException` to a tool provider. A tool that runs in a thread (asyncio.to_thread, e.g. cloudtrail) is abandoned, not stopped.
- `approvals.ceiling.write` and `.hosts` are parsed but read nowhere; don't describe them as controls. Only `ceiling.exec` is read, at load, where it gates the exec builtin (docs/design.md §3 says the same).
- No code sets TaskState.INPUT_REQUIRED and nothing emits reason 700; escalation ends a task FAILED with 704 (design.md §3 says so since #203).
- AGENTS.md cites `_OPERATOR_GATE_EVENTS` at cli.py:85; it is at cli.py:92.
- docs/bundle.schema.json must be regenerated (`daedalus schema --out docs/bundle.schema.json`) with any AgentSpec change, or test_schema fails.
RELATED DOCS: AGENTS.md (traps, build/test), docs/design.md (rationale; predates the code), docs/proposals/capability-seam.md (proposal, not behaviour), SECURITY.md.
