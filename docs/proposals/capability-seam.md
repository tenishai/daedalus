# The capability seam — how fleet agents get, discover, and gate tools

*Proposal, 2026-07. Ember. Input from the Iris working session (fleet-side
Ember + Shaz) and the whiteboard target architecture (Iris / Rook Remote /
Clio over cabal, per-agent techne, shared Archive/Library). Discussion
doc for Shaz, Rich, and both Embers — nothing here is built yet except
where marked GROUND TRUTH.*

## 1. Ground truth — what the runtime does today

Correcting some drift between fleet-side sketches and the shipped
runtime (daedalus 0.4.0, 419 tests):

- **Hybrid is not a proposal — it's how scout runs.** `tools.builtin`
  composes with `provider: mcp|liminal` through `MultiToolProvider`;
  one merged tool list reaches the model.
- **`tools.builtin` is a configured dict, not a name list** —
  `files: {roots: [...]}` — and `files` is today's only group.
- **`tools.allow` is a single global fnmatch ceiling** across all
  providers, enforced at listing (the model never sees a tool it can't
  call) AND at call time (hallucinated names miss the map). There is no
  per-provider allow list.
- **Empty allow = deny everything.** Fail closed. This is the runtime's
  most load-bearing safety invariant.
- **`approvals.ceiling.write/hosts` is parsed but UNENFORCED** — the
  allow list is tier 1 in practice. (Either enforce these fields
  someday or remove them; tracked as part of this design.)
- **Gates are first-match fnmatch on canonical names**
  (`approvals.gates`), so a trailing `match: "*"` works as a catch-all.
- **Discovery is already dynamic.** The ceiling filters whatever the
  provider offers *per task* — a new gateway capability appears to any
  bundle whose ceiling matches it, no yaml edit required.
- **Transports are faces, not identities.** One bundle serves cli, a2a,
  and Slack simultaneously. "Slack-resident agent vs headless service"
  is a `transports:` list, not an architectural fork. (Sherlock can keep
  his channel and persona while a2a makes him fleet infrastructure; the
  capacity question that follows belongs to cabal's bulletin board, not
  the bundle.)

## 2. The problem

The fleet pattern converging out of Iris/Cinder: domain agents with
local builtin tools (kubectl, gcloud — fast, scoped, no hop) plus shared
fleet capabilities through liminal (`sherlock.holmes_ask`,
`prudence.review_pr`, `archivist.search`, `thoth.search_docs`). Two
frictions:

1. **Enumeration drift.** Requiring every agent.yaml to list every
   fleet capability means N bundles to edit whenever the fleet grows.
2. **Local tools don't scale by fork.** Iris's kubectl set shouldn't
   live in daedalus core, and shouldn't require forking daedalus either.

And one hazard the fleet discussion surfaced but didn't close: with any
wide-open ceiling, **a newly appearing mutation tool runs ungated**
unless a gate pattern happens to match it. Gates are opt-in by name.

## 3. Proposals

### A. `tools.deny` — explicit openness, fail-closed default (daedalus)

Add a block list applied after the allow ceiling:

```yaml
tools:
  allow: ["*"]                      # explicit, visible operator choice
  deny:
    - "sherlock.holmes_investigate_alert"   # too expensive here
    - "files.write"
```

- Empty `allow` **stays** deny-everything. Openness is never a default
  or an absence — `allow: ["*"]` is written in the bundle, reviewable
  in the PR, visible in kaminos.
- `deny` uses the same fnmatch, wins over allow, enforced at both
  listing and call time like the ceiling.
- Small, additive; changes `AgentSpec` → regenerate
  `docs/bundle.schema.json` (the drift guard enforces this).

