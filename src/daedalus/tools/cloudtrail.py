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

The one tool queries an Okta system-log event data store for group
membership add/remove events for a single email: the provisioning-history
lane of an access audit ("user X was added to group Y on date Z by actor
W"). The query is lifted from a production audit service; its shapes
(event types, target extraction) follow Okta's system-log schema.
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
    ):
        self.role_arn = role_arn
        self.event_data_store = event_data_store
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
                    "dated group membership add/remove events from the "
                    "org's identity-provider system log, newest first. "
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
            rows = await asyncio.to_thread(self._query_sync, email, lookback)
        except _LaneError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001  degrade politely, never raise into the loop
            return f"The provisioning-history query failed partway: {exc}"
        if not rows:
            return (
                f"No provisioning events for {email} in the last "
                f"{lookback} days (searched groups matching "
                f"'{self.group_prefix}*')."
            )
        lines = [
            f"{len(rows)} provisioning events for {email} "
            f"(last {lookback} days, newest first):"
        ]
        for row in rows:
            actor = row.get("actor") or "?"
            lines.append(
                f"  {row.get('eventTime', '?')}  {row.get('eventType', '?')}  "
                f"{row.get('groupName', '?')}  (actor: {actor})"
            )
        return "\n".join(lines)

    # -- the synchronous lane (runs in a thread) ---------------------------

    def _query_sync(self, email: str, lookback_days: int) -> list[dict]:
        client = self._lake_client()
        query_id = client.start_query(
            QueryStatement=self._build_query(email, lookback_days)
        )["QueryId"]
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
            RoleSessionName="daedalus-cloudtrail",
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
FROM {self.event_data_store}
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
