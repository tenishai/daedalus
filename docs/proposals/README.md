# Proposals — design discussion, not documentation

Nothing here describes current behaviour. For what daedalus does today, read
the [README](../../README.md); for the architecture and its rationale, read
[design.md](../design.md), with `src/daedalus/protocols.py` as the authority on
the seams.

These documents were written inside a working fleet, and they read like it.
They name **agents and people by internal names** — Iris, Cinder, Clio, Ember,
Sherlock, "Rook Remote" — and assign decisions to individuals. None of that is
explained anywhere in this repo, because none of it is part of daedalus.

Read them for the technical argument, which is real: the entry-point tool-group
seam, the approval-ceiling analysis, and what the capability card has to carry.
Treat every name as "some agent" or "the person who owned that decision", and
treat every build-status claim as dated.

| file | what it is |
|---|---|
| `capability-seam.md` | 2026-07 design discussion: how tool groups and capability cards should compose across a fleet. Its ground-truth line (version and test count) was refreshed 2026-09-22 and matches the current release; the *design* discussion around it is still from July. |
