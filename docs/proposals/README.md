# Proposals — design discussion, not documentation

Nothing here describes current behaviour. For what daedalus does today, read
the [README](../../README.md); for the architecture and its rationale, read
[design.md](../design.md), with `src/daedalus/protocols.py` as the authority on
the seams.

These documents were written inside a working fleet, and they read like it:
they assign decisions to individuals and reference a specific fleet's
internal working sessions. None of that is part of daedalus, so
`capability-seam.md` has been generalized — agents and people referred to
generically ("the daedalus maintainer", "the operator", "a Slack-resident
agent") rather than by internal name. Its one personal path was replaced with
a placeholder earlier, in #71.

Read them for the technical argument, which is real: the entry-point tool-group
seam, the approval-ceiling analysis, and what the capability card has to carry.
Treat every build-status claim as dated.

| file | what it is |
|---|---|
| `capability-seam.md` | 2026-07 design discussion: how tool groups and capability cards should compose across a fleet. Its ground-truth line (version and test count) was refreshed 2026-09-22 and matches the current release; the *design* discussion around it is still from July. |
