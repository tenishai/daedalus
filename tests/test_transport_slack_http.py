"""The Slack transport's HTTP way in (kobo#304): POST /slack/events on the a2a
app, gated by the per-agent relay secret, deduped on event_id in the transport
so Socket Mode and HTTP share one memory. Against the same fake API seam as
test_transport_slack.py; no slack-sdk needed."""

import asyncio
import sys
import types

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from daedalus.transports.a2a import A2aTransport, build_a2a_app  # noqa: E402
from daedalus.transports.slack import (  # noqa: E402
    _EVENT_ID_CAP,
    EVENTS_PATH,
    SlackTransport,
)
from tests.test_transport_slack import CH, EchoHandler, FakeApi, event  # noqa: E402

SECRET = "relay-secret-for-tests"
HEADER = "X-Kobo-Relay-Secret"


def envelope(event_id="Ev0001", **extra) -> dict:
    """The event_callback body exactly as Slack posts it to the relay."""
    return {
        "token": "verification-token",
        "team_id": "T0TEAM",
        "api_app_id": "A0APP",
        "type": "event_callback",
        "event_id": event_id,
        "event_time": 1_700_000_000,
        "event": event(**extra),
    }


def over_http(relay_secret=SECRET, api=None):
    """A transport in HTTP mode: bot token, no app-level token."""
    transport = SlackTransport(
        CH,
        bot_token="xoxb",
        app_token=None,
        api=FakeApi() if api is None else api,
        relay_secret=relay_secret,
    )
    transport.app_token = None  # in case the environment leaked one in
    return transport


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent")


async def settled(transport: SlackTransport) -> None:
    """Wait for every turn the route scheduled."""
    if transport._inflight:
        await asyncio.gather(*transport._inflight)


# ---- the gate ------------------------------------------------------------------


async def test_unset_secret_is_503(monkeypatch):
    monkeypatch.delenv("SLACK_RELAY_SECRET", raising=False)
    handler = EchoHandler()
    transport = over_http(relay_secret=None)
    assert transport.relay_secret is None
    async with client(build_a2a_app(handler, slack=transport)) as c:
        response = await c.post(EVENTS_PATH, json=envelope(), headers={HEADER: SECRET})
    assert response.status_code == 503
    assert handler.tasks == []
    assert transport._inflight == set()


async def test_wrong_or_missing_header_is_403():
    handler = EchoHandler()
    transport = over_http()
    async with client(build_a2a_app(handler, slack=transport)) as c:
        wrong = await c.post(EVENTS_PATH, json=envelope(), headers={HEADER: "nope"})
        missing = await c.post(EVENTS_PATH, json=envelope())
    assert wrong.status_code == 403
    assert missing.status_code == 403
    assert handler.tasks == []


async def test_right_secret_is_accepted_and_the_handler_runs_once():
    handler = EchoHandler()
    api = FakeApi()
    transport = over_http(api=api)
    async with client(build_a2a_app(handler, slack=transport)) as c:
        response = await c.post(
            EVENTS_PATH, json=envelope(text="hello over http", ts="10.1"), headers={HEADER: SECRET}
        )
        assert response.status_code == 200
        assert response.json() == {"accepted": True}
        await settled(transport)
    assert [t.user_text() for t in handler.tasks] == ["hello over http"]
    # Everything after handle_event is the Socket Mode path: reaction lifecycle,
    # threaded reply.
    assert ("10.1", "eyes") in api.reacts
    assert api.posts or api.updates


async def test_repeated_event_id_is_accepted_but_not_run_again():
    handler = EchoHandler()
    transport = over_http()
    async with client(build_a2a_app(handler, slack=transport)) as c:
        first = await c.post(EVENTS_PATH, json=envelope("Ev42", ts="1.1"), headers={HEADER: SECRET})
        retry = await c.post(EVENTS_PATH, json=envelope("Ev42", ts="1.1"), headers={HEADER: SECRET})
        await settled(transport)
    assert first.status_code == retry.status_code == 200
    assert retry.json() == {"accepted": True}
    assert len(handler.tasks) == 1


async def test_url_verification_is_answered_with_its_challenge():
    handler = EchoHandler()
    transport = over_http()
    async with client(build_a2a_app(handler, slack=transport)) as c:
        response = await c.post(
            EVENTS_PATH,
            json={"type": "url_verification", "token": "t", "challenge": "abc123"},
            headers={HEADER: SECRET},
        )
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc123"}
    assert handler.tasks == []


async def test_bad_bodies_are_400_not_turns():
    handler = EchoHandler()
    transport = over_http()
    async with client(build_a2a_app(handler, slack=transport)) as c:
        not_json = await c.post(EVENTS_PATH, content=b"{nope", headers={HEADER: SECRET})
        a_list = await c.post(EVENTS_PATH, json=[1, 2], headers={HEADER: SECRET})
        not_an_event = await c.post(EVENTS_PATH, json={"type": "other"}, headers={HEADER: SECRET})
        await settled(transport)
    assert not_json.status_code == 400
    assert a_list.status_code == 400
    assert not_an_event.status_code == 200  # admitted by the gate, dropped by the transport
    assert handler.tasks == []


async def test_without_a_slack_transport_the_route_is_404():
    async with client(build_a2a_app(EchoHandler())) as c:
        response = await c.post(EVENTS_PATH, json=envelope(), headers={HEADER: SECRET})
    assert response.status_code == 404


