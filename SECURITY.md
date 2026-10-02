# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem.

Report it privately to **security@tenish.ai**.

If this repository's **Security** tab shows a **Report a vulnerability**
button, you can use that instead. GitHub offers it only on public
repositories where it has been turned on.

Include what you can of:

- what the problem is and what an attacker could do with it;
- the version, commit, or tag you tested;
- steps to reproduce, or a proof of concept;
- any configuration it depends on.

You don't need a complete write-up to report. A clear description of the
problem is enough to start.

## What happens next

- We acknowledge your report within **14 days**.
- We tell you whether we've confirmed the problem as soon as we know.
- We fix confirmed problems in the next release, or sooner for serious ones,
  and tell you before the fix is public.
- We credit you in the [release notes](CHANGELOG.md) unless you'd rather we didn't.

We ask that you give us a reasonable chance to fix a problem before disclosing
it publicly, and that you don't access or change data that isn't yours while
testing.

## Supported versions

daedalus is pre-1.0. Security fixes go into the **latest release** only; older
releases are not patched. If you're running an older version, upgrade to get
fixes.

## Scope

daedalus is the runtime agents run in. In scope:

- enforcement: any way around the operator's controls on what an agent may do;
- identity: JWKS handling, and anything that lets a message or caller
  impersonate the operator or another agent;
- untrusted input: anything a chat message, transcript or tool result can use
  to change an agent's instructions, memory or permissions (prompt injection
  that crosses a trust boundary the runtime claims to enforce);
- the a2a HTTP transport and its bearer/JWT authentication: when `auth` is
  configured, anything that reaches an agent through a POST without passing
  it. Without an `auth` block the POST surfaces are open by design, as are the
  GET endpoints (card, health, events);
- the Slack transport and other connectors: anything that lets someone other
  than an intended approver approve an action. Tier-2 approvals need an
  `approvers` list (Slack user ids) and a `channel` allowlist on the Slack
  transport block; without them every tier-2 action is denied. For a request that
  started in Slack, the person who sent it cannot approve it unless they are
  the only listed approver, a deliberate solo-operator choice. A request that
  did not start in Slack (a2a, cli, a colleague agent) has no known Slack
  requester, so this self-exclusion does not apply to it. `approvers` is
  transport-wide, not per channel. Reports that get around any
  of these rules are in scope.

**Out of scope:** vulnerabilities in third-party dependencies (please report
those upstream, and tell us if we ship an affected version), and problems that
require an attacker to already control the machine or the operator account
daedalus runs under.
