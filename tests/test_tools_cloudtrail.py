"""CloudTrailToolProvider: the cloud-lane sidecar. Every test drives the
provider through a fake boto3 module and an injected token fetch -- no
network, no metadata server. The shapes pinned here are the integration
path's client-side conduct: identity comes from the pod (never a stored
secret), credentials cache until near expiry, the query interpolates only
a shape-validated email, and every dead end degrades to a polite string."""

import asyncio

from daedalus.bundle import AgentSpec
from daedalus.tools.cloudtrail import CloudTrailToolProvider, _flatten_rows

EDS = "abcd1234-eeee-ffff-0000-111122223333"
ROLE = "arn:aws:iam::000000000000:role/example-lake-reader"


def _rows(*cells):
    return [list(cell) for cell in cells]


class FakeLake:
    """Scripted cloudtrail client: one query lifecycle plus pagination."""

    def __init__(
        self,
        statuses=("FINISHED",),
        pages=None,
        fail_start=False,
        lifecycle_pages=None,
        fail_lifecycle=False,
    ):
        self.statuses = list(statuses)
        self.pages = pages if pages is not None else [{"QueryResultRows": []}]
        # None -> the lifecycle lane exists but has no events (the default
        # keeps every membership-only test meaningful without edits).
        self.lifecycle_pages = lifecycle_pages
        self.fail_start = fail_start
        self.fail_lifecycle = fail_lifecycle
        self.started = []
        self.describes = 0
        self._lane_by_qid = {}

    def start_query(self, QueryStatement):
        if self.fail_start:
            raise RuntimeError("lake said no")
        is_lifecycle = "user.lifecycle" in QueryStatement
        if self.fail_lifecycle and is_lifecycle:
            raise RuntimeError("lifecycle lane said no")
        self.started.append(QueryStatement)
        qid = "q-%d" % len(self.started)
        self._lane_by_qid[qid] = is_lifecycle
        return {"QueryId": qid}

    def describe_query(self, EventDataStore, QueryId):
        assert EventDataStore == EDS
        self.describes += 1
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"QueryStatus": status}

    def get_query_results(self, EventDataStore, QueryId, NextToken=None):
        if self._lane_by_qid.get(QueryId):
            source = self.lifecycle_pages or [{"QueryResultRows": []}]
        else:
            source = self.pages
        page = dict(source[0 if NextToken is None else 1])
        if NextToken is None and len(source) > 1:
            page["NextToken"] = "page-2"
        return page


class FakeSts:
    def __init__(self):
        self.calls = []

    def assume_role_with_web_identity(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "AKIAFAKE",
                "SecretAccessKey": "shh",
                "SessionToken": "sesh",
                "Expiration": "later",
            }
        }


class FakeBoto3:
    def __init__(self, lake):
        self.lake = lake
        self.sts = FakeSts()
        self.client_calls = []

    def client(self, service, **kwargs):
        self.client_calls.append((service, kwargs))
        return self.sts if service == "sts" else self.lake


def _provider(lake, token="tok-123", **overrides):
    fake = FakeBoto3(lake)
    provider = CloudTrailToolProvider(
        overrides.pop("role_arn", ROLE),
        overrides.pop("event_data_store", EDS),
        region=overrides.pop("region", "us-east-2"),
        group_prefix=overrides.pop("group_prefix", "engineering_"),
        token_fetch=lambda: token,
        boto3_module=fake,
    )
    return provider, fake


def test_tools_lists_the_one_query_tool():
    provider, _ = _provider(FakeLake())
    (spec,) = asyncio.run(provider.tools())
    assert spec["name"] == "cloudtrail.query_provisioning"
    assert spec["parameters"]["required"] == ["email"]


def test_rejects_a_non_email_before_touching_aws():
    provider, fake = _provider(FakeLake())
    reply = asyncio.run(
        provider.call("cloudtrail.query_provisioning", {"email": "1; DROP TABLE --"})
    )
    assert "does not look like" in reply
    assert fake.client_calls == []


def test_unknown_tool_name_is_a_polite_no():
    provider, _ = _provider(FakeLake())
    reply = asyncio.run(provider.call("cloudtrail.other", {}))
    assert "don't provide" in reply


