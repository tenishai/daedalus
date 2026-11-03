# AGENTS.md — daedalus

Instructions for AI coding agents working in this repo. Claude Code loads this via
`CLAUDE.md` (`@AGENTS.md`); other AGENTS.md-aware tools read it directly.

`daedalus guide welcome` prints the user-facing tour of daedalus and its options from the
installed package; the guides live in `docs/` and ship via pyproject's force-include.

## Build, test, lint

```bash
make install    # pip install -e .[dev]
make test       # pytest -q
make lint       # ruff check src tests docker conftest.py
make check      # lint + test
```

`testpaths = ["tests", "docker"]` — `docker/test_seeder.py` is a real test file and a
contributor who only looks in `tests/` will miss it. ruff is `line-length = 100`,
`select = ["E","F","I","UP","B"]`.

## The rule that shapes everything: the base install stays small

Base dependencies are `pydantic`, `pyyaml`, `click`, `litellm`. That is the whole list.
techne (memory), liminal (tools) and cabal (orchestration) are **optional extras behind
Protocol seams** — an agent with no memory and no tool gateway still runs, and that is
the product, not a degraded mode.

So: **never add a module-scope import of an optional dependency.** `agent.py` defers
`import fastapi` and `import slack_sdk` inside the functions that need them. Follow that
pattern; a top-level import turns an extra into a requirement and nothing in CI will tell
you, because the dev environment has everything installed.

`src/daedalus/protocols.py` is the authority on the seams. `docs/design.md` is the
rationale and predates much of the implementation — where the two disagree, the code is
right.

## Traps that have already cost someone

- **techne is not on PyPI, and the default `test` CI job does not install it.** That job
  runs `pip install -e .[dev]`; techne lives in the `techne` extra. So every techne-path
  test skips there, and `pytest -q` prints `13 skipped` — which is not 13 tests. A
  module-level `importorskip` collapses a whole file into one `s`, and the three guarded
  files hold **92 tests** between them — 22 + 23 + 47, counted the way pytest collects,
  which is not the same as counting `def test_` (#48). A memory-path change can look
  tested when nothing ran it. Install a sibling checkout (`pip install -e ../techne`)
  before trusting a green run on that path, and run `pytest -rs` to see what the summary
  line hides. `tests/test_skip_counts_are_honest.py` keeps those numbers true; it does
  not make the tests run.

  A separate `techne` CI job installs tenish-techne from the tenish index (the
  `techne` extra, read-only token in the `TENISH_INDEX_TOKEN` secret) and runs the full
  suite with `DAEDALUS_REQUIRE_TECHNE=1`, which turns a missing/broken techne import into
  a hard failure instead of a skip (`conftest.py`). It runs on every push to main and
  every same-repo PR (#48); fork PRs get no secrets and skip it. Dependabot PRs read
  Dependabot secrets only, so the token is stored under the same name in both places;
  rotate both together.
- **The distribution is `tenish-daedalus`; the import package and the CLI are `daedalus`.**
  Anything that looks the package up by distribution name (`pip show`, `importlib.metadata`,
  an extras install string) needs the long name.
- **The version lives in `src/daedalus/__init__.py`**, not in `pyproject.toml`
  (`[tool.hatch.version]` points at it). Bumping pyproject silently does nothing.
- **`docs/bundle.schema.json` is drift-pinned** to `AgentSpec` by
  `tests/test_schema.py::test_committed_schema_cannot_drift_from_the_runtime`. Change an
  `AgentSpec` field and you must run `daedalus schema --out docs/bundle.schema.json` in
  the same commit or CI fails.
- **Operator-gate events are matched by exact name**, in the `_OPERATOR_GATE_EVENTS`
  frozenset in `src/daedalus/cli.py` — deliberately not by suffix. The comment there
  records the incident (#49) where a suffix match swallowed two unrelated events. Adding a
  gate event means adding it to that set explicitly.
- **Golden bundles are documentation.** `tests/golden/<name>/` bundles are loaded and
  round-tripped as tests. A new bundle-format feature wants a golden fixture, not only a
  unit test.

## Directory boundaries

```
src/daedalus/
  protocols.py     the Protocol seams — the contract optional extras implement
  agent.py         the runtime loop
  bundle.py        agent.yaml / persona.md / agent-card.json loading
  transports/      cli, a2a, slack — one file per transport
  memory/          techne adapter (optional)
  tools/           tool providers (optional)
  enforcement.py   the hook law layer (bundle/tools/*.py pre_task/post_draft); tiers 1-2 are in agent.py
tests/golden/      bundles that are both fixture and worked example
docs/proposals/    design notes, not documentation of current behaviour
```

## Architecture cards

`ARCHITECTURE.md` is the repo card: what this repo is, its entry points, and an index of its seams. Each seam that has a card lists it under `SEAMS → card:`, pointing at `docs/arch/<seam>.md`. Read the card before changing the code it describes.

Every card pins the file it describes with an `anchor: <path> sha=<git blob sha>` line in its frontmatter. When you change an anchored file, update the card in the same change: fix the text, set `updated:`, and replace the sha with `git hash-object <path>`. To add a seam card, add its line to the `SEAMS → card:` index too.

Check before you push:

```
python3 scripts/check_arch_cards.py
```

It fails when an anchored file has changed without its card, or when a card and the index disagree. CI runs it in the `test` and `fork-pr` jobs (#206), so a miss fails the build.
