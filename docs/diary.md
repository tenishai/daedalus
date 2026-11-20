# Seeding a specialist from a transcript diary

An agent can start life with a history: the sessions its operator already had about the
thing it will support, indexed into a [Daffyne](https://github.com/tenishai/daffyne) diary
and mounted beside its memory. Every question the agent is asked goes through techne's
recall, techne consults the diary, and each row reaches the model with where and when it was
said. The agent answers with the decision and a `file:line` anchor, not from its persona
alone; and when nothing in the diary touches the question, it says so.

The transcripts themselves never enter the pod. Only the index does, and it is built with
secrets scrubbed.

## 1. Build the index on your machine

Daffyne's own README has the full story ("Building an index to ship"). The short form: a
config whose roots are the session directories about the subject, with only the typed and
thought rows kept and redaction on.

```yaml
# ~/diaries/widget/daffyne.yaml
db: /home/you/diaries/widget/daffyne.duckdb
roots:
  claude-code: [/home/you/.claude/projects/-home-you-src-widget]
kinds: [text, thinking]
redact: true
```

```sh
DAFFYNE_CONFIG=~/diaries/widget/daffyne.yaml daffyne scan
```

One project of a few hundred sessions indexes in seconds and ships as one file well under
a hundred megabytes. Count secret shapes in it before it leaves (Daffyne's README shows
how); the counts should be zero.

## 2. Put two files on the memory volume

The bundle's `memory.home` (`/techne` in the runtime image) already holds `techne.db`. Add
`daffyne.duckdb` and a `daffyne.yaml` whose `db:` names that path:

```yaml
# /techne/daffyne.yaml
db: /techne/daffyne.duckdb
roots: {}
```

The entrypoint exports `DAFFYNE_CONFIG=/techne/daffyne.yaml` when that file is there and
nothing set the variable. Daffyne opens its index read-only; the agent never writes to it.

## 3. Name the reader in the bundle

```yaml
memory:
  personal: techne
  home: /techne
  recall_sources: [daffyne]
```

`recall_sources` lists installed `techne.recall_sources` entry points by name. daedalus adds
them to techne's `recall.allowed_sources` in memory on every load; the techne home's own
`config.yaml` is left as it is. An entry point that is named but not installed is reported
by techne as unavailable and the agent carries on with its own memory.

The runtime image carries the reader only when built with it: `make image DIARY=1` (a
sibling `../daffyne` checkout, or `--build-arg DIARY=1` with `INDEX_URL` set), or
`pip install 'tenish-daedalus[diary]'` for a local run.

## 4. What the model sees

```
## What you remember
Facts you were taught or learned earlier, and rows from the diaries you were given. A line
marked (memory) is something you know: treat it as known and say it is remembered. A line
marked with another source, a date and a link is someone else's words from that time: cite
it with its date, never claim it as your own experience.
- (memory) the widget team ships on Thursdays
- (daffyne, 2026-05-06, file:///home/you/.claude/projects/-home-you-src-widget/8fb9.jsonl#L13660) we settled it: the widget cache uses request coalescing
```

A persona can lean on that framing; the golden bundle `tests/golden/diarist/` is a worked
example.

## Acceptance

Ask the agent a question that was settled in one of the indexed sessions: it answers with
the decision, the date, and the anchor. Ask it a question nothing in the index touches: it
says so instead of inventing.

## Optional: the handbook layer

`techne import <the same session directory>` into the agent's techne home gives techne's
observations and threads over the same history. Measured on one project: 274 sessions became
18,227 observations and 906 threads, and 0 knowledge rows until a distillation pass runs.
The diary answers on day one; the handbook grows from there.
