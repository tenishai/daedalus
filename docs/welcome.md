# Welcome to daedalus

daedalus turns a directory into a running agent: a persona, an A2A agent card and an
`agent.yaml` wiring file. It loads that bundle, runs each turn on a model, and serves the
agent over a terminal chat, an A2A HTTP endpoint or Slack, with tool allowlists and
approval gates around every tool call.

This guide is written for an agent explaining daedalus to its human. It ships inside the
package, so `daedalus guide welcome` prints the copy that matches the installed version.

## Where it fits

daedalus runs on its own. Its base dependencies are pydantic, pyyaml, click and litellm;
every tenish project below is optional.

- **techne** (optional): personal memory. `memory.personal: techne` recalls from and
  commits to a techne store. Needs the `techne` extra.
- **liminal** (optional): tool gateway. `tools.provider: liminal`, or `tools.liminal.url`
  beside another provider, reaches liminal's tools over MCP. Needs the `tenish-liminal`
  package installed on its own; there is no daedalus extra for it.
- **cabal** (optional): orchestration. cabal sends tasks to the a2a transport with JWTs it
  mints, and its switchboard is what `tools.provider: colleagues` talks to.
- **kobo** (optional): composes bundles and drives the operator lanes (teach, knowledge,
  tool, learnings) through cabal.

Nothing in daedalus imports cabal or kobo; they call it.

## What it can do for you

- **Make an agent in one command.** `daedalus create prudence` writes `agent.yaml`,
  `persona.md` and `agent-card.json`. Edit the persona, then `daedalus chat ./prudence`.
- **Talk to it locally.** `daedalus chat <bundle>` is a REPL. A human is present, so
  tier-2 gates ask at the terminal.
- **Get one answer from a script or CI.** `daedalus run <bundle> -m "text"` prints the
  reply and exits 1 if the task failed. A `[usage]` line with token counts goes to stderr
  after each run.
- **Serve it.** `daedalus run <bundle>` serves every transport in `transports:` (a2a,
  slack). `-v` traces tool calls, results, reasoning and model resolution.
- **Give it tools without code.** Built-in read-only file tools, a command runner, MCP
  servers, a liminal gateway, colleague agents, and room brokers for GitHub and Jira, all
  set in `agent.yaml` and filtered by `tools.allow`.
- **Validate a bundle in an editor or another language.** `daedalus schema` prints the
  agent.yaml JSON Schema; the repo commits it as `docs/bundle.schema.json`.
- **Teach it skills.** `skills/<id>/SKILL.md` files are listed in the system prompt and
  loaded on demand with the `skills.load` tool (allow it in `tools.allow`). Memory then
  holds a short card per skill, so recall can say which skill fits a question without
  pasting the whole skill into every prompt.
- **Give it rules it always follows.** `guardrails.md` holds one rule per line, and every
  task's system prompt carries them right after the persona. An operator can replace the
  list on a running agent; the next task uses it, with no restart. Rules guide the model.
  To make sure something never happens, use the allow list, a gate or a law hook.

## Options

### Engine: litellm or claude-code

The operator picks the engine with `--engine` or `DAEDALUS_ENGINE`, never the bundle; the
same bundle must run either way. A model tier can also name the engine (below).

- **`litellm`** (default): daedalus's own loop, over any model litellm routes (Anthropic,
  OpenAI-compatible endpoints such as vLLM or ollama). Reach for it when: you want
  checkpoints, token budgets, `on_exhausted`, `context_tokens` and `max_repeats`, or a
  self-hosted model.
- **`claude-code`**: Claude Code is the agent, on Anthropic's Agent SDK. The bundle's
  tools reach it through a loopback MCP server inside the daedalus process, so the
  allowlist and gates still apply. Reach for it when: you want Claude Code's own loop and,
  if you list them in `engine.claude_code.builtin_tools`, its built-ins (Read, Grep,
  Bash). Cost: checkpoints, `on_exhausted`, `context_tokens` and `max_repeats` are not
  honoured, `model.budget.per_task_tokens` is refused at load, and Claude Code approves
  some built-in calls itself (read-only ones, its permission mode, the agent's own allow
  rules) without reaching daedalus's gates. Leaving a built-in out of `builtin_tools` is
  the only way to be sure it can't run. Needs the `claude-code` extra.