async def test_an_envelope_that_lands_before_serve_waits_and_is_not_lost():
    """The room restarts an agent after installing its app; a relay post can
    reach the a2a app before the Slack transport has its web client."""
    handler = EchoHandler()
    transport = SlackTransport(CH, bot_token="xoxb", app_token=None, relay_secret=SECRET)
    transport.app_token = None
    transport._api = None
    async with client(build_a2a_app(handler, slack=transport)) as c:
        response = await c.post(EVENTS_PATH, json=envelope(ts="2.2"), headers={HEADER: SECRET})
        assert response.status_code == 200
        await asyncio.sleep(0)
        assert handler.tasks == []  # waiting, not dropped
        transport._api = FakeApi()
        transport._started.set()
        await settled(transport)
    assert len(handler.tasks) == 1


# ---- the dedupe lives in the transport, shared by both ways in ------------------


def test_admit_shares_one_event_id_memory_between_socket_and_http():
    transport = over_http()
    assert transport.admit(envelope("EvA", ts="3.1")) == event(ts="3.1")
    assert transport.admit(envelope("EvA", ts="3.1")) is None  # Socket Mode's listener path
    assert transport.receive(envelope("EvA", ts="3.1"), EchoHandler()) is False  # the HTTP path
    assert transport.admit({"type": "url_verification"}) is None
    assert transport.admit("not a dict") is None
    # No event_id: admitted unchecked, as the socket listener always did.
    assert transport.admit({"type": "event_callback", "event": {"type": "message"}}) == {
        "type": "message"
    }
    assert transport.admit({"type": "event_callback"}) == {}


def test_event_ids_are_bounded_oldest_first():
    transport = over_http()
    for i in range(_EVENT_ID_CAP + 5):
        transport.admit(envelope(f"Ev{i}"))
    assert len(transport._event_ids) == _EVENT_ID_CAP
    assert "Ev0" not in transport._event_ids
    assert f"Ev{_EVENT_ID_CAP + 4}" in transport._event_ids
    assert transport.admit(envelope("Ev0")) is not None  # forgotten, so admitted again


# ---- transport choice at start ----------------------------------------------------


def _bundle(tmp_path):
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "transports:\n- kind: a2a\n  port: 9931\n- kind: slack\n"
        "tools:\n  provider: none\n  allow: []\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    return tmp_path


def _agent(tmp_path, monkeypatch):
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from tests.fakes import FakeTools, ScriptedLLM

    monkeypatch.setitem(sys.modules, "slack_sdk", types.ModuleType("slack_sdk"))
    return Agent(Bundle.load(_bundle(tmp_path)), llm=ScriptedLLM("done"), tools=FakeTools([]))


def test_app_level_token_present_means_socket_mode_exactly_as_today(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp")
    monkeypatch.setenv("SLACK_RELAY_SECRET", SECRET)  # even with the secret set
    transports = _agent(tmp_path, monkeypatch).transports()
    a2a = next(t for t in transports if isinstance(t, A2aTransport))
    slack = next(t for t in transports if isinstance(t, SlackTransport))
    assert slack.over_http is False
    assert a2a.slack is None
    assert slack.way_in_line() == "(slack events arrive over Socket Mode)"


def test_bot_token_alone_means_the_http_way_in(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_RELAY_SECRET", SECRET)
    transports = _agent(tmp_path, monkeypatch).transports()
    a2a = next(t for t in transports if isinstance(t, A2aTransport))
    slack = next(t for t in transports if isinstance(t, SlackTransport))
    assert slack.over_http is True
    assert a2a.slack is slack
    assert slack.relay_secret == SECRET
    assert EVENTS_PATH in slack.way_in_line()
    assert "no socket" in slack.way_in_line()


def test_neither_token_attaches_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    transports = _agent(tmp_path, monkeypatch).transports()
    a2a = next(t for t in transports if isinstance(t, A2aTransport))
    assert a2a.slack is None


async def test_serve_over_http_opens_no_socket(monkeypatch, capsys):
    """Socket Mode's imports would blow up here; the HTTP branch never reaches them."""
    for name in ("slack_sdk", "slack_sdk.socket_mode", "slack_sdk.socket_mode.aiohttp"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    class Boom:
        def __init__(self, *a, **kw):
            raise AssertionError("a socket was opened in HTTP mode")

    sys.modules["slack_sdk.socket_mode.aiohttp"].SocketModeClient = Boom

    class ServeApi(FakeApi):
        """serve() asks the workspace and bot identity first."""

        async def team_id(self):
            return "T0TEAM"

        async def auth_test(self):
            return "UBOT"

    transport = over_http(api=ServeApi())
    serving = asyncio.create_task(transport.serve(EchoHandler()))
    started = asyncio.create_task(transport._started.wait())
    done, _ = await asyncio.wait({serving, started}, timeout=2, return_when="FIRST_COMPLETED")
    if started not in done:
        serving.result()  # raises serve()'s own error when it failed instead
        raise AssertionError("serve() did not reach the ready point")
    assert transport.team_id == "T0TEAM"
    assert transport.user_id == "UBOT"
    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving
    out = capsys.readouterr().out
    assert "(slack listening in C0MAIN, on @-mention)" in out
    assert f"(slack events arrive over HTTPS at POST {EVENTS_PATH}; no socket opened)" in out


def test_serve_without_a_bot_token_is_still_a_clean_error():
    transport = SlackTransport(CH, bot_token=None, app_token=None)
    transport.bot_token = None
    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        asyncio.new_event_loop().run_until_complete(transport.serve(EchoHandler()))