Rejected alternative: default-open with deny-only ("no allow list =
discover everything"). It inverts the fail-closed invariant silently —
the exact silent-failure family we keep filing issues about.

### B. Entry-point builtin tool groups (daedalus)

Builtin groups become discoverable via a Python entry point, so fleet
repos ship their own without touching daedalus:

```toml
# fubo-k8s-tools pyproject.toml
[project.entry-points."daedalus.tool_groups"]
kubectl_read = "fubo_k8s_tools.daedalus:KubectlReadProvider"
```

```yaml
tools:
  builtin:
    files: {roots: ["/home/iris/work"]}
    kubectl_read: {contexts: ["vidai-prod", "vidai-dev"]}   # from the entry point
```

Unknown group name → same load-time BundleError as today, now listing
installed groups. `files` stays in-tree as the reference
implementation. The provider contract is the existing `ToolProvider`
protocol (tools()/call()); config dict passes to the constructor,
invalid config fails at load, not mid-conversation.

### C. Card-driven auto-gating (daedalus + liminal, coordinated)

Liminal capability cards already declare what each handler covers, and
liminal now derives per-tool scopes from cards (#41/#43). Add one field
to the card contract: **`mutating: true|false`** (Rich's call on the
exact name/shape). Then:

- daedalus's liminal provider reads card metadata; any tool marked
  mutating is **auto-gated** (`plan_approval`) unless the bundle
  already has a more specific gate for it.
- Safety defaults travel WITH the capability, set by its author —
  agents can tighten, not loosen. The governance model the platform
  design called for, with a mechanism.
- Until this lands, the documented mitigation for wide ceilings is a
  trailing catch-all gate:

```yaml
approvals:
  gates:
    - match: "pod_delete"
      require: plan_approval      # specific first
    - match: "*"
      require: plan_approval      # safety net for tools born tomorrow
```

(Open question: MCP-provider tools have no cards. Options: treat
metadata-less tools as mutating (strict), or leave them to explicit
gates (status quo). Proposal: strict mode as an opt-in
`tools.gate_unknown: true`, default off for now, revisit after fleet
experience.)

### D. Fleet read-policy lives in the gateway (liminal)

"Every domain agent gets fleet reads" is a *policy*, and liminal just
grew the policy layer for it: per-tool scope enforcement with scopes
derived from handler cards. The fleet deployment grants agent tokens
the read scopes; mutations require explicitly granted scopes. The
bundle ceiling remains the agent-side belt to the gateway's
suspenders — two independent fail-closed layers, either alone
sufficient to stop a surprise.

### E. The kaminos capability palette (kaminos, next arc after v0.5)

Liminal's card set IS the palette. Kaminos queries the gateway, shows
every capability with its card (description, credentials, mutating
flag), overlays the current bundle's effective ceiling
(allow ∩ ¬deny), and lets the operator toggle — writing `allow`/`deny`
back through the existing diff-before-save flow. The Interface Builder
model: see the palette of available objects, drag what you need,
inspect what you got.

## 4. Related, deliberately not covered here

- **The skills layer** (on-demand instruction packs, the
  `gs://fubo-skills` pattern) — the four-layer model's remaining gap in
  the bundle format. Separate design; it will want its own bundle
  section and schema rev. Don't conflate instructions (context) with
  tools (capabilities).
- **Sherlock's deployment shape** — see ground truth: a transports
  decision plus a cabal capacity question, not a bundle-format one.
- **The Archive/Library** — the techne outbox's landing zone. Separate
  design.

## 5. Decisions needed

| # | Decision | Whose call |
|---|---|---|
| 1 | `tools.deny` semantics as specified (A) | Shaz |
| 2 | Entry-point name `daedalus.tool_groups` + provider contract (B) | Shaz |
| 3 | Card `mutating` field name/shape in liminal (C) | Rich |
| 4 | Auto-gate default for metadata-less tools (C, open q) | Shaz + fleet experience |
| 5 | Fleet read-scope grant convention in liminal deploys (D) | Rich + fleet Ember |
| 6 | Palette as the kaminos arc after v0.5 (E) | Shaz |

A + B are small, self-contained daedalus PRs and unblock Iris-as-bundle
immediately. C needs Rich. D is deployment config, not code. E waits for
kaminos PR #3 (v0.5) to merge.

— Ember (SRE AI)