### Model: a model id or a tier

`model.id` is a litellm route string (`anthropic/claude-sonnet-4-5`, `openai/<name>`) or
one of the tier words `fast`, `balanced`, `best`. A tier is looked up in the YAML file
named by `DAEDALUS_TIERS`, which maps each tier to an engine, a model and its limits once
per deployment, `max_tokens` and `thinking` among them, so a deployment raises the reply
ceiling for every bundle on a tier (16384 for a reasoning model, say) without an edit to
the bundles. Reach for a tier when: the same bundle runs in several deployments with
different models. Cost: with no tiers file, or a tier the file doesn't offer, the bundle
refuses to load; an `--engine` that disagrees with the tier's engine is refused too.
`model.api_base` and `model.api_key_env` point at an OpenAI-compatible endpoint.

### Tools

`tools.provider` picks one primary provider:

- **`none`** (default): no remote tools. Reach for it when: the agent only talks, or uses
  built-ins and sidecars.
- **`mcp`**: stdio MCP servers listed in `tools.servers`, named `server.tool`. Reach for it
  when: an MCP server already does the job. Needs the `mcp` extra.
- **`liminal`**: the liminal gateway at `tools.liminal.url`. Reach for it when: tools and
  their credentials should live in a gateway, not in the agent. Needs `tenish-liminal`.
- **`colleagues`**: `colleagues.list_colleagues` and `colleagues.ask_colleague` against a cabal switchboard at
  `tools.colleagues.url`. Reach for it when: this agent should ask other agents. An agent
  answering a colleague's question answers from what it knows and its skills: it is offered
  no credentialed tools for that task, cannot ask onward, and says which tool the answer
  would need so the asker's owner can request it.

Built-in groups, under `tools.builtin`, stack beside the provider:

- **`files`** (`roots: [...]`): `files.read`, `files.list`, `files.search`, read-only and
  confined to the roots. A root that is `/`, at or under `/proc`, `/sys`, `/dev`, `/run` or
  `/etc`, `$HOME` or above it, or inside `$TECHNE_HOME` fails the bundle at load.
- **`exec`**: `exec.run`, an argv command in the agent's workspace with an optional
  `commands` allowlist, a timeout and capped output. Loads only with
  `approvals.ceiling.exec: true`. Cost: the command runs as the agent's user and network
  egress is not controlled, so it is not a sandbox for untrusted code.

Sidecar blocks also stack beside the provider when configured: `github` and `jira` (room
brokers), `cloudtrail` (needs the `aws` extra), `slack_read`, and `liminal.url` when
liminal is not the primary. A `kind: slack` transport block adds `slack.post_message`,
`slack.offer_choices`, `slack.pins` and `slack.bookmarks` once the bot token is set. `audit_trace` adds one tool, `audit.trace`, that runs an
access-provenance audit in a fixed order over the agent's own `cloudtrail`, `slack_read`
and gateway issue-tracker tools; it needs those two blocks on.

`tools.allow` is the ceiling: fnmatch patterns, and an empty list means no tools at all.

### Approvals: three tiers

1. **Ceiling.** `tools.allow` is checked when tools are listed and again at call time, so
   the model never sees a tool it can't call.
2. **Gates.** `approvals.gates: [{match: "github.open_pr"}]` sends a matched call, with
   its full arguments, to an approver. `chat` asks at the terminal; Slack asks by reaction
   from a listed `approvers` user; headless `run` and a2a deny, and a tool that would always
   be denied is withheld from the model. Reach for it when: a tool should run only after a
   person says yes. Cost: arguments are shown verbatim, secrets included.
3. **Escalation.** A task the runtime can't finish (deadline, provider down, engine error)
   ends failed with reason 704, and the transport carries that to a human:
   `approvals.escalation.route` (`slack:<channel>` for Slack), or cabal reads the code.

`approvals.ceiling.write` and `.hosts` are parsed but enforced nowhere.

### Transports: cli, a2a, slack