def test_query_carries_email_prefix_store_and_limit():
    lake = FakeLake(pages=[{"QueryResultRows": []}])
    provider, _ = _provider(lake, group_prefix="platform_")
    asyncio.run(
        provider.call("cloudtrail.query_provisioning", {"email": "a.b@example.com"})
    )
    membership, lifecycle = lake.started
    assert "= 'a.b@example.com'" in membership
    assert "LIKE 'platform_%'" in membership
    assert f"FROM {EDS}" in membership
    assert "LIMIT 200" in membership
    assert "user.lifecycle" not in membership


def test_lifecycle_query_targets_the_principal_without_group_filters():
    # The blind spot this lane closes: deactivation removes every group
    # membership WITHOUT per-group remove events, and the membership
    # query's UserGroup join excludes lifecycle events structurally. The
    # second statement must carry the five lifecycle event types, the
    # email, the window -- and none of the group-shaped filters.
    lake = FakeLake()
    provider, _ = _provider(lake, group_prefix="platform_")
    asyncio.run(
        provider.call("cloudtrail.query_provisioning", {"email": "a.b@example.com"})
    )
    _membership, lifecycle = lake.started
    for event in (
        "user.lifecycle.activate",
        "user.lifecycle.deactivate",
        "user.lifecycle.delete.initiated",
        "user.lifecycle.suspend",
        "user.lifecycle.unsuspend",
    ):
        assert event in lifecycle
    assert "= 'a.b@example.com'" in lifecycle
    assert f"FROM {EDS}" in lifecycle
    assert "LIMIT 200" in lifecycle
    assert "UserGroup" not in lifecycle
    assert "LIKE 'platform_%'" not in lifecycle


def test_lifecycle_events_merge_into_the_timeline_newest_first():
    lake = FakeLake(
        pages=[
            {
                "QueryResultRows": _rows(
                    (
                        {"eventTime": "2026-04-08 18:52:01.000"},
                        {"user": "a@example.com"},
                        {"groupName": "engineering_team_x"},
                        {"actor": "admin@example.com"},
                        {"eventType": "group.user_membership.add"},
                    )
                )
            }
        ],
        lifecycle_pages=[
            {
                "QueryResultRows": _rows(
                    (
                        {"eventTime": "2026-07-21 17:39:10.000"},
                        {"user": "a@example.com"},
                        {"actor": "offboarder@example.com"},
                        {"eventType": "user.lifecycle.deactivate"},
                    )
                )
            }
        ],
    )
    provider, _ = _provider(lake)
    reply = asyncio.run(
        provider.call("cloudtrail.query_provisioning", {"email": "a@example.com"})
    )
    assert reply.startswith("2 provisioning events for a@example.com")
    assert "user.lifecycle.deactivate" in reply
    assert "group.user_membership.add" in reply
    # newest first across BOTH lanes: the July deactivation renders before
    # the April group add
    assert reply.index("user.lifecycle.deactivate") < reply.index(
        "group.user_membership.add"
    )
    # the semantic the audit thread missed, taught at the point of use
    assert "without per-group remove events" in reply


def test_lifecycle_lane_failure_keeps_membership_rows_and_warns():
    lake = FakeLake(
        pages=[
            {
                "QueryResultRows": _rows(
                    (
                        {"eventTime": "2026-04-08 18:52:01.000"},
                        {"user": "a@example.com"},
                        {"groupName": "engineering_team_x"},
                        {"actor": "admin@example.com"},
                        {"eventType": "group.user_membership.add"},
                    )
                )
            }
        ],
        fail_lifecycle=True,
    )
    provider, _ = _provider(lake)
    reply = asyncio.run(
        provider.call("cloudtrail.query_provisioning", {"email": "a@example.com"})
    )
    assert "group.user_membership.add" in reply
    assert "offboarding coverage is incomplete" in reply


def test_empty_result_mentions_the_lifecycle_lane():
    provider, _ = _provider(FakeLake())
    reply = asyncio.run(
        provider.call(
            "cloudtrail.query_provisioning",
            {"email": "a@example.com", "lookback_days": 30},
        )
    )
    assert "account lifecycle events" in reply


def test_description_teaches_the_deactivation_semantics():
    provider, _ = _provider(FakeLake())
    (spec,) = asyncio.run(provider.tools())
    assert "lifecycle" in spec["description"]
    assert "deactivation" in spec["description"]


def test_quotes_in_email_shape_never_reach_the_query_unescaped():
    # The shape gate rejects quotes outright -- belt; escaping is braces.
    provider, fake = _provider(FakeLake())
    reply = asyncio.run(
        provider.call("cloudtrail.query_provisioning", {"email": "a'or1=1@x.co"})
    )
    assert "does not look like" in reply
    assert fake.client_calls == []


