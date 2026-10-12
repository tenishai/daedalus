# Changelog

All notable changes to daedalus are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic versioning.
Issue and pull request numbers all refer to this repository, written in full.

## [Unreleased]

Everything since v0.4.0 (2026-09-14). `__version__` is still 0.4.0.

### Upgrading

- **techne 0.18.2, under its new distribution name `tenish-techne`.** The `techne` extra
  needs `tenish-techne[vectors]>=0.18.2` (was `techne[vectors]>=0.17.0`), and
  `requirements-sources.txt` pins the `v0.18.2` tag. The import package and the `techne`
  command are unchanged. Run `pip uninstall techne` before reinstalling: pip treats the two
  names as unrelated projects and would leave the old one installed beside the new one.
  `docker/Dockerfile` installs and asserts 0.18.2, from a sibling checkout or an index.

### Fixed

- **A colleague's refusal is shown, and no longer bans it** (issues #155 and #56).
  `colleagues.ask_colleague` used to answer any non-200 with "Asking X failed: HTTP N."
  For a 4xx it now includes the switchboard's reason, capped at 300 characters. A 401, 403
  or 404 is worded as a refusal that won't change on retry, and no longer counts toward
  marking the colleague down, so a policy refusal can't hide a healthy colleague for five
  minutes. Other 4xx (400, 422, 429) and 5xx still count.
- **`colleagues.ask_colleague` is limited per answer** (issue #149). One answer gets at
  most 3 colleague asks. A repeat ask to the same colleague returns the earlier reply
  without dispatching and doesn't count. A reply that opens with "I can't" / "I don't have
  access" turns colleague asks off for the rest of the answer, and the tool result says to
  tell the user plainly. An agent answering another agent (inbound
  `cabal_delegation_depth` ≥ 1) is not offered `ask_colleague` at all, so A → B → A can't
  happen. A resumed task restores its inbound depth from the checkpoint; its ask count
  starts again at zero, because the ledger isn't checkpointed. Before this, only the turn budget and cabal's depth bound stopped a hunt,
  because every "I can't" was an HTTP 200 and counted as a success.
- **The runtime image builds against liminal 0.3.0.** liminal's distribution became
  `tenishai-liminal` (tenishai/liminal#118); `docker/Dockerfile` still asked pip for
  `liminal`, so a build with a current liminal checkout failed with "liminal
  (unavailable)". Both the wheel step and the install name the new distribution; the
  import package is unchanged.

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
  is followed again.
- **Slack replies stream, with progress.** The working message is a `chat.startStream`
  stream: each tool call, and each memory recall that finds something, appears as a
  progress line under it (a repeat straight after the same line is not shown again), and the answer lands in the same message. Events are stamped with their
  task's context id, so two threads working at once never show each other's steps. A
  workspace that refuses the stream gets the hourglass placeholder as before. The
  `slack` extra now needs slack-sdk 3.40 or later.

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
- **Skills are addressable.** Skills assigned to a bundle are listed to the agent, and a
  skill that cannot work says so. The seeder's summary breaks the count down by knowledge
  and skills (tenishai/daedalus#63, tenishai/daedalus#62).
- **A `techne` extra** with a source pin (`requirements-sources.txt`), since techne is not on
  PyPI (tenishai/daedalus#65).
- **Open-source community files.** `CONTRIBUTING.md` with the Developer Certificate of
  Origin, `CODE_OF_CONDUCT.md`, `SUPPORT.md`, `SECURITY.md`, issue and pull request
  templates, and this changelog (tenishai/daedalus#83, tenishai/daedalus#138).
- **A DCO check** on every pull request, and a Dependabot config for pip and GitHub Actions
  (tenishai/daedalus#138).

### Changed

- **Slack streamed replies read as what the agent did** (PR #160). The streamed
  message opens with "On it" the instant the stream starts, so a quick turn never
  shows an empty bubble. Each progress line says what the step did and names only
  the object that matters ("Read teams/data.yaml in example/infra", "Asked
  ledger", "Recalled 2 things from memory"), never a tool's internal name or a
  query's text; unknown tools read as their provider ("Used Atlassian").

- **The GitHub tool's default head-branch prefix is now `daedalus/`, and it is configurable.**
  `open_pr` used to name every branch `kobo/<agent>/<slug>`, which hardcoded another
  product's name. The new `tools.github.branch_prefix` bundle setting (default `daedalus`)
  gives `<prefix>/<agent>/<slug>`, the tool description the model sees names the configured
  prefix, and a value that is not a valid git ref component fails at bundle load. Bundles
  that relied on the old `kobo/` prefix should set `branch_prefix: kobo`. An exact `branch`
  argument still wins. Closes #140.
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
- **The distribution is named `tenishai-daedalus`** (pyproject `name`; was `daedalus`). The
  bare names `daedalus` and `techne` are taken or open to squatting on PyPI. The import
  package (`import daedalus`) and the CLI (`daedalus`) are unchanged; install extras as
  `tenishai-daedalus[a2a]`, and `pip show tenishai-daedalus` is how to look it up
  (tenishai/daedalus#138).
- **README rewritten**, `AGENTS.md` added, the design doc marked implemented, and the
  capability-seam proposal scrubbed of internal names (tenishai/daedalus#73,
  tenishai/daedalus#75).
- **CI** runs on a self-hosted pool with a fork guard, with a manual `techne` leg that fails
  rather than skips when techne is missing, uv-managed Python so `exec` tests can start it,
  and `docker/` collected and linted (tenishai/daedalus#76, tenishai/daedalus#92,
  tenishai/daedalus#134, tenishai/daedalus#61). pytest imports the checkout it runs in
  (tenishai/daedalus#74).

### Fixed

- **The README quickstart runs as written on a fresh Ubuntu machine** (tenishai/daedalus#145):
  `sudo apt update` precedes the `python3-venv` install. Found by the 2026-10-02
  clean-machine run (tenishai/agora#47).
- **A removal the store refuses no longer stops the seeder.** One undeletable entry used to
  crash-loop an agent at boot; the refusal is logged and retried next boot
  (tenishai/daedalus#79).
- **The MCP provider works with mcp 2.x**, and a string `command` plus `args` is read the way
  most configs write it; a malformed one is refused at load, naming the server
  (tenishai/daedalus#118).
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
- **An open Slack transport keeps tier-2 approvals closed**, and boot warns about it, until
  the approver allowlist is wired from configuration (tenishai/daedalus#77).
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