- **`cli`**: always available through `daedalus chat`.
- **`a2a`**: an HTTP endpoint (default `127.0.0.1:9200`) serving the agent card and taking
  tasks; this is what cabal calls. Needs the `a2a` extra. Auth: add
  `auth: {jwks_url, issuer, audience}` to verify cabal-minted JWTs on the POST routes and
  `/events`; `auth.operator_subs` (default `["operator:*", "service:kobo"]`) says who may
  use the operator lanes. Without `auth` the boot line says NO AUTH, and off loopback a
  warning goes to stderr. Reach for it when: other services or agents send it work.
- **`slack`**: with `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`, Socket Mode. With the bot
  token alone, no socket: Slack's events arrive at `POST /slack/events` on the `a2a`
  app, forwarded by a relay that proves itself with the `X-Kobo-Relay-Secret` header
  matching `SLACK_RELAY_SECRET` (so the bundle needs an `a2a` block too). The boot
  line says which way in is active. `channel` limits where it answers (none listed =
  every invited channel), `wake` is `mention`, `thread` or `channel`, and `approvers`
  lists who may approve gates. Reaction approvals are wired only with both a `channel`
  allowlist and `approvers`. Needs the `slack` extra. Reach for it when: the agent's
  users are in Slack.

### Memory

- **`memory.personal: none`** (default): no recall. The agent still runs.
- **`memory.personal: techne`**: recalls up to five entries from the agent's own techne
  knowledge into each prompt, plus up to five from the team pool when a recall source
  such as techne-archivist-sync is installed. At the end of each task it publishes knowledge
  carrying one of techne's `archivist_publish_tags`; with that list empty, the default,
  nothing leaves the agent. `memory.home` sets the store directory and its config.yaml;
  without it the store is techne's own default (`$TECHNE_HOME`, else `~/.techne`), so set
  one or the other whenever more than one agent shares a host. Reach for it when: the
  agent should remember across conversations, or kobo will teach it. Needs the `techne`
  extra.
- **`memory.revision: operator`** (opt-in): a background pass where the agent's model
  judges techne's contradiction candidates. A correction the operator taught is promoted;
  one a colleague asserted is promoted only at two agrees (the asserter counts as one, and
  up to three same-team colleagues are asked), and any disagreement holds it. Everything else stays pending for a human.
- **`checkpoints: sqlite`**: saves loop state under `~/.cache/daedalus/<name>/` so a task
  can resume (litellm engine only).

### Text tool-call salvage (vLLM and similar)

Some self-hosted models write tool calls as `<tool_call>` text instead of native calls
(Qwen on vLLM does). `tools.salvage_text_calls: true` runs JSON found inside `<tool_call>`
or `<tools>` tags. Unset, it is on only when `model.api_base` is set in `agent.yaml`; the
`OPENAI_API_BASE` env var does not count. Cost: salvage skips the model's own decision to
call, so a reply that quotes tagged JSON runs it. For native tool calls instead, vLLM must
be started with its tool-call parser enabled; see vLLM's tool-calling docs for the flags.

### Timeouts and limits

- **`task_timeout`** (default 600 s): one task's whole engine run, model and tool calls
  together. Past it the run is cancelled and the task fails with 704.
- **`model.request_timeout`** (default 120 s): one model call, litellm engine only.
  litellm's own default is 6000 s.
- **`model.max_tokens`** (default 8192): the visible reply's ceiling, sent to the provider
  as its required output cap. It exists because every provider demands one, and because
  it bounds cost and keeps a full reply inside the request timeout and the room's relay.
  A reply that hits it ends with `[reply truncated: hit the model's max_tokens limit
  (N)]`, and the agent's log gets one line saying the model, the ceiling that was sent,
  the completion tokens and how many of them were thinking, when the provider reports
  that. A one-line answer that hits the cap is thinking spending the budget, not a long
  answer; see the next setting.
