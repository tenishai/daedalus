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
- kaminos — the builder GUI (companion, not yet released)

> Status: 0.4.0, released. Bundle loader, runtime, CLI/A2A/Slack transports,
> approvals and enforcement have all shipped. Design rationale:
> [docs/design.md](docs/design.md) — where it disagrees with the code, the
> code is right.

## Quickstart

Python 3.11+.

```bash
pip install -e .                    # not on PyPI yet
export ANTHROPIC_API_KEY=...        # or any provider litellm supports
daedalus create prudence            # scaffolds the bundle below
daedalus chat ./prudence            # talk to it
```

`daedalus create` writes an agent.yaml whose model reads
`${DAEDALUS_MODEL:-anthropic/claude-sonnet-4-5}`, so `export DAEDALUS_MODEL=…`
switches provider without editing the bundle. Any litellm route string works,
including a local endpoint via `api_base`. The other commands are
`daedalus run` (serve the declared transports), `version` and `schema`.

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
presence. The GUI (kaminos) edits the same files. No code generation,
ever: the bundle is the source of truth, diffable and PR-reviewable.

## License

Copyright (C) 2026 tenishai

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
