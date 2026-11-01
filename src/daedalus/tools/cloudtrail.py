"""CloudTrail Lake sidecar: directory-group provisioning history via
workload-identity federation.

The cloud-access integration path (2026-09-03 ruling): the pod's own GCP
service account IS the identity. AWS trusts it directly through a scoped
role (sts:AssumeRoleWithWebIdentity), so no credential is stored, minted
by a broker, or injected by a gateway -- scoping lives in AWS IAM, where
the role is expected to allow ONLY the Lake query actions on ONE event
data store. Revoking the role's trust is the kill switch.

Auth chain (in-cluster by design): the GCP metadata server mints an
identity token for audience sts.amazonaws.com -- the metadata server adds
the `oaud` claim AWS's Google federation checks, which locally-minted
gcloud tokens do NOT carry -- and boto3 exchanges it for role
credentials, cached until shortly before expiry.

The one tool queries an Okta system-log event data store for a single
email's provisioning history -- TWO statements per call. The membership
statement returns group add/remove events ("user X was added to group Y
on date Z by actor W"); the lifecycle statement returns account events
(deactivate, delete, suspend, unsuspend, activate) for the same person.
The second statement exists because of a real audit miss: deactivating a
user removes them from EVERY group without emitting per-group remove
events, so a membership-only history reads as "still in the group" for a
person who was offboarded months ago. The membership query is lifted from
a production audit service; its shapes (event types, target extraction)
follow Okta's system-log schema.
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
import urllib.request

_METADATA_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity?audience={aud}"
)
_AUDIENCE = "sts.amazonaws.com"
_EMAIL_SHAPE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_DEFAULT_LOOKBACK_DAYS = 1825  # ~5 years: an offboarded person's ADD may be years old
_MAX_LOOKBACK_DAYS = 3650
_QUERY_DEADLINE_S = 60.0
_POLL_INTERVAL_S = 1.5
_ROW_LIMIT = 200
_CRED_EXPIRY_BUFFER_S = 120.0

# Account-lifecycle event types. Deactivation (and suspension) strips every
# group membership WITHOUT per-group group.user_membership.remove events --
# the blind spot that once turned "no removal event" into a wrong "still in
# the group" audit answer. Activate/unsuspend are included so a restore
# after suspension is visible in the same timeline.
_LIFECYCLE_EVENTS = (
    "user.lifecycle.activate",
    "user.lifecycle.deactivate",
    "user.lifecycle.delete.initiated",
    "user.lifecycle.suspend",
    "user.lifecycle.unsuspend",
)

# The subset whose side effect is membership-wide REMOVAL. Suspension is
# deliberately NOT here: in Okta it freezes sign-in but RETAINS group
# memberships (unsuspend restores the user intact), so calling it a
# removal would be the incident's mistake with a different event name.
_ACCOUNT_WIDE_EVENTS = frozenset(
    ("user.lifecycle.deactivate", "user.lifecycle.delete.initiated")
)

# Events that restore an account. What matters for a current-state answer
# is the LATEST lifecycle event, never the mere presence of an earlier
# deactivation -- asserting offboarding past a later restore would be the
# original wrong answer with the sign flipped.
_RESTORE_EVENTS = frozenset(("user.lifecycle.activate", "user.lifecycle.unsuspend"))


_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_EDS_ID = re.compile(rf"^{_UUID}$")
_EDS_ARN = re.compile(
    rf"^arn:aws[a-z-]*:cloudtrail:[a-z0-9-]+:\d{{12}}:eventdatastore/(?P<id>{_UUID})$"
)
_SESSION_NAME_BAD = re.compile(r"[^\w+=,.@-]")
_SESSION_NAME_MAX = 64  # AWS RoleSessionName limit


def _eds_id(event_data_store: str) -> str:
    """The event data store ID, from either an ID or an ARN, or ValueError.

    CloudTrail Lake takes no bind parameters for `FROM`, so the value is
    interpolated into the SQL and must be proven to be an identifier first
    (#88). The query takes the ID, which is the last segment of the ARN."""
    if _EDS_ID.match(event_data_store):
        return event_data_store
    match = _EDS_ARN.match(event_data_store)
    if match:
        return match.group("id")
    raise ValueError(
        f"cloudtrail event_data_store {event_data_store!r} is neither an event data "
        "store ID (a UUID) nor an eventdatastore ARN"
    )


def _session_name(agent_name: str) -> str:
    """`daedalus-<agent>`, sanitised to AWS's RoleSessionName alphabet and
    length, so AWS-side audit can tell agents apart (#88)."""
    name = _SESSION_NAME_BAD.sub("-", f"daedalus-{agent_name}" if agent_name else "daedalus")
    return name[:_SESSION_NAME_MAX]


def _metadata_token() -> str | None:
    """An identity token from the GCP metadata server, or None when not
    running on GCP compute (the metadata host does not resolve)."""
    encoded = urllib.parse.quote(_AUDIENCE, safe="")
    request = urllib.request.Request(
        _METADATA_URL.format(aud=encoded),
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.read().decode().strip()
    except Exception:  # noqa: BLE001  absent metadata = not on GCP, a state not an error
        return None


class CloudTrailToolProvider:
    def __init__(
        self,
        role_arn: str,
        event_data_store: str,
        region: str,
        group_prefix: str = "engineering_",
        token_fetch=None,
        boto3_module=None,
        agent_name: str = "",
    ):
        self.role_arn = role_arn
        self.event_data_store = event_data_store
        # Raises ValueError on anything that is not an ID or ARN, before it can
        # reach a query string. `_query_eds` is what FROM interpolates.
        self._query_eds = _eds_id(event_data_store)
        self.session_name = _session_name(agent_name)
        self.region = region
        self.group_prefix = group_prefix
        self._token_fetch = token_fetch or _metadata_token
        self._boto3 = boto3_module  # injectable: tests pass a fake module
        self._creds: dict | None = None
        self._creds_expiry: float = 0.0

    async def tools(self) -> list[dict]:
        return [
            {
                "name": "cloudtrail.query_provisioning",
                "description": (
                    "Directory-group provisioning history for one person: "
                    "dated group membership add/remove events PLUS account "
                    "lifecycle events (deactivation, deletion, suspension) "
                    "from the org's identity-provider system log, newest "
                    "first. Deactivation removes ALL group memberships "
                    "without per-group remove events, so the absence of a "
                    "remove event is never proof of current membership. "
                    "Read-only. Default lookback is 5 years -- an "
                    "offboarded person's audit-relevant ADD may be years "
                    "older than the audit window."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "description": "The person's email, exactly",
                        },
                        "lookback_days": {
                            "type": "integer",
                            "description": (
                                f"How far back to search (default "
                                f"{_DEFAULT_LOOKBACK_DAYS}, cap "
                                f"{_MAX_LOOKBACK_DAYS})"
                            ),
                        },
                    },
                    "required": ["email"],
                },
            }
        ]

    async def call(self, name: str, payload: dict) -> str:
        if name != "cloudtrail.query_provisioning":
            return f"I don't provide a tool named {name}."
        email = (payload.get("email") or "").strip()
        if not _EMAIL_SHAPE.match(email):
            return (
                "I need a plain email address to search provisioning "
                "history -- that value does not look like one."
            )
        lookback = payload.get("lookback_days") or _DEFAULT_LOOKBACK_DAYS
        try:
            lookback = max(1, min(int(lookback), _MAX_LOOKBACK_DAYS))
        except (TypeError, ValueError):
            lookback = _DEFAULT_LOOKBACK_DAYS
        try:
            rows, lifecycle_warning, truncated = await asyncio.to_thread(
                self._query_sync, email, lookback
            )
        except _LaneError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001  degrade politely, never raise into the loop
            return f"The provisioning-history query failed partway: {exc}"
        if not rows:
            text = (
                f"No provisioning events for {email} in the last "
                f"{lookback} days (searched groups matching "
                f"'{self.group_prefix}*' and account lifecycle events)."
            )
            if lifecycle_warning:
                text += f"\n  warning: {lifecycle_warning}"
            return text
        lines = [
            f"{len(rows)} provisioning events for {email} "
            f"(last {lookback} days, newest first):"
        ]
        latest_lifecycle = None  # rows are newest-first; first hit wins
        any_off_event = False
        for row in rows:
            actor = row.get("actor") or "?"
            when = row.get("eventTime") or "?"
            event_type = str(row.get("eventType", "?"))
            if event_type.startswith("user.lifecycle."):
                if latest_lifecycle is None:
                    latest_lifecycle = (when, event_type)
                if (
                    event_type in _ACCOUNT_WIDE_EVENTS
                    or event_type == "user.lifecycle.suspend"
                ):
                    any_off_event = True
                lines.append(
                    f"  {when}  {event_type}  [account lifecycle]  (actor: {actor})"
                )
            else:
                lines.append(
                    f"  {when}  {event_type}  "
                    f"{row.get('groupName', '?')}  (actor: {actor})"
                )
        # The note keys on the LATEST lifecycle event: an earlier
        # deactivation followed by a restore is not offboarding.
        if latest_lifecycle is not None:
            when, event_type = latest_lifecycle
            if event_type in _ACCOUNT_WIDE_EVENTS:
                lines.append(
                    "  note: deactivation or deletion removes ALL group "
                    "memberships without per-group remove events -- treat "
                    f"{when} as the removal date for every group then held."
                )
            elif event_type == "user.lifecycle.suspend":
                lines.append(
                    "  note: suspension freezes sign-in but RETAINS group "
                    "memberships -- it is not a removal; verify current "
                    "state before any access claim."
                )
            elif event_type in _RESTORE_EVENTS and any_off_event:
                lines.append(
                    f"  note: the account was RESTORED on {when} after an "
                    "earlier deactivation/suspension -- the earlier event "
                    "is not a current-state answer; verify live membership "
                    "before any access claim."
                )
        elif lifecycle_warning is None:
            lines.append(
                "  lifecycle lane: no account events in this window "
                "(deactivation/deletion/suspension) -- offboarding "
                "coverage is complete for the window."
            )
        for lane in truncated:
            lines.append(
                f"  note: the {lane} lane hit its {_ROW_LIMIT}-row cap -- "
                "older events may be missing from this list."
            )
        if lifecycle_warning:
            lines.append(f"  warning: {lifecycle_warning}")
        return "\n".join(lines)

    # -- the synchronous lane (runs in a thread) ---------------------------

    def _query_sync(
        self, email: str, lookback_days: int
    ) -> tuple[list[dict], str | None, list[str]]:
        """Both lanes, one merged timeline. The membership lane is the
        original contract and its failure fails the call; the lifecycle
        lane is additive, so ITS failure degrades to a warning string --
        an audit must never lose the membership answer because the newer
        lane hiccuped, but it must also never silently lose offboarding
        coverage (the warning is the honesty). The third return names
        lanes that hit the row cap, so truncation is never silent."""
        client = self._lake_client()
        rows = self._run_query(client, self._build_query(email, lookback_days))
        truncated = ["membership"] if len(rows) >= _ROW_LIMIT else []
        lifecycle_warning = None
        try:
            lifecycle_rows = self._run_query(
                client, self._build_lifecycle_query(email, lookback_days)
            )
        except Exception:  # noqa: BLE001  additive lane: degrade to a warning
            lifecycle_rows = []
            lifecycle_warning = (
                "the account-lifecycle lane failed, so offboarding "
                "coverage is incomplete this run -- a deactivation would "
                "not appear above."
            )
        if len(lifecycle_rows) >= _ROW_LIMIT:
            truncated.append("lifecycle")
        merged = sorted(
            rows + lifecycle_rows,
            key=lambda row: str(row.get("eventTime") or ""),
            reverse=True,
        )
        return merged, lifecycle_warning, truncated

    def _run_query(self, client, statement: str) -> list[dict]:
        query_id = client.start_query(QueryStatement=statement)["QueryId"]
        deadline = time.monotonic() + _QUERY_DEADLINE_S
        while True:
            status = client.describe_query(
                EventDataStore=self.event_data_store, QueryId=query_id
            )["QueryStatus"]
            if status == "FINISHED":
                break
            if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                raise _LaneError(
                    f"The lake ended query {query_id} with status {status}."
                )
            if time.monotonic() > deadline:
                raise _LaneError(
                    f"The lake did not finish query {query_id} within "
                    f"{int(_QUERY_DEADLINE_S)}s."
                )
            time.sleep(_POLL_INTERVAL_S)

        rows: list = []
        next_token = None
        while True:
            kwargs = {"EventDataStore": self.event_data_store, "QueryId": query_id}
            if next_token:
                kwargs["NextToken"] = next_token
            response = client.get_query_results(**kwargs)
            rows.extend(response.get("QueryResultRows", []))
            next_token = response.get("NextToken")
            if not next_token:
                break
        return _flatten_rows(rows)

    def _lake_client(self):
        # THE SECOND WALL (kobo#310): on a colleague's task the role is never
        # assumed and a cached session is never reused, whatever the tool
        # filter let through. Checked here, where the credential is used, so
        # the refusal leaves before anything leaves the pod. The contextvar is
        # copied into the worker thread by asyncio.to_thread.
        from daedalus.tools.colleagues import consult_refusal

        refusal = consult_refusal("cloudtrail.query_provisioning")
        if refusal is not None:
            raise _LaneError(refusal)
        boto3 = self._boto3
        if boto3 is None:
            try:
                import boto3  # noqa: PLC0415  optional dependency, aws extra
            except ImportError:
                raise _LaneError(
                    "The aws extra is not installed on this runtime, so I "
                    "cannot reach the provisioning lake."
                ) from None
        creds = self._assume(boto3)
        return boto3.client(
            "cloudtrail",
            region_name=self.region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )

    def _assume(self, boto3) -> dict:
        if self._creds is not None and time.monotonic() < self._creds_expiry:
            return self._creds
        token = self._token_fetch()
        if not token:
            raise _LaneError(
                "I can only reach the provisioning lake from my own pod -- "
                "no workload identity is available here."
            )
        sts = boto3.client("sts", region_name=self.region)
        response = sts.assume_role_with_web_identity(
            RoleArn=self.role_arn,
            RoleSessionName=self.session_name,
            WebIdentityToken=token,
        )
        self._creds = response["Credentials"]
        # Sessions run 3600s; refresh well before the edge. The Expiration
        # field is a datetime whose clock may not match ours -- a fixed
        # local buffer window is simpler and always safe.
        self._creds_expiry = time.monotonic() + 3600.0 - _CRED_EXPIRY_BUFFER_S
        return self._creds

    def _build_query(self, email: str, lookback_days: int) -> str:
        """The provisioning-history query, against Okta system-log events
        replicated into the lake. The lake does not support parameterized
        queries; the email is validated to a strict shape before it gets
        here, and single quotes are doubled anyway."""
        cutoff = time.strftime(
            "%Y-%m-%d 00:00:00.000",
            time.gmtime(time.time() - lookback_days * 86400),
        )
        safe_email = email.replace("'", "''")
        safe_prefix = self.group_prefix.replace("'", "''")
        return f"""
SELECT
    eventData.eventTime AS eventTime,
    json_extract_scalar(target_user.value, '$.alternateId') AS user,
    json_extract_scalar(target_group.value, '$.displayName') AS groupName,
    json_extract_scalar(
        json_parse(element_at(eventData.additionaleventdata, 'actor')),
        '$.alternateId'
    ) AS actor,
    element_at(eventData.additionaleventdata, 'eventType') AS eventType
FROM {self._query_eds}
CROSS JOIN UNNEST(
    CAST(json_parse(element_at(eventData.additionaleventdata, 'target')) AS array(json))
) AS target_user(value)
CROSS JOIN UNNEST(
    CAST(json_parse(element_at(eventData.additionaleventdata, 'target')) AS array(json))
) AS target_group(value)
WHERE
    element_at(eventData.additionaleventdata, 'eventType') IN (
        'group.user_membership.add',
        'group.user_membership.remove',
        'application.provision.group_push.push_memberships'
    )
    AND json_extract_scalar(target_user.value, '$.alternateId') = '{safe_email}'
    AND json_extract_scalar(target_group.value, '$.type') = 'UserGroup'
    AND json_extract_scalar(target_group.value, '$.displayName') LIKE '{safe_prefix}%'
    AND eventData.eventTime > '{cutoff}'
ORDER BY eventData.eventTime DESC
LIMIT {_ROW_LIMIT}
""".strip()

    def _build_lifecycle_query(self, email: str, lookback_days: int) -> str:
        """Account-lifecycle events for the principal. Deliberately NO
        group join, NO UserGroup filter, NO group-prefix LIKE: lifecycle
        events target only the user, so the membership query's shape
        excludes them structurally -- which is exactly how the blind spot
        happened."""
        cutoff = time.strftime(
            "%Y-%m-%d 00:00:00.000",
            time.gmtime(time.time() - lookback_days * 86400),
        )
        safe_email = email.replace("'", "''")
        event_list = ",\n        ".join(f"'{event}'" for event in _LIFECYCLE_EVENTS)
        return f"""
SELECT
    eventData.eventTime AS eventTime,
    json_extract_scalar(target_user.value, '$.alternateId') AS user,
    json_extract_scalar(
        json_parse(element_at(eventData.additionaleventdata, 'actor')),
        '$.alternateId'
    ) AS actor,
    element_at(eventData.additionaleventdata, 'eventType') AS eventType
FROM {self._query_eds}
CROSS JOIN UNNEST(
    CAST(json_parse(element_at(eventData.additionaleventdata, 'target')) AS array(json))
) AS target_user(value)
WHERE
    element_at(eventData.additionaleventdata, 'eventType') IN (
        {event_list}
    )
    AND json_extract_scalar(target_user.value, '$.alternateId') = '{safe_email}'
    AND eventData.eventTime > '{cutoff}'
ORDER BY eventData.eventTime DESC
LIMIT {_ROW_LIMIT}
""".strip()


class _LaneError(Exception):
    """A polite, user-facing dead end -- rendered verbatim as the tool
    result, never raised into the agent loop."""


def _flatten_rows(rows: list) -> list[dict]:
    """The lake returns rows as [{"col1": v}, {"col2": v}, ...]; flatten
    each row's cells into one dict."""
    out: list[dict] = []
    for row in rows:
        merged: dict = {}
        for cell in row:
            merged.update(cell)
        out.append(merged)
    return out