- **`model.thinking`** (`mode`, `budget_tokens`; litellm engine only): the model's thinking
  budget, kept apart from the reply's. `mode: auto` (default) asks models known to reason
  (Claude 3.7 and later) to think within `budget_tokens` (default 4096) and raises the
  request's ceiling by that much, so the answer keeps all of `max_tokens`; other routes are
  sent nothing. `mode: on` always asks. `mode: off` asks the route not to think where it
  can: an OpenAI-compatible server in front of a Qwen-style model gets
  `enable_thinking: false`; a Claude that thinks only when asked is simply not asked;
  a Claude that always thinks keeps its default. On the newest Claude models the budget
  becomes an effort level (1024 low, 2048 medium, 4096 high, 8192 xhigh). Reach for `off`
  on a fast tier that answers tool-shaped one-liners; reach for a bigger budget when a
  reasoning model's replies arrive cut. Write a bare `off` or quote it; both are read as
  the word.
- **`tools.max_steps`** (default 10), **`tools.context_tokens`**, **`tools.max_repeats`**:
  when the loop counts as exhausted. **`tools.on_exhausted`**: `stop` replies with the
  limit message, `wrap_up` makes one last tool-free call for a partial answer.
- **`tools.on_truncated`** (litellm engine): what a reply cut off by `max_tokens` with no
  tool call becomes. `reply` (default) returns it marked `[reply truncated ...]`, as before;
  `fail` returns it as a FAILED task (702); `continue` asks the model to carry on, bounded by
  `max_steps`. A cut-off reply's tool calls are never run, whatever this says: each gets an
  error result asking the model to re-issue it.
- **`model.budget.per_task_tokens`**: a token budget per task (litellm engine only).

Both timeouts must be positive; a bundle can raise them, not turn them off.

### Extras

| Extra | Enables | Reach for it when |
|---|---|---|
| (none) | create, chat, run, cli transport, litellm engine, files and exec built-ins | Always: the base. |
| `mcp` | `tools.provider: mcp` | You use stdio MCP servers. |
| `claude-code` | `--engine claude-code` (claude-agent-sdk) | Claude Code should run the agent. |
| `bridge` | the loopback MCP bridge, `engines/bridge.py` (mcp>=2.2, uvicorn) | You build an engine that runs its own loop; no shipped engine uses it. |
| `a2a` | the a2a transport and JWT auth | cabal or another service sends tasks. |
| `slack` | the slack transport | The agent lives in Slack. |
| `aws` | the `cloudtrail` sidecar (boto3) | The agent queries CloudTrail Lake, with a role it assumes itself or a credential leased from the room's permission service (`bab_url`). |
| `techne` | `memory.personal: techne` | The agent should remember. |

`claude-code` runs on mcp 1.x or 2.x, whichever the Agent SDK and the rest of the environment
settle on, so it can share a venv with kobo, which pins `mcp>=1.15,<2`. Through 0.7.0 it required
mcp>=2.2 for the bridge, which no engine uses; that requirement is now the `bridge` extra, and
`bridge` still can't share a venv with kobo.

## How to install

The distribution is `tenish-daedalus`; the import package and the CLI are `daedalus`.

From a pinned git tag:

```bash
pip install "tenish-daedalus[a2a,slack] @ git+https://github.com/tenishai/daedalus.git@v0.9.0"
```

or, optionally, from a package index your team runs, as `tenish-daedalus`. The `techne` extra needs a
concrete source for techne: the repo's `requirements-sources.txt` pins it, so a checkout
installs with `pip install -r requirements-sources.txt ".[techne]"`. The runtime image,
`docker/Dockerfile`, carries techne, liminal and the a2a, slack, aws and claude-code
extras.

Then: `daedalus create <name>`, set a model (`ANTHROPIC_API_KEY`, or `OPENAI_API_BASE`,
`OPENAI_API_KEY` and `DAEDALUS_MODEL=openai/<name>`), and `daedalus chat ./<name>`.

## Where to go next

- `daedalus guide`: lists the bundled guides. `daedalus guide design` is the design
  rationale; where it and the code disagree, the code is right.
- `daedalus schema`: every agent.yaml field, with types and defaults.
- In the repo: `README.md` (quickstart), `docs/design.md` §1.2 (an annotated agent.yaml),
  `ARCHITECTURE.md` and `docs/arch/` (seam cards), `CHANGELOG.md`.
