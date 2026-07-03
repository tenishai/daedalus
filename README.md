# daedalus

An open-source agent construction kit. An agent is a directory — a persona,
an A2A agent card, and a wiring file — and daedalus is the runtime that
brings it to life: pluggable transports (CLI, A2A, Slack), a three-tier
approval model, and Protocol seams for memory, tools, and checkpoints.

Part of the tenish family:

- [techne](https://github.com/dimartin/techne) — memory
- [limenal](https://github.com/dimartin/limenal) — tool gateway
- cabal — orchestration
- ladon — secrets
- gojira — deploy
- **daedalus** — agent construction kit (this repo)
- kaminos — the builder GUI (companion, coming)

> Status: design. See [docs/design.md](docs/design.md). No usable code yet.

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
presence. The GUI (hephaestus) edits the same files. No code generation,
ever: the bundle is the source of truth, diffable and PR-reviewable.

## License

Apache-2.0
