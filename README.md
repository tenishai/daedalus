# daedalus

An open-source agent construction kit. An agent is a directory — a persona,
an A2A agent card, and a wiring file — and daedalus is the runtime that
brings it to life: pluggable transports (CLI, A2A, Slack), a three-tier
approval model, and Protocol seams for memory, tools, and checkpoints.

**daedalus runs on its own.** Its only dependencies are pydantic, pyyaml,
click and litellm. The tenish projects below are optional extras behind
Protocol seams, not requirements — an agent with no memory and no tool
gateway still runs.

- [techne](https://github.com/tenishai/techne) — memory (optional)
- [liminal](https://github.com/tenishai/liminal) — tool gateway (optional)
- cabal — orchestration (optional)
- **daedalus** — agent construction kit (this repo)
- a visual builder GUI — a companion project, in development

> Status: 0.5.2, released. Bundle loader, runtime, CLI/A2A/Slack transports,
> approvals and enforcement have all shipped. Design rationale:
> [docs/design.md](docs/design.md) — where it disagrees with the code, the
> code is right.

## Quickstart

Python 3.11+ and git. On Debian or Ubuntu, install the venv module first:
`sudo apt update && sudo apt install python3-venv git`.

```bash
git clone https://github.com/tenishai/daedalus.git
cd daedalus
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                    # not on PyPI yet
```

Point it at a model. For any OpenAI-compatible endpoint (vLLM, ollama, a hosted gateway):

```bash
export OPENAI_API_BASE=http://localhost:8000/v1   # your endpoint
export OPENAI_API_KEY=...                         # any non-empty string if the server ignores it
export DAEDALUS_MODEL=openai/<model-name>         # the name the endpoint serves
daedalus create prudence            # scaffolds the bundle below
daedalus chat ./prudence            # talk to it
```

For Anthropic, set `ANTHROPIC_API_KEY` instead of the three variables above; the default
model is `anthropic/claude-sonnet-4-5`. Any provider litellm supports works the same way.

`daedalus create` writes an agent.yaml whose model reads
`${DAEDALUS_MODEL:-anthropic/claude-sonnet-4-5}`, so `export DAEDALUS_MODEL=…`
switches provider without editing the bundle. Any litellm route string works,
including a local endpoint via `api_base` in agent.yaml. The other commands are
`daedalus run` (serve the declared transports, or `-m "text"` for one reply),
`version` and `schema`.

## The idea in one file tree

```
prudence/
├── agent-card.json    # who the agent is (A2A standard)
├── persona.md         # how it behaves
└── agent.yaml         # what it's wired to
```

```bash
daedalus chat ./prudence     # talk to it locally
daedalus run  ./prudence     # serve its declared transports
```

Same bundle, three runtimes — a chat REPL, an A2A endpoint, a Slack
presence. A companion GUI is planned to edit the same files. No code generation,
ever: the bundle is the source of truth, diffable and PR-reviewable.

## How this was built

tenishai is a small team of engineers who build with AI coding agents. Most of
this code was written in pairing sessions with Claude models in Claude Code: a
person set the design, directed the work and reviewed what landed, and the
agent wrote much of the code and docs. Many commits name the agent in a
`Co-Authored-By:` trailer, either a Claude model or Poe (earlier Raven), the
name of the agent one of us pairs with. A commit without a trailer may still
have had AI help.

## License

Copyright (C) 2026 tenish and the daedalus contributors

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along
with this program. If not, see <https://www.gnu.org/licenses/>.

Full text: [LICENSE](LICENSE).
