"""audit.trace -- the access-provenance evidence chain, run in a fixed order.

The skills already say the order: provisioning history first, then the
ticket and the request thread anchored on the event's timestamp. A model
still skips or reorders steps, guesses a search term, or stops at the first
empty search. This tool runs the chain itself and hands the model one
evidence pack to reason over:

1. The `cloudtrail` lane (the identity provider's system log): every group
   add and remove for the person, plus account lifecycle (create, activate,
   deactivate, suspend).
2. For each add being asked about, the issue tracker through the gateway's
   Jira tools: the access-request project, searched for the person's FULL
   email (a bare local part misses) in a window around the add, widened
   once when empty. Each candidate is read: does it name the group, was it
   created before the add, who is the approver, when was it approved.
3. The request thread: the one the ticket (or its parent) names, when the
   request bot writes the thread into the description (`thread_marker`),
   else an anchored search of the `slack_read` channel that keeps only a
   thread carrying the ticket's key. Its keys and reactions (the approval
   emoji) come back with it.

Everything org-specific (tracker site and project, group naming, the request
bot's phrasing) comes from the bundle's `tools.audit_trace` block. It calls
the agent's own providers in process, so it has exactly the reach and
credentials the agent already has, and it never raises into the loop: every
dead end is a line in the pack.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
_FIND_TS = re.compile(r"^Thread (\d{10}\.\d{6})", re.M)
_LOOKBACK_DAYS = 1825
_MAX_EVENTS = 6
_MAX_TICKETS = 4


def _untrusted_json(text: str):
    """The gateway wraps a federated result in BEGIN/END marker lines; the
    payload between them is JSON."""
    start = text.find("]{")
    end = text.rfind("[END UNTRUSTED")
    body = text[start + 1 : end] if start != -1 and end != -1 else text
    try:
        return json.loads(body)
    except ValueError:
        return None


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(raw, fmt).astimezone(UTC)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _utc(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "unknown time"


def _group_core(group: str, prefixes: list[str]) -> str:
    g = group.strip().lower().split("@", 1)[0]
    for prefix in sorted(prefixes, key=len, reverse=True):
        if g.startswith(prefix.lower()):
            return g[len(prefix) :]
    return g


def _names_group(text: str, group: str, prefixes: list[str], noise: list[str]) -> bool:
    """True when `text` names the group: the full name, or every word of its
    distinctive part (with prefix `eng_ops_`, eng_ops_global-cdn -> "global"
    and "cdn"), across the - / _ spellings tickets use. `noise` words (a
    region suffix, say) are not required."""
    flat = re.sub(r"[-_]", " ", text.lower())
    if group.lower() in text.lower():
        return True
    skip = {w.lower() for w in noise}
    words = [w for w in re.split(r"[-_]", _group_core(group, prefixes)) if w and w not in skip]
    return bool(words) and all(re.search(rf"\b{re.escape(w)}\b", flat) for w in words)


def _first_fields(node):
    """The `fields` of the first issue anywhere in a gateway answer, whatever
    envelope it came in ({"data": ...}, {"issues": {"nodes": [...]}}, ...)."""
    if isinstance(node, dict):
        if isinstance(node.get("fields"), dict):
            return node["fields"]
        for value in node.values():
            found = _first_fields(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _first_fields(value)
            if found:
                return found
    return {}


def _row(kind: str, r: dict) -> str:
    what = r.get("groupName") if kind else r.get("eventType")
    actor = r.get("actor") or "?"
    lead = f"{kind}  " if kind else ""
    return f"- {lead}{_utc(_when(r.get('eventTime')))}  {what}  actor {actor}"


def _person(value) -> str:
    if isinstance(value, dict):
        name = value.get("displayName") or ""
        email = value.get("emailAddress") or ""
        return f"{name} ({email})" if email else name or json.dumps(value)[:80]
    if isinstance(value, list):
        return ", ".join(_person(v) for v in value)
    return str(value)


class AuditTraceProvider:
    def __init__(
        self,
        multi,
        cloudtrail,
        slack,
        *,
        tracker_site: str,
        tracker_project: str,
        tracker_tools: str = "atlassian",
        group_prefixes: list[str] | None = None,
        group_noise_words: list[str] | None = None,
        thread_marker: str | None = None,
        approval_phrase: str = "Approved by",
        approver_field: str = "Approver",
    ):
        self.multi = multi
        self.cloudtrail = cloudtrail
        self.slack = slack
        self.cloud_id = tracker_site
        self.project = tracker_project
        self.tracker_tools = tracker_tools
        self.group_prefixes = list(group_prefixes or [])
        self.group_noise_words = list(group_noise_words or [])
        self._thread_ts = (
            re.compile(rf"{re.escape(thread_marker)}[^0-9]{{0,40}}(\d{{10}}\.\d{{6}})")
            if thread_marker
            else None
        )
        self._approved = re.compile(rf"{re.escape(approval_phrase)} [^\n\"]{{3,160}}")
        self.approver_field = approver_field

    async def tools(self) -> list[dict]:
        return [
            {
                "name": "audit.trace",
                "description": (
                    "Run the access-provenance evidence chain for one person in a fixed order "
                    "and return one evidence pack: (1) every directory-group add/remove and "
                    "account lifecycle event (create, activate, deactivate, suspend) from the "
                    "identity provider's log; (2) for each group add, the access-request "
                    "tickets that name the person's email, created in a window around the add, "
                    "each checked for naming the group, its created time against the add, its "
                    "approver and approval comment; (3) the Slack request thread the ticket "
                    "names (else an anchored channel search), with its ticket keys and "
                    "reactions. Call this FIRST for any question about when someone got access, "
                    "who approved it, or when they were onboarded; then read further only where "
                    "the pack shows a gap. Pass `group` when the question names one."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "description": "The person's email, exactly"},
                        "group": {
                            "type": "string",
                            "description": "The directory group asked about (optional)",
                        },
                    },
                    "required": ["email"],
                },
            }
        ]

    async def call(self, name: str, payload: dict) -> str:
        if name != "audit.trace":
            raise LookupError(f"unknown tool {name!r}")
        email = (payload.get("email") or "").strip().lower()
        group = (payload.get("group") or "").strip().lower().split("@", 1)[0]
        if "@" not in email:
            return "I need the person's full email address."
        try:
            return await self._trace(email, group)
        except Exception as exc:  # noqa: BLE001  never raise into the loop
            return f"audit.trace stopped partway: {exc}"

    async def _atlassian(self, tool: str, args: dict):
        try:
            text = await self.multi.call(
                f"{self.tracker_tools}.{tool}", {"cloudId": self.cloud_id, **args}
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"{tool} failed: {exc}"
        data = _untrusted_json(text)
        if data is None:
            return None, f"{tool} answered something I could not read: {text[:160]}"
        return data, None

    async def _trace(self, email: str, group: str) -> str:
        out: list[str] = [
            f"audit.trace for {email}"
            + (f", group {group}" if group else "")
            + ". Every line below comes from a live call this tool made just now. Cite from it; "
            "do not add facts it did not return.",
        ]
        rows, lifecycle_warning, truncated = await asyncio.to_thread(
            self.cloudtrail._query_sync, email, _LOOKBACK_DAYS
        )
        adds = [r for r in rows if r.get("eventType") == "group.user_membership.add"]
        removes = [r for r in rows if r.get("eventType") == "group.user_membership.remove"]
        lifecycle = [r for r in rows if str(r.get("eventType", "")).startswith("user.lifecycle.")]

        out.append("\n## 1. Identity provider log (5-year lookback)")
        for r in sorted(lifecycle, key=lambda r: str(r.get("eventTime"))):
            out.append(_row("", r))
        if not lifecycle:
            out.append(
                "- no account lifecycle events (no create/activate/deactivate/suspend) "
                "in the window" + (f"; warning: {lifecycle_warning}" if lifecycle_warning else "")
            )
        out.append(f"- {len(adds)} group adds, {len(removes)} group removes")
        for lane in truncated:
            out.append(f"- note: the {lane} lane hit its row cap; older events may be missing")

        # One event per (group, minute): the log often records an add twice.
        targets: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for r in sorted(adds, key=lambda r: str(r.get("eventTime"))):
            g = str(r.get("groupName") or "").lower()
            if group and g != group:
                continue
            key = (g, str(r.get("eventTime"))[:16])
            if key in seen:
                continue
            seen.add(key)
            targets.append(r)
        for r in sorted(adds, key=lambda r: str(r.get("eventTime"))):
            if not group:
                out.append(_row("add", r))
        if group:
            later_removes = [r for r in removes if str(r.get("groupName") or "").lower() == group]
            for r in later_removes:
                out.append(_row("remove", r))
            if not targets:
                held = sorted({str(r.get("groupName")) for r in adds})
                out.append(
                    f"- NO add event for {group} in 5 years. Groups this person was added to: "
                    f"{', '.join(held) or 'none'}."
                )
                out.append(
                    "  If they are in the group today, the membership came another way (nested "
                    "group, added on the "
                    "Google side, or before the log's reach). The log cannot show current "
                    "membership."
                )
                return "\n".join(out)
        if len(targets) > _MAX_EVENTS:
            out.append(
                f"- tracing the {_MAX_EVENTS} most recent adds; pass `group` to trace an older one"
            )
            targets = targets[-_MAX_EVENTS:]

        for r in targets:
            out.extend(await self._trace_add(email, r))
        return "\n".join(out)

    async def _trace_add(self, email: str, r: dict) -> list[str]:
        g = str(r.get("groupName") or "")
        at = _when(r.get("eventTime"))
        lines = [f"\n## 2. {g}: added {_utc(at)} by {r.get('actor') or '?'}"]
        if at is None:
            return lines + ["- the event time could not be read; skipping"]

        issues, window, err = [], 0, None
        for days in (7, 30):
            d0 = (at - timedelta(days=days)).strftime("%Y-%m-%d")
            d1 = (at + timedelta(days=days)).strftime("%Y-%m-%d")
            jql = (
                f'project = {self.project} AND text ~ "{email}" AND created >= "{d0}" '
                f'AND created <= "{d1}" ORDER BY created ASC'
            )
            data, err = await self._atlassian(
                "searchJiraIssuesUsingJql",
                {
                    "jql": jql,
                    "maxResults": 30,
                    "fields": ["summary", "created", "status", "parent"],
                },
            )
            window = days
            if err:
                break
            issues = ((data or {}).get("data") or data or {}).get("issues") or []
            if issues:
                break
        if err:
            lines.append(f"- Jira search failed: {err}")
        else:
            lines.append(
                f"- Jira: {len(issues)} {self.project} ticket(s) naming {email}, "
                f"created within +/-{window} days of the add"
            )

        # Read the likeliest first: created before the add, nearest to it.
        def _rank(issue):
            created = _when((issue.get("fields") or {}).get("created"))
            if created is None:
                return (2, 0)
            gap = (at - created).total_seconds()
            return (0, gap) if gap >= -300 else (1, -gap)

        ranked = sorted(issues, key=_rank)
        if len(ranked) > _MAX_TICKETS * 2:
            lines.append(
                f"  - reading the {_MAX_TICKETS * 2} created nearest before the add; "
                f"{len(ranked) - _MAX_TICKETS * 2} others not read"
            )
        read: list[dict] = []
        for issue in ranked[: _MAX_TICKETS * 2]:
            key = issue.get("key")
            detail, derr = await self._atlassian(
                "getJiraIssue",
                {"issueIdOrKey": key, "view": "evidence", "responseContentFormat": "markdown"},
            )
            if derr:
                lines.append(f"  - {key}: could not read it ({derr})")
                continue
            fields = _first_fields(detail)
            created = _when(fields.get("created"))
            summary = fields.get("summary") or ""
            cdata, cerr = await self._atlassian("listJiraIssueComments", {"issueIdOrKey": key})
            comments = json.dumps(cdata) if cdata else ""
            in_ticket = _names_group(
                summary + " " + str(fields.get("description") or ""),
                g,
                self.group_prefixes,
                self.group_noise_words,
            )
            in_comment = not in_ticket and _names_group(
                comments, g, self.group_prefixes, self.group_noise_words
            )
            before = created is not None and created <= at + timedelta(minutes=5)
            delta = (at - created).total_seconds() / 60 if created else None
            approver = ""
            field = (fields.get("customFields") or {}).get(self.approver_field)
            if field:
                value = (
                    field.get("value") if isinstance(field, dict) and "value" in field else field
                )
                if value:
                    approver = f"Approver field: {_person(value)}"
            thread = self._thread_ts.search(json.dumps(fields)) if self._thread_ts else None
            parent = (
                (fields.get("parent") or {}).get("key")
                if isinstance(fields.get("parent"), dict)
                else None
            )
            approvals, approved_at = [], None
            for c in ((cdata or {}).get("data") or cdata or {}).get("comments") or []:
                body = str(c.get("body") or "")
                m = self._approved.search(body)
                if m:
                    approvals.append(m.group(0))
                    approved_at = approved_at or _when(c.get("created"))
            status = (
                (fields.get("status") or {}).get("name")
                if isinstance(fields.get("status"), dict)
                else fields.get("status")
            )
            when_note = (
                f"{abs(delta):.0f} min {'before' if delta >= 0 else 'AFTER'} the add"
                if delta is not None
                else "created time unknown"
            )
            names_note = "yes" if in_ticket else ("only in a comment" if in_comment else "NO")
            lines.append(
                f'  - {key} "{summary}" created {_utc(created)} ({when_note}), status {status}'
                + (f", sub-task of {parent}" if parent else "")
                + f"; names the group: {names_note}"
                + (f"; {approver}" if approver else "")
                + (
                    f'; approval comment at {_utc(approved_at)}: "{approvals[0]}"'
                    if approvals
                    else "; no approval comment"
                )
                + (f"; comments unreadable ({cerr})" if cerr else "")
            )
            approval_gap = (at - approved_at).total_seconds() if approved_at else None
            read.append(
                {
                    "key": key,
                    "delta": delta,
                    "before": before,
                    "names": in_ticket or in_comment,
                    "approval_gap": approval_gap,
                    "thread": thread.group(1) if thread else None,
                    "parent": parent,
                }
            )
        matching = [x for x in read if x["names"] and x["before"]]
        # The add follows its approval within minutes (the bot applies it), so
        # an approval recorded shortly before the add beats a ticket that was
        # merely created nearer to it (seen 2026-11-04: the approved request
        # was approved 19 s before the add, and a duplicate request for the
        # same group had been created closer to it).
        approved_before = [
            x
            for x in matching
            if x["approval_gap"] is not None and -300 <= x["approval_gap"] <= 86400
        ]
        if approved_before:
            best = min(approved_before, key=lambda x: x["approval_gap"])
        else:
            best = min(matching, key=lambda x: x["delta"] or 0) if matching else None
        if read and best is None:
            lines.append(
                "  - none of these both names the group and was created before the add: "
                "no ticket matches this add"
            )
        elif best:
            gap = best.get("approval_gap")
            lines.append(
                f"  - best match: {best['key']} (names the person and the group, "
                "created before the add"
                + (
                    (
                        f"; its approval was recorded {gap:.0f} s before the add"
                        if gap >= 0
                        else f"; its approval comment was posted {-gap:.0f} s AFTER the add"
                    )
                    if gap is not None and gap < 86400
                    else ""
                )
                + "). Other matching tickets above are context, not the authorization for this add."
            )

        # The thread: the one the ticket (or its parent) names; else an
        # anchored search, keeping only a thread that carries the ticket's key.
        channel = self.slack.channels[0] if len(getattr(self.slack, "channels", [])) == 1 else ""
        ts, how = None, ""
        if best and best["thread"]:
            ts, how = best["thread"], f"named in {best['key']}'s description"
        elif best and best["parent"]:
            pdata, _ = await self._atlassian(
                "getJiraIssue",
                {"issueIdOrKey": best["parent"], "responseContentFormat": "markdown"},
            )
            found = (
                self._thread_ts.search(json.dumps(_first_fields(pdata)))
                if pdata and self._thread_ts
                else None
            )
            if found:
                ts, how = (
                    found.group(1),
                    f"named in the parent ticket {best['parent']}'s description",
                )
        if not ts and channel:
            wanted = (
                [best["key"], best["parent"]]
                if best
                else [
                    x["key"]
                    for x in sorted((x for x in read if x["before"]), key=lambda x: x["delta"] or 0)
                ]
            )
            wanted = [k for k in wanted if k]
            found = await self.slack._find_threads(
                email, channel, None, 5, around=at.strftime("%Y-%m-%d"), window_days=7
            )
            sections = found.split("\n\n=====\n\n")
            for key in wanted:
                hit = next((s for s in sections if re.search(rf"\b{re.escape(key)}\b", s)), None)
                if hit and _FIND_TS.search(hit):
                    ts, how = (
                        _FIND_TS.search(hit).group(1),
                        f"found by an anchored search; it carries {key}",
                    )
                    break
            if not ts:
                others = [
                    f"{_FIND_TS.search(s).group(1)} "
                    f"({', '.join(dict.fromkeys(_KEY.findall(s))) or 'no ticket keys'})"
                    for s in sections
                    if _FIND_TS.search(s)
                ]
                if others:
                    lines.append(
                        "- Slack: no thread within +/-7 days carries "
                        + (", ".join(wanted) if wanted else "a matching ticket")
                        + f". Threads naming the person in that window: {'; '.join(others)}"
                    )
                else:
                    lines.append(f"- Slack: {found.splitlines()[0] if found else 'no answer'}")
        if ts and channel:
            try:
                body, n = await self.slack._thread_text(channel, ts)
                link = await self.slack._permalink(channel, ts)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"- Slack thread {ts}: could not read it ({exc})")
            else:
                keys = list(dict.fromkeys(_KEY.findall(body)))
                reactions = [
                    ln.strip() for ln in body.splitlines() if ln.strip().startswith("reactions:")
                ]
                lines.append(f"- Slack thread {ts} ({how}), {n} messages: {link or 'no permalink'}")
                lines.append(f"  - ticket keys in thread order: {', '.join(keys) or 'none'}")
                if reactions:
                    lines.append(
                        "  - reactions on the opening message: "
                        + reactions[0].removeprefix("reactions: ")
                    )
                    for extra in reactions[1:3]:
                        lines.append(
                            f"  - reactions on a reply: {extra.removeprefix('reactions: ')}"
                        )
                lines.append(
                    f"  - read the whole thread with slack.read_thread {ts} before quoting it"
                )
        return lines