def test_rows_flatten_and_render_newest_first_header():
    lake = FakeLake(
        pages=[
            {
                "QueryResultRows": _rows(
                    (
                        {"eventTime": "2026-05-14 09:00:00.000"},
                        {"user": "a@example.com"},
                        {"groupName": "engineering_team_x"},
                        {"actor": "admin@example.com"},
                        {"eventType": "group.user_membership.add"},
                    )
                )
            }
        ]
    )
    provider, _ = _provider(lake)
    reply = asyncio.run(
        provider.call("cloudtrail.query_provisioning", {"email": "a@example.com"})
    )
    assert reply.startswith("1 provisioning events for a@example.com")
    assert "group.user_membership.add" in reply
    assert "engineering_team_x" in reply
    assert "(actor: admin@example.com)" in reply


def test_pagination_concatenates_pages():
    lake = FakeLake(
        pages=[
            {"QueryResultRows": _rows(({"eventTime": "t1"}, {"eventType": "e"}))},
            {"QueryResultRows": _rows(({"eventTime": "t2"}, {"eventType": "e"}))},
        ]
    )
    provider, _ = _provider(lake)
    reply = asyncio.run(
        provider.call("cloudtrail.query_provisioning", {"email": "a@example.com"})
    )
    assert reply.startswith("2 provisioning events")


def test_empty_result_names_the_window_and_prefix():
    provider, _ = _provider(FakeLake())
    reply = asyncio.run(
        provider.call(
            "cloudtrail.query_provisioning",
            {"email": "a@example.com", "lookback_days": 30},
        )
    )
    assert "No provisioning events for a@example.com" in reply
    assert "30 days" in reply
    assert "'engineering_*'" in reply


def test_missing_identity_is_a_polite_dead_end():
    provider, fake = _provider(FakeLake(), token=None)
    reply = asyncio.run(
        provider.call("cloudtrail.query_provisioning", {"email": "a@example.com"})
    )
    assert "my own pod" in reply
    assert fake.sts.calls == []


def test_failed_query_status_reaches_the_caller():
    lake = FakeLake(statuses=("FAILED",))
    provider, _ = _provider(lake)
    reply = asyncio.run(
        provider.call("cloudtrail.query_provisioning", {"email": "a@example.com"})
    )
    assert "FAILED" in reply


def test_start_query_exception_degrades_politely():
    provider, _ = _provider(FakeLake(fail_start=True))
    reply = asyncio.run(
        provider.call("cloudtrail.query_provisioning", {"email": "a@example.com"})
    )
    assert reply.startswith("The provisioning-history query failed partway")


def test_credentials_cache_across_calls():
    lake = FakeLake()
    provider, fake = _provider(lake)
    asyncio.run(provider.call("cloudtrail.query_provisioning", {"email": "a@b.co"}))
    asyncio.run(provider.call("cloudtrail.query_provisioning", {"email": "c@d.co"}))
    assert len(fake.sts.calls) == 1
    (call,) = fake.sts.calls
    assert call["RoleArn"] == ROLE
    assert call["WebIdentityToken"] == "tok-123"


def test_lookback_is_clamped():
    lake = FakeLake()
    provider, _ = _provider(lake)
    reply = asyncio.run(
        provider.call(
            "cloudtrail.query_provisioning",
            {"email": "a@example.com", "lookback_days": 999999},
        )
    )
    assert "3650 days" in reply


def test_flatten_rows_merges_cells():
    assert _flatten_rows([[{"a": 1}, {"b": 2}], [{"a": 3}]]) == [
        {"a": 1, "b": 2},
        {"a": 3},
    ]


def test_bundle_spec_defaults_off_and_parses_on():
    base = {"name": "x", "model": {"id": "openai/gpt-test"}}
    spec = AgentSpec.model_validate(base)
    assert spec.tools.cloudtrail.role_arn is None

    spec = AgentSpec.model_validate(
        {
            **base,
            "tools": {
                "cloudtrail": {
                    "role_arn": ROLE,
                    "event_data_store": EDS,
                    "region": "us-east-2",
                }
            },
        }
    )
    assert spec.tools.cloudtrail.event_data_store == EDS
    assert spec.tools.cloudtrail.group_prefix == "engineering_"
