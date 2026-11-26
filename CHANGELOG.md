# Changelog

All notable changes to daedalus are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic versioning.
Issue and pull request numbers all refer to this repository, written in full.

## [Unreleased]

### Changed

- **Licence: MIT.** The project moves from AGPL-3.0-or-later to MIT, with the agreement of every contributor (tenishai/agora discussion 69). Releases already published keep the licence they shipped under.

### Upgrading

- **`LLMResponse` token counts are `int | None` now, not `int`** (issue #268). None means
  the provider reported no count; the LiteLLM adapter used to turn that into 0. Code that
  does arithmetic on `input_tokens` or `output_tokens` raises a `TypeError` on None, so it
  must handle None (`LLMResponse.tokens_spent()` still counts None as 0, for budgets).
- **Write a hook that wants the run record as `post_draft(draft, record=None)`.** A
  runtime older than this one calls every `post_draft` with one argument, so a required
  second parameter raises there, and a hook that raises blocks every reply. Agents in one
  room run mixed runtime versions. `record` is None on an older runtime and on the
  learnings lane.

### Added

- **`tools.retry_malformed` asks again, off the record, when tool calls can't run.** A
  litellm-engine reply whose calls name no offered tool, carry arguments that aren't JSON,
  or leave out an argument the tool's schema requires is not run. The model is asked again,
  up to `retry_malformed` times (at most 5), on a scratch copy of the history that holds
  the bad reply and what was wrong with each call; only the reply finally used enters the
  history. Each retry is a model call charged to the budget, not to `max_steps`, and a
  `malformed_retry` trace event records it. Retries spent, the last reply is used as before.
  0, the default, changes nothing.

- **A `post_draft` hook can see the run behind the reply** (issue #268). A hook with a
  second positional parameter gets a frozen `RunRecord`: the engine and model, the model
  calls (`turns`), each tool call with its outcome (`ok`, `empty`, `error`, `blocked`, or
  `unknown` for a Claude Code built-in) and duration, tokens per call and in total,
  `total_input_tokens` (cached or not, whichever engine ran), Claude Code's cost, the
  limit the run hit (`steps`, `repeats`, `context`, `budget`) and whether a reply was cut
  at max_tokens. A total is None if any call went unreported. One-parameter hooks are
  unchanged. What a hook returns replaces the reply in history, so text it adds is
  replayed to the model on the next turn, sent to an agent that consulted this one, and
  fed to the learnings summary. A check that reads the record and only logs or blocks is
  safe; a footer is not, yet.
- **Tool outcomes come from the code path, not from the result's first words.**
  `Agent._call_tool` returns a `ToolResult` (a `str` with an `outcome`), and the engines
  put it on the `tool_result` event. A real result that starts with `error:` is `ok`. A
  gateway refusal is `blocked`: a colleague-task refusal, and liminal's policy refusal on a
  call (JSON-RPC -32001), which a provider raises as the new `ToolCallBlocked`. liminal
  uses -32001 for every token failure too (expired, bad signature, untrusted issuer, a
  failed JWKS fetch), so a `blocked` call can be a gateway auth outage: read its message. A liminal
  or MCP result is `empty` when its content blocks hold nothing, judged with liminal's
  untrusted-result envelope set aside, and with or without the notice liminal PR 234
  appends inside it after any blank blocks. Whitespace and zero-width text are blank.
- **Two cache fields on `LLMResponse`:** `cache_read_input_tokens` and
  `cache_creation_input_tokens`, read from litellm's `prompt_tokens_details`.
- **Two trace events.** The LiteLLM engine emits `usage` for every model call (the model
  and the four counts, budget or not) and `loop_limit` with the limit it stopped on.
- **One usage line per finished chat task, where a pod can see it.** A new `task_usage`
  event carries the task's totals, and `daedalus run -v` (how pods run) prints it as
  `[usage] task: engine=… model=… turns=… input=… output=… cache_read=… cache_write=…`,
  `unknown` where none was reported. The same text goes to an INFO log line, which prints
  only where the host configures logging (daedalus sets up no handler). The per-call
  `usage` events are not printed, and there is no metric.

## [0.9.0] — 2026-11-21

### Upgrading

- **Each skill is stored once more, as its card.** At its first boot on 0.9.0, an agent
  whose bundle offers `skills.load` seeds every skill into memory again as its card (see
  Changed). Nothing to do; a bundle without `skills.load` keeps the whole text.
- **A person's Slack turn starts with a line naming who sent it and where.** The line reads
  `users.info` and `conversations.info`, so it is fullest with `users:read`,
  `users:read.email`, `channels:read` and `groups:read` (room-made apps carry them).
  Without them it names the user id or the channel id instead; nothing fails.

### Added

- **The model is told where each person's Slack message was said.** A rule can now name
  Slack channels ("only answer in these channels"), so the line above a person's turn
  ends with the conversation: `(From Ada Lovelace <ada@example.com>, in #sre-requests.)`,
  `in a direct message` for a 1:1 DM, `in a group message` for a group DM, and
  `in <#C0123>` when `conversations.info` cannot name the channel (room-made apps carry
  `channels:read` and `groups:read`). A DM's id says what it is, so it is never looked
  up; any other channel is read once an hour, and a failed read is tried again after a
  minute, the same as a person. Every person path names its channel: a message, a
  reaction that wakes the agent, a pick from its choices, a slash command. The
  `## Rules` intro now says the line names who sent a message and where, and that a rule
  may end with lists, `(These people: ...)`, `(These emoji: ...)`, `(These channels: ...)`
  or `(These repositories: ...)`, and is about exactly what they name: people by the
  email in that line, channels by the channel in it, emoji by the reaction that woke the
  agent, repositories by the ones its tools act on. The room writes a rule that names
  things that way.

- **The model is told who sent each person's Slack message.** The sender's user id rode
  on the message for the journal only, so a rule like "only answer these people" had
  nothing to be kept against. Every person's turn (a message, a reaction that wakes the
  agent, a pick from its choices, a slash command) now reaches the model with a first
  line saying who sent it: `(From Ada Lovelace <ada@example.com>.)`, the name and email
  from `users.info` (room-made apps carry `users:read` and `users:read.email`). Without
  the email scope it is `(From Ada Lovelace <@U0123>.)`; when `users.info` cannot be read,
  `(From <@U0123>.)`. A display name is cleaned of the brackets and `@` the line is made
  of. One lookup per person an hour, shared with a reaction's program check; a failed one
  is tried again after a minute. A program's words keep their own preface and name
  nobody. The line is the new `Message.sender_label`, shown by `Agent._messages` on every
  turn the thread has had, and kept out of the text: law hooks and recall still get the
  person's words alone, because a hook may read the first email in a message as the
  subject of the ask (an access-audit check does). It never crosses the
  a2a wire and never goes into the journal. The `## Rules` intro now says that line names
  the sender, that words in a message are not proof, and that a rule ending in
  `(These people: Name <email>; ...)` is about exactly the people it names, matched by
  email: that is how the room writes a rule about named people. Not yet: the room's own
  chat and a colleague's ask, which arrive over a2a, carry no sender.

- **Guardrails: the owners' rules, in front of the model on every task.** A bundle may carry
  `guardrails.md`, one rule per line (blank lines, headings and HTML comments are skipped,
  and a leading list marker is dropped). The rules go into the system prompt as one
  `## Rules` block right after the persona, in file order. They never go through recall.
  Rules used to live in three places:
  - the persona, always loaded;
  - skills, loaded only when the model asked;
  - "guardrail skills", which load only on request and so were not guardrails at all.
  None of these showed in one place what an agent must always do.
  
  The block is cut from the bottom at 4,000 characters and says how many rules it left out.
  A new operator lane, `system_task: guardrails`, replaces the whole list live: the next task
  uses it, with no restart. The list is kept under `<memory.home>/guardrails/` and follows the
  boot seeder's staleness protocol: a restart on an older bundle keeps the live list, a bundle
  that caught up takes over, and a newer edit made in the repository wins. Rules are guidance
  in the prompt, not a control. What must never happen still belongs in the allow list, a
  gate or a law hook.

### Changed

- **Memory holds a skill's card, not its whole text, when the bundle offers `skills.load`.**
  The boot seeder stored every SKILL.md whole, and recall pasted whole rows into the prompt,
  so a question near a skill brought the entire skill along on every task, beside the
  skills index and `skills.load` that already reach it. Replayed on a seven-skill audit
  agent's bundle with the runtime image's own seeder and search, the five rows recalled per
  question carried 14,000 to 59,000 characters, 67 to 98% of it skill text, and skills
  took up to three of the five rows. A skill now seeds as its card: the id, the one-line
  summary and how to load it. The same questions recall 2,000 to 5,000 characters, and the
  rows go to learned memories. An agent seeded the old way re-seeds each skill once at its
  next boot (the card mode is part of the file's digest); a bundle without `skills.load`
  keeps the whole text, since recall is its only route to a skill.
- **The skills index falls back to a skill's `description`** when it has no `when_to_use`,
  before the name. A skill written with a name and a description only was listed by its
  name alone. Index lines are one line, cut at 300 characters.

## [0.8.0] — 2026-11-14

### Upgrading

- **Another Slack app's @-mention now wakes an agent.** It used to be dropped. The turn runs
  with knowledge-only tools by default, the same ceiling as a colleague's ask; set
  `SLACK_BOT_MENTIONS=off` on an agent to keep the old behaviour (see Added).

### Added

- **One agent can ask another in a Slack channel.** A message a bot wrote was dropped whole,
  so an agent that @-mentioned another agent got silence. Another app's `app_mention` now
  wakes the mentioned agent. `SLACK_BOT_MENTIONS` sets what that mention may do:
  `knowledge` (the default) answers it the way a colleague's ask over the switchboard is
  answered, with knowledge-only tools and no borrowed credential (tenishai/kobo#310),
  because some apps post text an outsider wrote and a mention inside it must not drive an
  agent's access; `full` gives it the tools a person's mention gets; `off` drops it. An
  agent never answers its own words, a bot's plain message is still dropped in every wake
  mode, and a bot's words are labelled as a program's in the thread's history. Two bounds
  end a loop between agents: three bot-woken turns in a row in one thread, after which the
  mention gets a stop-sign reaction and no answer until a person speaks to the agent there,
  and thirty an hour from one sending app.
- **A reaction can wake an agent in Slack.** `SLACK_WAKE_EMOJI` names the emoji, separated
  by commas (`ticket, eyes`); unset, a reaction is dropped as it always was. It works in
  every wake mode: a named emoji added to a message in a channel the agent is in asks the
  agent about that message. The agent reads the message, the person who reacted is the
  requester, and the answer lands in that message's thread (which a `thread`-mode agent
  then follows when the message started it). The rest is not configurable: a removed
  reaction never wakes it, one message wakes it once per emoji however many people add the
  same one, and its own reactions never do. A reaction another app left is treated the way
  another app's mention is (`SLACK_BOT_MENTIONS`, same bounds), and a reactor Slack will
  not identify is never taken for a person. People's reactions start at most sixty turns an
  hour; past that the message gets a stop-sign reaction and no answer. The app needs
  `reactions:read`, the `reaction_added` event, and `channels:history` / `groups:history`
  or `reactions:read` to read the message; `users:read` tells a person from a program.

- **An agent in Slack can post, offer choices, pin and bookmark.** A bundle with a
  `kind: slack` transport block now gets four tools once its bot token is set, each still
  subject to `tools.allow` like any other (`slack.*` allows them all): `slack.post_message`
  (a message in the conversation it was asked in, or in another channel, where it names the
  person it is sent for), `slack.offer_choices` (a question with buttons, or a menu past
  five choices), `slack.pins` and `slack.bookmarks` (add, remove, list). A tool called with
  no channel acts where the task was asked; a block's `channel` list is also the list of
  other channels the tools may reach. A turn a program asked for (a colleague's ask, another
  app's mention) never gets them. With the `chat:write.public` scope a post reaches a public
  channel the agent was never invited to; pins need `pins:read` / `pins:write`, bookmarks
  `bookmarks:read` / `bookmarks:write`. Slack's refusals come back as a sentence that names
  the missing scope.
- **A click and a slash command reach the agent.** The first pick from choices the agent
  offered rewrites that message to say who chose what, removes the buttons, and runs as that
  person's next message in the thread; nothing about an offer is kept in memory, so a
  restart between the offer and the pick loses nothing. `/its-command some words` posts a
  message in the channel saying who asked what and the agent answers in its thread; an
  empty command, or one in a conversation the agent cannot post in, is answered to that
  person alone. A command is answered only in a conversation the agent is in;
  `SLACK_COMMAND_PUBLIC=on` also answers in a public channel it was never added to. Over Socket Mode these are the `interactive` and `slash_commands` requests.
  Over HTTP the relay forwards them to the same `POST /slack/events`: a `block_actions`
  payload as Slack sent it, and a command's form fields as one JSON object with
  `"type": "slash_command"` added. A repeated `trigger_id` runs once. The app needs
  interactivity turned on and a slash command (with the `commands` scope) pointed at the
  relay or the socket.

### Fixed

- **A person's upload in a thread the agent follows is no longer dropped.** Slack marks a
  message that carries a file `file_share` and a thread reply also sent to the channel
  `thread_broadcast`. Both were dropped with the edits and joins, so in `thread` and
  `channel` wake modes a screenshot posted without a mention never reached the agent
  (a mention still worked, through `app_mention`). Both now go through the wake rule like
  any other message a person wrote. Edits, joins, deletions and a bot's uploads stay dropped.

## [0.7.1] — 2026-11-11

### Upgrading

- **`pip install "tenish-daedalus[claude-code]"` no longer brings mcp 2.x or uvicorn.** Only
  the loopback MCP bridge (`engines/bridge.py`) needed them, and no engine uses it. An
  environment that runs the bridge installs the new `bridge` extra (see Changed).
- **A slack transport block may be `optional: true`.** Such an agent boots with no Slack
  token in its environment and serves Slack once one is there; a block without the flag
  still fails boot without a token, as before. A room that wires Slack dormant at birth
  needs this release (tenishai/kobo#434).

### Added

- **`audit.trace`: the access-provenance chain in a fixed order** (`tools.audit_trace`). An
  auditing agent asked "when did this person get this access, and who approved it" used to
  pick its own steps from a skill, and sometimes skipped one, searched a partial name, or
  stopped at the first empty search. The tool runs the chain itself over the agent's own
  lanes: the provisioning log's adds, removes and account lifecycle; the access-request
  tickets naming the person's full email in a window around each add, each checked for
  naming the group, its created time and its approval; the approval recorded nearest before
  the add chosen as the authorization; and the request thread the ticket (or its parent)
  names, else an anchored search that keeps only a thread carrying the ticket's key. On when
  `tracker_site` and `tracker_project` are set; needs `cloudtrail` and `slack_read` on.
- **`slack.find_threads` reads replies too.** A request bot's opening message often names the
  requester by Slack id, and the email appears only in a reply, so a root-only scan answered
  "no thread" for a thread that existed. The find now also reads the replies of up to 150
  threads in the window. **`slack.read_thread` shows who posted and who reacted**: each
  message's author and reactions with the reacting users (the approval is often a reaction),
  resolved to names and emails when the app has `users:read`, and said plainly when not.
- **`cloudtrail.query_provisioning` returns account creation** (`user.lifecycle.create`), the
  real answer to "when was this person onboarded".

- **An agent image is the runtime and nothing else** (one runtime, bundles as artifacts;
  tenishai/kobo#434). With `BUNDLE_ARTIFACT` in the pod's environment the entrypoint pulls
  the bundle from that registry artifact at start (`docker/bundle_pull.py`: one gzip
  tarball layer, a Google access token from the pod's own identity, the last pulled bundle
  kept on the agent's volume for a start the registry cannot serve); without it the image's
  own `/bundle` runs as before. Memory replication moved into the runtime image: with
  `MEMORY_REPLICA_BUCKET` set the entrypoint writes the litestream config and runs under
  it, so an agent's Dockerfile no longer adds litestream or its own entrypoint. A slack
  transport block marked `optional: true` serves only once a Slack token is in the
  environment, instead of failing boot: the room wires Slack dormant at birth, and adding
  it later is a Secret and a restart.

### Changed

- **The `claude-code` extra no longer requires mcp 2.x** (tenishai/ergon#34). It needed
  `mcp>=2.2` and uvicorn only for the loopback MCP bridge (`engines/bridge.py`, PR 107), which
  no engine uses: the Claude Code engine serves its tools through the Agent SDK's in-process
  MCP server (PR 108). `claude-code` is now `claude-agent-sdk` alone, which accepts mcp 1.x or
  2.x, so it co-installs with kobo's `mcp>=1.15,<2` and ergon's version set can include it. The
  bridge's requirements are a new `bridge` extra, and the bridge refuses to start on mcp 1.x
  with a message naming that extra. CI runs the `claude-code` extra on mcp 1.x as a third leg.

### Fixed

- **The streamed reply opens with the agent's face too** (tenishai/daedalus#251). 0.7.0 put
  the icon on `chat.postMessage` only; a reply that streams (`chat.startStream`, the path a
  room agent answers on) opened without it, so Slack showed the app's default picture on
  every answer. The stream now carries `SLACK_ICON_URL` as its icon the same way, and falls
  back to plain when the app lacks `chat:write.customize`.

## [0.7.0] — 2026-11-03

### Upgrading

- **A custom `ToolProvider` is withheld on a colleague's task unless it says otherwise.**
  An agent answering another agent (`cabal_delegation_depth` ≥ 1) is now offered only
  tools whose provider sets `knowledge_only = True` (see Changed). A provider that only
  reads the agent's own material should set it to stay offered there.
- **Bundles that use `tools.workspace` or `tools.bind` need this release.** kobo validates
  bundles against its vendored copy of `docs/bundle.schema.json`, so refresh that copy
  before kobo builds one.

### Added

- **The agent's face on every Slack message.** With `SLACK_ICON_URL` set (the room
  serves the agent's own picture there) and the app's `chat:write.customize` scope,
  every `chat.postMessage` carries that picture as its icon, chunks included; an
  owner changes the face in the room and Slack follows on the next message. An app
  installed before the scope refuses the field once, and the transport posts plain
  from then on, never dropping a message.

- **One agent can work a different checkout on each task** (tenishai/daedalus#247,
  for tenishai/agents#171). `tools.workspace.parent` names a directory of prepared
  per-task folders, and a builtin `files` or `exec` root written as the literal
  `{workspace}` (or `{workspace}/sub`) is filled per task. Task metadata
  `daedalus_workspace` holds one folder name, never a path. A name that isn't one plain
  folder, a symlink out of the parent or a missing folder fails the task with 701 before
  any tool is listed, so a caller can choose which folder a task uses but never widen the
  roots. The token without a parent, a parent that isn't a directory (checked whenever it
  is set), or a sub-root with `..` fail at load; a parent removed after load fails the task
  with 701. The Claude Code engine runs in the task's workspace, ahead of
  `engine.claude_code.cwd`. `tools.bind` (`{tool: {argument: dotted.metadata.path}}`)
  fills a tool's arguments from task metadata on every call, before the approval gates.
  Bound arguments are removed from the listing the model sees, whatever the model sends
  for them is overwritten, and a missing value fails the call; a bind key that matches no
  offered tool emits `bind_unmatched`. `daedalus run -m … --metadata '<json object>'`
  merges into the one-shot task's metadata, so a launcher can name the workspace and
  bound values. A bundle with neither field behaves as before. `docs/bundle.schema.json`
  regenerated, so kobo's vendored copy needs refreshing for bundles that use them.

- **The thinking budget is kept apart from the visible reply's, and a cut reply says why
  in the log** (tenishai/daedalus#240). A one-line answer hit the 8192 `max_tokens` cap
  twice on one day because the provider counted the model's thinking against it.
  `model.thinking` (`mode: auto|off|on`, default `auto`; `budget_tokens`, default 4096,
  floor 1024) is new: for Claude routes, `on`, and `auto` on a 3.7-or-later id, send
  litellm's `thinking={"type": "enabled", "budget_tokens": N}` with the request's
  `max_tokens` raised by N, so the answer keeps its whole budget (litellm translates the
  shape for the models that take an effort level instead); `off` sends nothing. For
  OpenAI-compatible routes (`openai/`, `hosted_vllm/`) `off` and `on` send
  `chat_template_kwargs.enable_thinking` in `extra_body`; `auto` sends nothing. Every
  provider fact is one table in `daedalus.llm`. When a reply finishes with reason
  `length`, `LiteLLMClient` logs one INFO line with the model, the ceiling sent, the
  completion tokens and the reasoning tokens when the provider reports them; the
  truncated sentence on the reply is unchanged. A tiers-file entry may set `max_tokens`
  and `thinking`, filled when the bundle is silent, so a deployment raises the ceiling per
  tier; the bundle default stays 8192. `model.max_tokens` now carries a description and
  refuses zero. A bare YAML `off`/`on` under `thinking.mode` is read as the word, not the
  boolean YAML 1.1 makes of it. `docs/bundle.schema.json` regenerated; the greeter golden
  bundle carries the new block.
- **Slack events can arrive over HTTP, with no app-level token** (tenishai/kobo#304,
  tenishai/kobo#371). The a2a app gains `POST /slack/events`: the room's relay
  forwards Slack's event envelope exactly as Slack posted it, with the header
  `X-Kobo-Relay-Secret` equal to the agent's `SLACK_RELAY_SECRET` (503 while that env
  is unset, 403 when it differs). The route answers `200 {"accepted": true}` at once
  and schedules the same `handle_event` Socket Mode calls, so threads, wake modes,
  streaming, approvals and images are untouched; a `url_verification` body gets its
  challenge back. Transport choice at start: `SLACK_APP_TOKEN` present means Socket
  Mode exactly as before and the route answers 404; absent, with `SLACK_BOT_TOKEN`,
  means the HTTP way in and no socket is opened. One boot line says which. Both ways
  in pass through `SlackTransport.admit`, which drops a repeated `event_id` (the last
  2000 remembered) before the existing `(channel, ts)` dedupe. Existing agents, which
  all have the app-level token, change nothing.
- **`engine.claude_code.cwd` sets the directory Claude Code works in** (tenishai/daedalus#242,
  PR 243), passed to the Agent SDK as `cwd`. A path that isn't a directory fails the load
  when the claude-code engine runs the bundle; the litellm engine, which never uses it,
  still loads the same bundle. Schema regenerated.
- **The `cloudtrail` sidecar can lease its credential instead of assuming a role.** With
  `tools.cloudtrail.bab_url` and `bab_integration` set, the pod asks the room's permission
  service (bab) for a one-hour credential, proving itself with the service account token
  projected at `bab_token_file` (default `/var/run/secrets/bab/token`, audience `bab`).
  The service holds the role, narrows the session to what this agent was granted and names
  the agent in it; the pod assumes nothing and `role_arn` may be omitted. A refusal comes
  back to the model as a sentence ("I do not hold X on Y; ask my owner to request it"), a
  pending approval as "waiting for approval", and an unreachable service means no new
  credential while a cached one keeps working until it expires. The credential is re-asked
  for at least once a minute, so a grant the owner revokes stops the next call within a
  minute rather than when the hour runs out.

### Changed

- **An agent answering another agent gets knowledge-only tools** (tenishai/kobo#310, the
  strict model). When a task arrives with `cabal_delegation_depth` ≥ 1, the tools offered
  for that task are only those from providers that declare `knowledge_only = True`
  (today: `skills.load`); MCP and gateway tools, the github, jira, cloudtrail and slack
  sidecars and `colleagues.ask_colleague` are withheld (event
  `tools_withheld_for_colleague`, names listed), and the `## You are answering a
  colleague` prompt block now says so and names whose owner can request a tool the answer
  needs. As a second wall, the cloudtrail lane, the github broker mint and a liminal
  gateway call refuse on such a task before anything leaves the pod, with one sentence:
  "I can't use my own access for a task another agent asked for; your owner can request
  <permission> for <agent> on its Access page." Tasks a person started are unchanged.
  Outbound asks now carry `daedalus_asked_by: <this agent's name>` beside the depth key,
  a cross-repo string contract like `cabal_delegation_depth`; cabal copies metadata
  through verbatim. A custom `ToolProvider` that only reads the agent's own material
  should set `knowledge_only = True` to stay offered on a colleague's task; the default
  is credentialed.

### Fixed

- **MCP tool results keep the file an embedded resource carries** (PR 238). The `mcp`
  provider flattened a result to its text blocks, so a server that returns a file as an
  `EmbeddedResource` (GitHub's hosted MCP server answers `get_file_contents` that way)
  reached the model as one confirmation line. `result_text` now renders each resource's
  text after a line naming its uri and mime type, names a binary resource without
  expanding it, and still skips image blocks.

## [0.6.1] — 2026-10-31

### Upgrading

- **`memory.personal: techne` needs techne 0.19.1 or later** (tenishai/daedalus issue
  #228). The `techne` extra now requires `tenish-techne[vectors]>=0.19.1`, and an older
  techne fails the bundle at load with a `BundleError`. Reinstall with
  `pip install -U 'tenish-daedalus[techne]'` from the tenish index.

### Fixed

- **CI runs the techne-backed tests on every push and same-repo PR** (tenishai/daedalus
  issue #48). The `techne` job cloned the private techne repo, which this repository's
  CI token can't read, so it only ran by hand and failed. It now installs
  `tenish-techne` from the tenish index through the `techne` extra, with the read-only
  index token in the `TENISH_INDEX_TOKEN` repository secret.

- **The litellm engine accepts a tool name the model returned without its server prefix**
  (tenishai/daedalus issue #167). Gemini sometimes sends `remember` for
  `colleagues__remember`, and the call was refused as unknown. A bare name now resolves
  when exactly one offered tool ends in it; an ambiguous or unknown name is refused as
  before, and the allowlist check still runs on the resolved name. The trace records a
  `bare_tool_name` event when this happens.

- **The open-listener warning names the bundle and goes to the log** (tenishai/daedalus
  issue #204). An a2a transport on an address other than loopback with no `auth` block
  already printed a stderr warning; it now goes through logging at WARNING when the
  process has configured logging (else stderr, once), and the text names the
  bundle and `host:port`, says anyone who can reach it can use every tool the allowlist
  admits, and says to add `auth`. It still serves; loopback and `auth` stay quiet.

### Added

- **A model-free `scripted` engine for tests** (tenishai/daedalus issue #227). With
  `DAEDALUS_SCRIPTED_ENGINE=1`, `--engine scripted` runs one command per message instead of
  calling a model: `remember: <fact>` commits through the agent's memory store,
  `recall: <query>` replies with the store's hits as a JSON list of `content` and `source`, and
  `call: <tool> <json args>` calls a tool through the same allowlist, approval gates and
  tool provider the litellm engine uses. Anything else fails the task with reason 704.
  Without the variable the engine is refused at load. `EngineTurn` gains an optional
  `memory` field for it.

### Changed

- **techne memory reaches the team pool** (tenishai/daedalus issue #228). `TechneMemoryStore`
  still recalls up to k of the agent's own memories from its knowledge, and now adds up to
  k rows from other stores through `techne.api.recall`, the same recall `techne_recall`
  runs: a fact another agent published comes back from an installed recall source such as
  techne-archivist-sync, or from a federated store. `recall_hits()` says where each hit
  came from (`local`, or the source's or store's name, e.g. `liminal_archivist`). At the
  end of each task, including one finished by `resume()`, the agent calls
  `techne.api.publish()` once, which sends knowledge carrying one of techne's
  `archivist_publish_tags`; with that list empty, techne's default, nothing is sent.
  `MemoryStore.commit` takes `tags`, and the scripted engine's `remember:` turns `#words`
  into tags. `memory.home` now also supplies techne's config.yaml.
  The agent's own recall now leaves out soft-archived knowledge, as `techne_recall`
  does; before, it returned it (tenishai/daedalus issue #233).

## [0.6.0] — 2026-10-26

### Upgrading

- **`tools.builtin.files` refuses roots that serve the host** (tenishai/daedalus issue
  #199). `roots: ["/"]` used to load, and `files.read` then served `/proc/self/environ`,
  the agent's whole environment, to anyone who could message it. A root is now refused at
  load, with a `BundleError` naming it, when it is `/`, at or under `/proc`, `/sys`, `/dev`,
  `/run` or `/etc`, `$HOME` or any directory above it, or at or under `$TECHNE_HOME`.
  Symlinks are resolved first. A bundle with such a root fails to boot until the root names
  a workspace directory.
- **Text tool calls are salvaged only inside tags, and only for self-hosted models by
  default** (tenishai/daedalus issue #200). The litellm engine used to run any tool-shaped
  JSON it found in a reply, including JSON the model only quoted, so a caller could ask
  the model to repeat `{"name": "exec__run", ...}` and have it run, skipping the model's
  own decision to call. Salvage now reads only JSON inside `<tool_call>` or `<tools>` tags,
  and it runs only when the new `tools.salvage_text_calls` is true. Unset, it is on when
  `model.api_base` is set (a self-hosted endpoint such as vLLM) and off otherwise. A
  self-hosted model that leaks bare JSON outside tags is no longer salvaged; its reply comes
  back as text. `docs/bundle.schema.json` gains the field, so a vendored copy (kobo)
  needs re-vendoring to accept it.
- **`GET /events` needs a token when the a2a transport has `auth`.** Send the same
  `Authorization: Bearer <cabal-minted JWT>` the POST routes take (same issuer and
  audience); without it the route returns 401. Bundles with no `auth` block are unchanged.
  kaminos's trace proxy sends no token, so its trace pane, like its test chat, works only
  against bundles without `auth`.
- **`/events` no longer carries tool arguments, tool results or reasoning text by default.**
  `tool_call`, `tool_result` and `reasoning` events go out with `arguments_chars`,
  `content_chars` and `text_chars` in place of the content; names and every other event
  are unchanged. Set `DAEDALUS_TRACE_CONTENT=1` in the agent's environment to send the
  content as before. `run -v` and the Slack progress lines read the in-process bus and
  still see everything.
- **The operator lanes admit only allowlisted subs.** teach, knowledge, tool and
  learnings tasks are refused unless the verified sub is `operator:<anything>` or exactly
  `service:kobo`, the sub kobo's deploy docs mint for `CABAL_OPERATOR_TOKEN`. If your
  room's token uses another sub, the agent emits `teach_refused` (and the other
  `<lane>_refused` events) with that sub; add it to the a2a transport's `auth` block:
  `operator_subs: ["operator:*", "<your kobo sub>"]`. A list replaces the default, and an
  entry ending in `*` matches by prefix. kobo falls back to its archive commit when the
  instant lane refuses, so a refused teach lands at the next rebuild instead of being lost.
- **Tasks now have a deadline, and model calls a timeout.** An engine run that takes
  longer than `task_timeout` (agent.yaml top level, seconds, default 600) is cancelled,
  tool calls included, and the task fails with reason 704. A litellm model call that
  takes longer than `model.request_timeout` (seconds, default 120; a tiers-file entry can
  set it for its model) fails with an `LLMError`; before, litellm waited up to 6000 s. A
  bundle whose tasks legitimately run longer must raise `task_timeout`. Both fields are
  new in `docs/bundle.schema.json`, so a copy vendored elsewhere (kobo) needs
  re-vendoring to accept them.
- **`POST /a2a/tasks` with an id the agent already holds now returns 409.** It used to
  replace the stored task. cabal and kobo mint a fresh id for every dispatch, so they are
  unaffected; a caller that reuses ids must send a new one. This applies with or without
  `auth`.
- **With `auth`, a task answers only to the sub that created it.** `tasks/get` and a
  `message/send` continuation from any other sub, or from a token with no sub, get the
  same error as an unknown id. cabal's re-mint copies the caller's sub, so a person or
  kobo continuing its own task through cabal is unaffected. Without `auth`, every task
  stays visible to every caller, as before.

### Added

- **`daedalus guide`** lists the guides that ship in the package, and `daedalus guide
  welcome` prints the tour: what daedalus is, where techne, liminal, cabal and kobo fit,
  and every engine, tier, tool provider, approval tier, transport, memory option, timeout
  and extra, with when to reach for each. `daedalus guide design` prints docs/design.md.
  An unknown name exits non-zero and lists the valid ones.
- **`docs/bundle.schema.json` describes four fields**: `tools.builtin` (its groups, and the
  files roots refused at load), `tools.salvage_text_calls`, `model.request_timeout` and
  `task_timeout`, so an editor reading the schema shows them.

### Changed

- **The a2a boot line names its auth regime, and an open listener warns** (tenishai/daedalus
  issue #204). The line now reads `(a2a listening on 0.0.0.0:8080, NO AUTH)` or `..., JWT
  auth)`, and with no `auth` block on an address other than loopback the runtime also prints
  a WARNING to stderr: anyone who can reach the port can send it tasks. It does not refuse
  to start, because kobo writes no `auth` block when cabal isn't configured.
- **CI runs `scripts/check_arch_cards.py`** in the `test` and `fork-pr` jobs (issue
  #206), so a change to an anchored file without its architecture card fails the build
  instead of merging.

### Security

- **litellm is pinned exactly, to 1.104.2** (PR #193). The pin replaces a range, so a fresh
  install can no longer pick up a newer litellm; it went in after litellm 1.107.0 on PyPI
  (2026-10-21, since quarantined) shipped a credential stealer. Raise it only to a release
  someone has checked.
- **The `/events` trace stream is behind the transport's verifier.** It used to answer
  without a token even when `auth` was configured, and its replay of the last 100 events
  included full tool results: files the agent read, Slack threads, colleague answers.
  Anything that could reach the pod's port could read them (issue #196).
- **The operator-lane gate is an allowlist** (issue #195, closing issue #47). It used to
  refuse `persona:`, `agent:` and bare `operator` and admit every other verified sub, so a
  service token minted for something else could deliver a tool file, which the agent runs
  as itself at its next boot. The code and docs no longer call the tool file's
  import-statement check validation: it is a lint, and `__import__("os")` passes it.
- **a2a tasks belong to the caller that created them** (issue #197). Any token cabal
  minted for the agent's audience could read another caller's task with `tasks/get`,
  append a turn to it with `message/send`, or replace it through `POST /a2a/tasks` with
  the same id, so an agent-to-agent token could read a human operator's conversation.
  The transport now records the verified sub on each stored task and answers other subs
  as if the task did not exist, and refuses a repeated id with 409.

### Fixed

- **The a2a task store and the Slack thread map are bounded** (tenishai/daedalus issue
  #202). Both grew for the life of the pod, Slack threads with their images, until a busy
  agent was OOM-killed. The a2a store keeps at most 1000 tasks and drops the least recently
  stored finished ones (a running task is never dropped; a dropped id answers like an
  unknown one). Slack keeps the 500 most recently used threads, and a dropped thread starts
  a fresh conversation. Images still stay in a kept thread's history.
- **The files builtin and the checkpoint store no longer block the event loop**
  (tenishai/daedalus issue #205). A `files.search` over a large root, a large read or a
  sqlite checkpoint write ran on the loop and stalled every other coroutine, `/healthz`
  included. They run on a worker thread now, and `files.search` stops after 10 seconds,
  walk included, with a note saying how many files it searched and that a missing match is
  not an absent one. A regex with catastrophic backtracking on one line is still not
  bounded.
- **Under auth, a reused `contextId` no longer replaces another caller's journal row**
  (tenishai/daedalus issue #212). The journal kept one row per `context_id`, and a fresh
  `message/send` takes the caller's `contextId`, so sub B could send A's id and replace A's
  row, which is the learnings summarizer's source. Rows now carry the task's verified sub,
  and the upsert replaces only a row with the same `context_id` and owner. Without auth
  every owner is empty and nothing changes; an existing `journal.db` gains the column in
  place.
- **`docs/design.md` §3 no longer claims three security properties the code doesn't have**
  (tenishai/daedalus issue #203). Gate plans carry the call's full arguments and the Slack
  approver posts them verbatim; nothing parks a task in `input-required`; and core has no
  masking or placeholder-refusal primitives. The section now says so, and names the one
  redaction that exists (the `/events` trace).
- **A hung provider no longer holds a task for up to 100 minutes, and a failed a2a task
  no longer stays WORKING** (issue #201). litellm's default request timeout is 6000 s and
  nothing else bounded a task, so a stalled provider kept the request open after cabal
  had given up, `tasks/get` answered WORKING until the pod restarted, and the Slack
  thread kept its eyes reaction. Model calls now time out, the engine run has a
  deadline that cancels whatever is in flight, and a run past it ends FAILED with reason
  704 on every transport. When an a2a handler raises or is cancelled, its stored task is
  marked FAILED with reason 704; the HTTP and JSON-RPC errors are unchanged.


## [0.5.5] — 2026-10-15

### Upgrading

- No bundle or config change. The `## Clock` section ends with one more line, an
  instruction about relative dates. Anything that reads or matches the system prompt
  should expect it.

### Changed

- **The `## Clock` section tells the model to state today's date before working out a
  relative one.** After the past-week line it now says: "Before you reason about any
  relative date (yesterday, last Tuesday, two weeks ago), write out today's date and
  weekday from the lines above, and read the date from the list instead of counting.
  When you build a search window or filter from a relative date, say the dates you are
  using." On 0.5.4 a model asked a task question containing "last Tuesday" used a
  Wednesday a week earlier and searched the wrong days, although the list had the right
  date; with the same instruction in the question it was right (tenishai/daedalus#184).
  Whether the sentence in the section has the same effect is checked downstream. Pull
  request tenishai/daedalus#185.

## [0.5.4] — 2026-10-14

### Upgrading

- No bundle or config change. The `## Clock` section is two lines longer: after
  `Current time (UTC): …` it now carries `Yesterday: <Weekday> <date>` and
  `Past week: <Weekday> <date>, …` for the seven days before. Anything that reads or
  matches the system prompt should expect them.

### Changed

- **The `## Clock` section lists yesterday and the past week.** After the current-time
  line it now carries `Yesterday: Tuesday 2026-10-13` and
  `Past week: Tuesday 2026-10-13, Monday 2026-10-12, …`, seven days back, all from the
  same UTC reading. With only today's weekday, one model asked "What is today's weekday?
  Then give the date of last Tuesday." named a Wednesday or a Thursday, contradicting
  its own count (tenishai/daedalus#181). The section is two lines longer. Pull request
  tenishai/daedalus#182.

## [0.5.3] — 2026-10-14

### Upgrading

- No bundle or config change. The clock line's format changed from
  `Current time (UTC): <ISO 8601>` to `Current time (UTC): <Weekday> <ISO 8601>`, and it
  moved from after the skills index to a `## Clock` section right after the persona.
  Anything that reads or matches the system prompt should expect the weekday and the new
  position.

### Changed

- **The clock line names the weekday and has its own heading.** It now reads
  `Current time (UTC): Wednesday 2026-10-14T11:38:14+00:00`, under a `## Clock` heading
  placed right after the persona and before the skills index. An agent given only the ISO
  date called a Wednesday a Thursday and placed "last Tuesday" on a Monday
  (tenishai/daedalus#175). The weekday comes from the UTC reading and a fixed English
  list, not the pod's locale. Before, with skills assigned, the line followed the skills
  index and read as part of it (tenishai/daedalus#173). Pull request tenishai/daedalus#176.

## [0.5.2] — 2026-10-14

### Upgrading

- No bundle or config change. The system prompt gains a `Current time (UTC)` line after
  the persona and skills index, so it is a few tokens longer and differs from one task to
  the next. Within a task it is re-sent unchanged, so turns after the first still hit the
  prompt cache.

### Added

- **Every task tells the model the current time.** The system prompt now carries a
  `Current time (UTC): <ISO 8601>` line after the persona and skills index, read from the
  pod's clock when the task starts (tenishai/daedalus#171). Before, nothing in the prompt
  carried a date: one agent said it didn't know the date, and another invented one and
  named a source that doesn't exist. The learnings task already passes the day it
  summarises, so it is unchanged. Pull request tenishai/daedalus#172.

## [0.5.1] — 2026-10-13

### Upgrading

- No bundle or config change. A room that receives learnings sees two differences: a
  candidate from its own chat carries `said_by: "user"`, and a candidate whose attribution
  fails verification isn't sent. kobo's `learnings_require_attribution` switch is safe to
  turn on once every agent runs 0.5.1.

### Changed

- **The learnings task no longer sends unattributed candidates.** This changes what the
  room receives (tenishai/daedalus#165). An empty `said_by` used to mean either a plain
  `user:` turn (a user with no sender id, which is how the room's own chat arrives) or an
  attribution that failed verification, and the room couldn't tell which. A plain `user:`
  turn now comes back as `said_by: "user"`, checked against those turns like any named
  speaker, and a candidate whose quote isn't in the named speaker's own turns is dropped.
  A room that shows `said_by` verbatim will show `user` for candidates from its own chat.
  Pull request tenishai/daedalus#166.

## [0.5.0] — 2026-10-12

Everything since v0.4.0 (2026-09-14), 57 commits.

### Upgrading

- **The distribution is now `tenish-daedalus`** (was `daedalus` in 0.4.0): every released
  tenish Python distribution is named `tenish-<project>`. The import package
  (`import daedalus`) and the `daedalus` command are unchanged. Run
  `pip uninstall daedalus tenishai-daedalus` before installing 0.5.0: v0.4.0 installed as
  `daedalus`, and a checkout of main between tenishai/daedalus#138 and this release
  installed as `tenishai-daedalus`; pip treats each name as an unrelated project and would
  leave the old one beside the new. Extras install as `tenish-daedalus[a2a]`, and
  `pip show tenish-daedalus` looks it up. `__version__` is still the literal in
  `src/daedalus/__init__.py`, so `daedalus version` is right with any install; reinstall an
  editable checkout (`pip install -e ".[dev]"`) anyway so pip's record carries the new name.
  `v0.5.0` is the first tag that builds `tenish-daedalus`; `v0.4.0` builds `daedalus`.
- **techne 0.18.2, under its new distribution name `tenish-techne`**
  (tenishai/daedalus#152). The `techne` extra needs `tenish-techne[vectors]>=0.18.2` (was
  `techne[vectors]>=0.17.0`), and `requirements-sources.txt` pins the `v0.18.2` tag. The import package and the `techne`
  command are unchanged. Run `pip uninstall techne` before reinstalling: pip treats the two
  names as unrelated projects and would leave the old one installed beside the new one.
  `docker/Dockerfile` installs and asserts 0.18.2, from a sibling checkout or an index.
- **The runtime image needs liminal 0.4.0, under its new distribution name
  `tenish-liminal`.** `docker/Dockerfile` installs `tenish-liminal` (was
  `tenishai-liminal`), and its index path asks for `tenish-liminal>=0.4.0`. A
  sibling-checkout build wants `../liminal` at `v0.4.0` or later: pip refuses a direct
  requirement whose name doesn't match the project, so an older checkout fails the wheel
  step. The import package `liminal` is unchanged.
- **Slack tier-2 approvals need an `approvers` list** (tenishai/daedalus#141). A
  `kind: slack` block now takes `approvers: [U...]` (Slack user ids), and reaction
  approvals are wired only when the transport has both a `channel` allowlist and
  approvers. In 0.4.0 any member of an allowlisted channel, the requester included, could
  approve; with no approvers a tier-2 action is now denied before a plan is posted. Add
  `approvers` to any bundle that relies on Slack approvals.
- **Bundles that load in 0.4.0 can fail in 0.5.0.** `memory.team: outbox` is rejected,
  since it never did anything (tenishai/daedalus#142, closes tenishai/daedalus#111). A
  `cloudtrail` sidecar's `event_data_store` must be an event data store ID (a UUID) or an
  eventdatastore ARN (tenishai/daedalus#142, closes tenishai/daedalus#88). An empty
  `channel:` key on a Slack block is a boot error; leave the key out to mean every channel
  the app was invited to (tenishai/daedalus#77).
- **`github.open_pr` branches are named `daedalus/<agent>/<slug>`** (was
  `kobo/<agent>/<slug>`). Set `tools.github.branch_prefix: kobo` to keep the old names
  (tenishai/daedalus#144).
- **A bundle with skills needs `skills.load` in `tools.allow`.** 0.5.0 delivers assigned
  skills as an index in the system prompt plus a `skills.load(name)` tool. Without
  `skills.load` admitted, the index is left out, boot logs "N skill(s) assigned but
  tools.allow does not admit 'skills.load'", and the agent gets only the memory recall it
  had in 0.4.0 (tenishai/daedalus#63).
- **The `slack` extra needs slack-sdk 3.40 or later** for streamed replies
  (tenishai/daedalus#147). `wake: thread` and `wake: channel` also need `message.channels` /
  `message.groups` subscribed in the Slack app manifest.

### Added

- **Slack wake modes.** A `kind: slack` block takes `wake: mention | thread | channel`
  (`SLACK_WAKE_MODE` in the environment overrides it). `mention` is the default and
  answers the same messages as before. `thread` answers a mention and then every reply in a thread
  the agent was asked in at the top, without re-tagging; a mention deep in someone
  else's thread is answered and adopts nothing. `channel` answers every message. The
  mode rides on each Slack task as metadata and becomes a prompt notice, so the agent
  tells the truth when asked whether it must be mentioned. Mixed `wake` values across
  blocks fail the bundle. The `thread` and `channel` modes need `message.channels` /
  `message.groups` subscribed in the Slack app manifest. In `thread` mode the
  followed threads are kept in memory, so a restart forgets them: an old thread
  then needs a mention on every message, and a new thread started with a mention
  is followed again (tenishai/daedalus#147).
- **Slack replies stream, with progress.** The working message is a `chat.startStream`
  stream: each tool call, and each memory recall that finds something, appears as a
  progress line under it (a repeat straight after the same line is not shown again), and the answer lands in the same message. Events are stamped with their
  task's context id, so two threads working at once never show each other's steps. A
  workspace that refuses the stream gets the hourglass placeholder as before. The
  `slack` extra now needs slack-sdk 3.40 or later (tenishai/daedalus#147).
- **The Claude Code engine.** `daedalus run` and `daedalus chat` take `--engine claude-code`
  (or `DAEDALUS_ENGINE`) to run a bundle with Claude Code as the agent, on Anthropic's
  Agent SDK. A turn now runs behind an `Engine` seam; the LiteLLM loop is the first
  engine and Claude Code the second. The bundle's own tools, allowlist and approval gates
  still decide every call, including Claude Code's built-ins. Engines that run their own
  loop get a loopback MCP bridge to the bundle's tools. New `engine.claude_code` bundle
  block (`binary`, `model`, `builtin_tools`, `permission_mode`, `on_denied`,
  `config_dir`) and a `claude-code` extra. The runtime image ships the engine and uses the
  SDK's bundled CLI unless a bundle names a binary
  (tenishai/daedalus#104, tenishai/daedalus#107, tenishai/daedalus#108, tenishai/daedalus#112).
- **Model tiers.** A bundle can set `model.id` to `fast`, `balanced` or `best`, and each
  deployment maps those words to real models once, in a YAML file named by `DAEDALUS_TIERS`
  (`examples/tiers/`). The same bundle then runs on Claude in one place and on a local
  model in another. The resolved model is logged, emitted as a `model_resolved` event and
  served on the A2A card as `daedalus_model`. The published bundle schema now accepts
  `${VAR}` placeholders in non-string fields, which the loader already expanded, and a
  conformance test checks real bundles against it
  (tenishai/daedalus#127, tenishai/daedalus#132).
- **`exec.run` builtin.** Runs a command, as an argv list and never a shell string, in the
  agent's own workspace, on any engine. It loads only with `approvals.ceiling.exec: true`,
  confines `cwd` to the configured roots, passes an allowlisted environment, kills the
  process group on timeout or cancel, and caps each output stream. It has no sandbox: the
  runtime image runs under `tini` so killed children are reaped
  (tenishai/daedalus#121).
- **Better file tools.** `files.read` takes `offset` and `limit`, so a bundle with a low
  byte cap can still read large files. New `files.search` is a rooted regex or plain-text
  content search that returns `path:line: text` ready for `files.read`
  (tenishai/daedalus#94, tenishai/daedalus#97).
- **Loop limits that return something.** `tools.on_exhausted: wrap_up` makes one last
  tool-free model call when steps or context run out and marks the reply as a partial
  answer. `tools.context_tokens` counts a run as exhausted once a prompt reaches that size.
  `tools.max_repeats` stops a model that repeats the same tool call with the same arguments
  (tenishai/daedalus#99, tenishai/daedalus#102).
- **`daedalus run --recall`** sets what personal memory recalls on, instead of the whole
  prompt, which a large payload swamps (tenishai/daedalus#123).
- **Token usage and cost.** `engine_finished` now carries input, output and cache token
  counts, the per-model breakdown and `total_cost_usd` when the SDK reports them, and
  `daedalus run` prints one `[usage]` line to stderr. Total input counts cache reads and
  writes (tenishai/daedalus#136, tenishai/daedalus#137).
- **Conversation journal and learnings.** Completed conversations are journaled, and a
  `learnings` system task summarizes the day into candidate statements, keeping only what a
  person said (tenishai/daedalus#37).
- **Images and new tool lanes** (tenishai/daedalus#37): image attachments in Slack reach the
  model, bounded and authenticated; read-lane sidecars `slack_read` and `jira`, with
  anchored time windows; a `cloudtrail` sidecar for provisioning history through workload
  identity federation; `github.push_branch`, `github.search_prs` and an exact-branch override
  on `github.open_pr`; a `liminal` sidecar; Slack markdown tables render as aligned
  monospace blocks; and the stable prompt prefix is cached.
- **Skills are addressable.** When `tools.allow` admits `skills.load`, the system prompt
  carries a `## Skills` index, one line per assigned skill with its id and summary, and
  the model calls `skills.load(name)` to read a skill in full before acting on it. The
  index is left out when the tool isn't allowed, since the agent couldn't open anything
  it names. Boot warns when a bundle has skills but no `skills.load`, and when a skill's
  `requires:` names tools the allowlist doesn't admit; neither stops the load. The
  seeder's summary breaks the count down by knowledge and skills
  (tenishai/daedalus#63, tenishai/daedalus#62).
- **A `techne` extra** with a source pin (`requirements-sources.txt`), since techne is not on
  PyPI (tenishai/daedalus#65).
- **Open-source community files.** `CONTRIBUTING.md` with the Developer Certificate of
  Origin, `CODE_OF_CONDUCT.md`, `SUPPORT.md`, `SECURITY.md`, issue and pull request
  templates, and this changelog (tenishai/daedalus#83, tenishai/daedalus#138).
- **A DCO check** on every pull request, and a Dependabot config for pip and GitHub Actions
  (tenishai/daedalus#138).
- **Architecture cards.** `ARCHITECTURE.md` is the repo card and `docs/arch/` holds a card
  per seam (a2a-wire, approvals, bundle, engines, protocols); each pins the files it
  describes by blob sha, and `scripts/check_arch_cards.py` fails when an anchored file
  changes without its card (tenishai/daedalus#109).

### Changed

- **Slack streamed replies say what the agent is doing** (tenishai/daedalus#160). The streamed
  message opens with "On it" the instant the stream starts, so a quick turn never
  shows an empty bubble. Each progress line is built from the tool's own name, in
  the present tense, and names only the object that matters ("Reading file
  teams/data.yaml in example/infra", "Asking ledger", "Thinking it over, with 2
  things from memory"), never a query's text; a tool whose name starts with no known verb reads as
  "Using" with its provider and name ("Using Datadog mute monitor"). Progress lines
  are italic text in the message, not `task_update` chunks, which a channel
  thread did not show at all. Once the stream closes, the message is rewritten
  to the answer alone when the answer fits one Slack message; a longer answer, or a
  rewrite Slack refuses, leaves the trail above the answer.
- **The GitHub tool's default head-branch prefix is now `daedalus/`, and it is configurable.**
  `open_pr` used to name every branch `kobo/<agent>/<slug>`, which hardcoded another
  product's name. The new `tools.github.branch_prefix` bundle setting (default `daedalus`)
  gives `<prefix>/<agent>/<slug>`, the tool description the model sees names the configured
  prefix, and a value that is not a valid git ref component fails at bundle load. Bundles
  that relied on the old `kobo/` prefix should set `branch_prefix: kobo`. An exact `branch`
  argument still wins (tenishai/daedalus#144, closes tenishai/daedalus#140).
- **A bare `- kind: slack` transport answers in every channel the app was invited to.** A
  non-empty `channel` list is still an allowlist. An empty `channel` key is now a boot
  error rather than "every channel", and `slack_read.channels: []` now turns the lane on
  (tenishai/daedalus#77).
- **Gated tools nobody can approve are withheld.** With no approver wired (a headless run),
  a gated tool is no longer offered and then refused on every call; a `tools_withheld`
  event says which, and the system prompt tells the model why
  (tenishai/daedalus#105).
- **`engine.claude_code.binary` defaults to unset**, so the SDK's bundled Claude Code CLI is
  used rather than a `claude` on `PATH` (tenishai/daedalus#112).
- **Image builds** take techne and liminal from sibling checkouts or from a package index,
  and `make image` passes both contexts (tenishai/daedalus#128).
- **Every third-party GitHub Action is pinned to a full commit SHA**, and the CI header
  comment no longer names the CI host (tenishai/daedalus#138).
- **The distribution is named `tenish-daedalus`** (tenishai/daedalus#163; pyproject `name`; was
  `daedalus` in 0.4.0). The bare name `daedalus` is taken or open to squatting on PyPI, and every
  released tenish Python distribution is `tenish-<project>`, as techne is `tenish-techne`
  and liminal `tenish-liminal`. tenishai/daedalus#138 first renamed it to
  `tenishai-daedalus`, a name no release used; this release settles on `tenish-daedalus`.
  pyproject's `name`, the install hints in error messages, the runtime image, AGENTS.md,
  docs/design.md and the repo card change; the import package, the CLI and the GitHub URL
  (`github.com/tenishai/daedalus`) do not.
- **The copyright holder is "tenish and the daedalus contributors"** in a new NOTICE file
  and the README, and pyproject's `authors` is `tenish`. The licence (AGPL-3.0-or-later)
  is unchanged (tenishai/daedalus#146).
- **The README says how daedalus was built, and CONTRIBUTING.md gives the policy for
  AI-assisted contributions** (tenishai/daedalus#153).
- **The README quickstart starts from a clone**, makes a venv, and lists the environment
  for an OpenAI-compatible endpoint (`OPENAI_API_BASE`, `OPENAI_API_KEY`,
  `DAEDALUS_MODEL=openai/<model>`). The tiers example and docstring use a localhost
  placeholder instead of a private host, and the docs describe the visual builder without
  naming private repos (tenishai/daedalus#139).
- **README rewritten**, `AGENTS.md` added, the design doc marked implemented, and the
  capability-seam proposal scrubbed of internal names (tenishai/daedalus#73, which
  re-applies tenishai/daedalus#68, tenishai/daedalus#70 and tenishai/daedalus#71 after the
  history rewrite dropped them; tenishai/daedalus#75).
- **CI** runs on a self-hosted pool with a fork guard, with a manual `techne` leg that fails
  rather than skips when techne is missing, uv-managed Python so `exec` tests can start it,
  and `docker/` collected and linted (tenishai/daedalus#76, tenishai/daedalus#92,
  tenishai/daedalus#134, tenishai/daedalus#61). pytest imports the checkout it runs in
  (tenishai/daedalus#74), and a test fails if any self-hosted job loses its fork guard
  (tenishai/daedalus#154).

### Fixed

- **A colleague's refusal is shown, and no longer bans it** (tenishai/daedalus#157, closes
  tenishai/daedalus#155 and tenishai/daedalus#56). `colleagues.ask_colleague` used to
  answer any non-200 with "Asking X failed: HTTP N." For a 4xx it now includes the switchboard's reason, capped at 300 characters. A 401, 403
  or 404 is worded as a refusal that won't change on retry, and no longer counts toward
  marking the colleague down, so a policy refusal can't hide a healthy colleague for five
  minutes. Other 4xx (400, 422, 429) and 5xx still count.
- **`colleagues.ask_colleague` is limited per answer** (tenishai/daedalus#150, closes
  tenishai/daedalus#149). One answer gets at most 3 colleague asks. A repeat ask to the same colleague returns the earlier reply
  without dispatching and doesn't count. A reply that opens with "I can't" / "I don't have
  access" turns colleague asks off for the rest of the answer, and the tool result says to
  tell the user plainly. An agent answering another agent (inbound
  `cabal_delegation_depth` ≥ 1) is not offered `ask_colleague` at all, so A → B → A can't
  happen. A resumed task restores its inbound depth from the checkpoint; its ask count
  starts again at zero, because the ledger isn't checkpointed. Before this, only the turn
  budget and cabal's depth bound stopped a hunt, because every "I can't" was an HTTP 200
  and counted as a success.
- **A failed colleague ask gives back its slot and can be retried** (tenishai/daedalus#159,
  closes tenishai/daedalus#151). An ask counts toward the per-answer limit, and its reply is
  reused for a repeat, only when the switchboard answered: a 200, or a 401/403/404 policy
  refusal that a retry won't change. A transport failure, a 5xx, another 4xx, asking
  yourself or an unconfigured ask gives the slot back, and a retry to the same colleague goes
  out again; the colleague-down breaker still bounds it.
- **A liminal refusal is reported as a refusal, and the turn goes on**
  (tenishai/daedalus#158, closes tenishai/daedalus#156). A reachable gateway that answers
  with a JSON-RPC error (such as "no policy grant") now reads "liminal at <url> refused the
  request: <message>" instead of "unreachable ... is it running?", which is kept for
  transport failures and whose start-it-yourself hint appears only for loopback URLs. When
  several tool providers are composed, `MultiToolProvider` drops a refusing provider's
  tools for the turn; the agent emits `tool_provider_refused` and tells the model, once,
  that those tools are refused by policy. An outage still fails the turn, and so does a
  refusal from the only provider.
- **The runtime image builds against a current liminal.** liminal's distribution became
  `tenishai-liminal` in its 0.3.0 (tenishai/liminal#118); `docker/Dockerfile` still asked
  pip for `liminal`, so a build with a current liminal checkout failed with "liminal
  (unavailable)" (tenishai/daedalus#147). Both the wheel step and the install now name
  liminal 0.4.0's distribution, `tenish-liminal`; the import package is unchanged.
- **The README quickstart runs as written on a fresh Ubuntu machine** (tenishai/daedalus#145):
  `sudo apt update` precedes the `python3-venv` install. Found by the 2026-10-02
  clean-machine run (tenishai/agora#47).
- **A removal the store refuses no longer stops the seeder.** One undeletable entry used to
  crash-loop an agent at boot; the refusal is logged and retried next boot
  (tenishai/daedalus#79).
- **The MCP provider works with mcp 2.x**, and a string `command` plus `args` is read the way
  most configs write it; a malformed one is refused at load, naming the server
  (tenishai/daedalus#118).
- **The checkpoint store closes each SQLite connection it opens**, instead of leaving it
  to garbage collection (tenishai/daedalus#142, closes tenishai/daedalus#95).
- **`files` builtin `max_bytes` set through `${VAR:-default}`** arrives as a string and made
  every `files.read` fail. It is converted at load, and a bad value is a load error
  (tenishai/daedalus#119).
- **A `requires:` typo in a skill** (`requires: yes` parses as a boolean) no longer takes the
  agent down at boot (tenishai/daedalus#64).
- **Slack:** a mention aimed at another agent is not answered, tagged-only is enforced
  runtime-side, one transport serves many channels, and replies and approvals land where the
  event happened (tenishai/daedalus#37).
- **Stop replies** (token budget or steps exhausted) now say what happened in plain words and
  point at the remedies, instead of leaking the loop's internal sentinel text into a Slack
  channel (tenishai/daedalus#37).

### Security

- **Learnings treat transcript text as untrusted.** Text cannot forge prompt structure, and
  `said_by` must name the speaker whose turn holds the quote (tenishai/daedalus#37).
- **An open Slack transport keeps tier-2 approvals closed**, and boot warns about it
  (tenishai/daedalus#77).
- **Slack tier-2 approvers come from the bundle.** The approver allowlist used to exist
  only as a constructor argument, so any member of an allowlisted channel, the requester
  included, could approve a tier-2 action. It is now the `approvers` list on the
  `kind: slack` block; with none, tier-2 is denied. The requester never approves their own
  request unless they are the sole listed approver, and the requester is tracked per task
  (tenishai/daedalus#141, closes tenishai/daedalus#53 and tenishai/daedalus#90).
- **The learnings task goes through the law layer.** It refuses when enforcement is locked
  and runs its reply through `post_draft` hooks (tenishai/daedalus#142, closes
  tenishai/daedalus#86).
- **A cold JWKS cache makes one fetch.** Concurrent A2A verifies on a fresh process share
  a single JWKS GET instead of each making one (tenishai/daedalus#142, closes
  tenishai/daedalus#55).
- **The CloudTrail sidecar names its STS session per agent** (`daedalus-<agent>`, was a
  constant), so AWS-side audit can tell agents apart, and its `event_data_store` is
  validated before it reaches the query's `FROM` (tenishai/daedalus#142, closes
  tenishai/daedalus#88).
- **The docs say what is open.** Slack DMs bypass the channel allowlist, with the tool
  ceiling as the only limit, and the A2A `/events` GET endpoint is open even with auth
  configured, a known hardening item (tenishai/daedalus#142, closes tenishai/daedalus#87,
  part of tenishai/daedalus#91).
- **`exec.run` is opt-in and says what it is not.** Its environment allowlist is not a
  boundary, since a command runs as the same user (tenishai/daedalus#121).
- **`SECURITY.md`** describes how to report a vulnerability privately
  (tenishai/daedalus#83).

## [0.4.0] — 2026-09-14

The first tag since v0.3.0, 91 commits earlier. The change that makes this release matter is
that the delegation hop count now crosses the A2A boundary (tenishai/daedalus#40): without it,
cabal's delegation-depth guardrail cannot fire on cross-agent traffic at all.

- **Colleagues:** a directory filter and a colleague-down breaker stop offering colleagues
  that cannot answer (tenishai/daedalus#38, tenishai/daedalus#39); agents find, ask and remember
  what their colleagues tell them; a quorum of specialists can change a belief, and the
  operator's word cannot be outvoted.
- **Enforcement:** the law layer loads, composes and fails closed, and every reply passes
  through it; a returned denial now denies, and hooks get their own pool
  (tenishai/daedalus#52).
- **Memory:** techne 0.16.1 with semantic recall, the teach lane, a knowledge lane, and a boot
  seeder that converges bundle knowledge and skills into the store.
- **Slack:** a thinking placeholder that becomes the reply, long replies chunked at safe
  boundaries, and the approver of a tier-2 action is recorded (tenishai/daedalus#54).
- **Tools:** the GitHub read quartet (`list_prs`, `get_pr`, `list_releases`, `read_file`)
  and a `github.open_pr` sidecar with attribution enforcement.
- **A2A auth:** revocation has a TTL and an unknown `kid` a cooldown
  (tenishai/daedalus#50); the teach operator shield checks a prefix that is actually minted
  (tenishai/daedalus#49).
- **Runtime image:** the `daedalus-runtime` base image with a seed-then-run entrypoint.
- **Contract:** `docs/bundle.schema.json` is the published bundle schema and embeds the version.
- **Known, not fixed here:** the colleague breaker counts a 403 policy refusal as a transport
  failure (tenishai/daedalus#56).

## [0.3.0] — 2026-07-06

- **A2A transport** with agent-card serving and JSON-RPC, plus the cabal peer surface
  (`/a2a/tasks`, dual-key reason codes, JWT verification).
- **Slack transport** with reaction approvals and tier-3 escalation.
- **liminal tool gateway** as a `ToolProvider`; a down provider degrades to a task failure.
- **Events:** an `EventBus` and an SSE `/events` stream make the trace attachable.

## [0.2.0] — 2026-07-05

- Bundle loader, agent runtime and CLI transport (`daedalus create`, `run`, `chat`).
- Tool loop with an MCP provider and approval tiers 1 and 2, plus read-only builtin
  `files` tools.
- techne-backed memory, checkpoints and a token budget.
- Relicensed from Apache-2.0 to AGPL-3.0-or-later.
