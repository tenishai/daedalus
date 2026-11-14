"""The a2a transport — a daedalus bundle as a callable A2A peer.

Serves the bundle's agent card at /.well-known/agent-card.json and accepts
two dispatch surfaces: A2A JSON-RPC (message/send, tasks/get) for spec
callers, and POST /a2a/tasks taking a bare Task envelope — the shape a
cabal's DispatchRouter sends to federated peers (broker/router.py
_dispatch_remote). N.B. the switchboard's own inbound /a2a/tasks speaks a
different dialect — a routing envelope {target, task} (what the colleagues
provider POSTs) — the shared path is convention, not compatibility: this
surface takes the bare Task only. Non-streaming, bound to loopback by default. Optional
JWT verification (transport `auth` config) gates both POST surfaces and the
GET /events trace stream (#196), and binds each stored task to the sub that
created it (#197); card
signing stays deferred (cabal accepts an empty card at graduation and does
not verify cards on dispatch). Requires the a2a extra: pip install
'tenish-daedalus[a2a]'.

POST /slack/events is the Slack transport's HTTP way in (kobo#304): the room's
relay forwards Slack's event envelopes here when the agent has no app-level
token. It is gated by the per-agent relay secret, not the JWT verifier, and it
exists only when the Slack transport asked for it (`slack=`); otherwise 404.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import sys
from collections import OrderedDict

from fastapi import FastAPI, Request  # module imported only when the extra is present
from fastapi.responses import JSONResponse, StreamingResponse

from daedalus.events import EventBus
from daedalus.protocols import Handler
from daedalus.task import VERIFIED_SUB_KEY, Message, ReasonCode, Task, TaskState

log = logging.getLogger(__name__)


class ParamsError(ValueError):
    pass


def task_to_a2a(task: Task) -> dict:
    status: dict = {"state": task.state.value}
    reply = task.agent_text()
    if reply:
        # Cabal's convention carries the current message in status.message —
        # cbl renders replies from there (finding #22). History keeps the
        # full transcript for spec callers; both stay populated.
        status["message"] = {
            "kind": "message",
            "role": "agent",
            "parts": [{"kind": "text", "text": reply}],
        }
    return {
        "kind": "task",
        "id": task.id,
        "contextId": task.context_id or task.id,
        "status": status,
        "history": [
            {"kind": "message", "role": m.role, "parts": [{"kind": "text", "text": m.text}]}
            for m in task.history
        ],
        "metadata": task.metadata,
    }


def text_of(message: dict) -> str:
    parts = message.get("parts") or []
    texts = [p.get("text", "") for p in parts if p.get("kind") == "text"]
    joined = "\n".join(t for t in texts if t)
    if not joined:
        raise ParamsError("message has no text parts")
    return joined


def task_from_a2a(payload: dict) -> Task:
    """A bare A2A Task envelope (cabal's outbound dispatch shape) -> Task.

    Metadata rides through verbatim — cabal's routing context (cabal_trace_id,
    cabal_span_id, ...) must come back in the response envelope. Messages
    without text parts are skipped; a task with no user text is rejected.
    """
    if not isinstance(payload, dict) or payload.get("kind") not in (None, "task"):
        raise ParamsError("body is not an A2A task")
    task_id = payload.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ParamsError("task.id is required")
    history: list[Message] = []
    for message in payload.get("history") or []:
        if message.get("role") not in ("user", "agent"):
            continue
        try:
            history.append(Message(role=message["role"], text=text_of(message)))
        except ParamsError:
            continue  # data-only message: nothing for a text loop to carry
    # Cabal carries the CURRENT message in status.message with an empty
    # history (finding #22) — ingest it as the latest turn. Skipped if a
    # history-carrying caller already ends with the identical message.
    status_message = (payload.get("status") or {}).get("message")
    if isinstance(status_message, dict) and status_message.get("role") in ("user", "agent"):
        try:
            latest = Message(role=status_message["role"], text=text_of(status_message))
        except ParamsError:
            latest = None
        if latest is not None and (not history or history[-1] != latest):
            history.append(latest)
    task = Task(
        id=task_id,
        context_id=payload.get("contextId"),
        state=TaskState.SUBMITTED,
        history=history,
        metadata=dict(payload.get("metadata") or {}),
    )
    if not task.user_text():
        raise ParamsError("task has no user text")
    return task


def mark_failed(task: Task, exc: BaseException) -> None:
    """End a stored task whose handler raised or was cancelled (#201).

    Without this the task kept the WORKING state the handler set, and
    tasks/get reported it working until the pod restarted. FAILED with
    reason 704, the code a down tool provider already gets, so cabal reads
    it as a failure to pass to the operator."""
    if isinstance(exc, asyncio.CancelledError):
        text = "agent error: the task was cancelled before it finished"
    else:
        text = f"agent error: {exc}"
    task.reply(text, state=TaskState.FAILED, reason=ReasonCode.ESCALATION)


async def run_handler(handler: Handler, task: Task) -> Task:
    """handler.handle_task(task), marking `task` (the stored object) FAILED
    on any exception or cancellation before re-raising it."""
    try:
        return await handler.handle_task(task)
    except BaseException as exc:
        mark_failed(task, exc)
        raise


def stamp_verified_sub(task: Task, claims: dict | None) -> None:
    """The transport owns metadata[VERIFIED_SUB_KEY]: wire-supplied values
    are discarded unconditionally, and the key exists only when a verifier
    accepted the caller's token and its claims carried a sub. Handlers
    (the teach lane) trust it on that basis."""
    task.metadata.pop(VERIFIED_SUB_KEY, None)
    sub = (claims or {}).get("sub")
    if isinstance(sub, str) and sub:
        task.metadata[VERIFIED_SUB_KEY] = sub


#: Set to "1" to put tool arguments, tool results and reasoning text on the
#: /events wire. Off by default (#196): those fields carry whatever the agent
#: read (files, Slack threads, colleague answers), and /events is a network
#: surface. The in-process bus keeps them either way: `run -v` and the Slack
#: transport's progress lines read them there, not over HTTP.
TRACE_CONTENT_ENV = "DAEDALUS_TRACE_CONTENT"

#: event type -> the field that carries content. On the wire, without
#: TRACE_CONTENT_ENV, the field is replaced by `<field>_chars`, its length.
_CONTENT_FIELDS = {"tool_call": "arguments", "tool_result": "content", "reasoning": "text"}


def trace_content_enabled() -> bool:
    return os.environ.get(TRACE_CONTENT_ENV) == "1"


def wire_event(event: dict, content: bool) -> dict:
    """The event as /events sends it. Names, ids, counts and types always
    go out; with `content` False the content field becomes its size."""
    field = _CONTENT_FIELDS.get(event.get("type"))
    if content or field is None or field not in event:
        return event
    value = event[field]
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    redacted = {k: v for k, v in event.items() if k != field}
    redacted[f"{field}_chars"] = len(text)
    return redacted


async def sse_stream(events: EventBus, content: bool = False):
    """Replay recent events, then stream live — each as an SSE data frame.
    `content` False sends each event through wire_event's redaction."""
    replay, queue = events.subscribe()
    try:
        for event in replay:
            yield f"data: {json.dumps(wire_event(event, content), default=str)}\n\n"
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(wire_event(event, content), default=str)}\n\n"
    finally:
        events.unsubscribe(queue)


#: Tasks one a2a app keeps in memory (#202); only finished ones are evicted.
MAX_STORED_TASKS = 1000
_FINISHED = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED})


def build_a2a_app(
    handler: Handler,
    card: dict | None = None,
    events: EventBus | None = None,
    verifier=None,
    trace_content: bool | None = None,
    max_tasks: int = MAX_STORED_TASKS,
    slack=None,
) -> FastAPI:
    """Separable from serving so tests drive it with a TestClient.

    `slack` (a SlackTransport serving over HTTP, duck-typed: `relay_secret`,
    `receive(envelope, handler)`) turns on POST /slack/events (kobo#304). The
    route hands every admitted envelope to that transport with THIS app's
    `handler`, the same Agent every transport serves.

    `max_tasks` bounds the in-memory task store (#202): past it, the least
    recently stored FINISHED tasks are dropped, with their owner records. A task
    still running is never dropped.

    `verifier` (a JwtVerifier, duck-typed) gates both POST surfaces and
    GET /events when present (#196); the card and healthz GETs stay open.
    `trace_content` (default: DAEDALUS_TRACE_CONTENT=1) decides whether
    /events carries tool arguments, results and reasoning text.
    """
    if trace_content is None:
        trace_content = trace_content_enabled()
    app = FastAPI()
    tasks: OrderedDict[str, Task] = OrderedDict()
    # task id -> the verified sub that created it (#197). Kept beside the task,
    # not in its metadata, so nothing a handler returns can change the owner.
    owners: dict[str, str | None] = {}
    lock = asyncio.Lock()

    def store(task: Task) -> None:
        """tasks[task.id] = task, most recent last, then evict (#202). Call under
        lock. A busy agent used to keep every task it ever ran until the pod
        was OOM-killed; finished tasks are dropped oldest first past max_tasks,
        and a dropped id answers like an unknown one."""
        tasks[task.id] = task
        tasks.move_to_end(task.id)
        excess = len(tasks) - max_tasks
        if excess <= 0:
            return
        for old_id in [i for i, t in tasks.items() if t.state in _FINISHED][:excess]:
            del tasks[old_id]
            owners.pop(old_id, None)

    def caller_sub(claims: dict | None) -> str | None:
        sub = (claims or {}).get("sub")
        return sub if isinstance(sub, str) and sub else None

    def visible(task_id: object, claims: dict | None) -> Task | None:
        """The stored task when this caller may see it, else None (#197).

        With no verifier there is no caller identity, so every task is
        visible, as before. With a verifier, only the sub that created the
        task sees it; a token without a sub sees none. Callers answer None
        exactly as they answer a missing id, so through tasks/get and
        continuation another caller's task id cannot be told apart from one
        that does not exist. POST /a2a/tasks still answers 409 for any stored
        id, whoever owns it. Call under lock.
        """
        task = tasks.get(task_id) if isinstance(task_id, str) else None
        if task is None or verifier is None:
            return task
        sub = caller_sub(claims)
        if sub is None or owners.get(task_id) != sub:
            return None
        return task

    def error(rpc_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}

    async def authorize(request: Request) -> tuple[JSONResponse | None, dict | None]:
        """(401, None) when auth is configured and the token fails;
        (None, claims) when it verifies; (None, None) with no verifier."""
        if verifier is None:
            return None, None
        from daedalus.transports.a2a_auth import AuthError

        try:
            claims = await verifier.verify_header(request.headers.get("authorization"))
        except AuthError as exc:
            return (
                JSONResponse(
                    {"error": f"unauthorized: {exc}"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                ),
                None,
            )
        return None, claims

    @app.get("/.well-known/agent-card.json")
    async def agent_card() -> dict:
        return card or {}

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/events")
    async def event_stream(request: Request):
        """The runtime trace over SSE — the kaminos trace pane's feed.
        Opens with a replay of recent events, then streams live. Behind the
        same verifier as the POSTs (#196): the replay alone is the last 100
        events, so an open door here leaks after the fact too."""
        denied, _claims = await authorize(request)
        if denied is not None:
            return denied
        if events is None:
            return JSONResponse({"error": "no event stream wired"}, status_code=404)
        return StreamingResponse(
            sse_stream(events, content=trace_content), media_type="text/event-stream"
        )

    async def message_send(params: dict, claims: dict | None) -> dict:
        message = params.get("message")
        if not isinstance(message, dict):
            raise ParamsError("params.message is required")
        text = text_of(message)
        task_id = message.get("taskId")
        async with lock:
            if task_id:
                task = visible(task_id, claims)
                if task is None:
                    raise ParamsError(f"unknown taskId {task_id!r}")
                task.history.append(Message(role="user", text=text))
                task.state = TaskState.SUBMITTED
                stamp_verified_sub(task, claims)
            else:
                task = Task.from_text(text, context_id=message.get("contextId"))
                stamp_verified_sub(task, claims)
                tasks[task.id] = task
                owners[task.id] = caller_sub(claims)
        task = await run_handler(handler, task)
        async with lock:
            store(task)
        return task_to_a2a(task)

    async def tasks_get(params: dict, claims: dict | None) -> dict:
        task_id = params.get("id")
        async with lock:
            task = visible(task_id, claims)
        if task is None:
            raise ParamsError(f"unknown task id {task_id!r}")
        return task_to_a2a(task)

    methods = {"message/send": message_send, "tasks/get": tasks_get}

    @app.post("/a2a/tasks")
    async def peer_dispatch(request: Request):
        """Cabal's federated-dispatch surface: bare Task in, bare Task out.

        Error bodies are readable JSON — cabal wraps any >=400 response text
        in a DispatchError, so what we return here is what its operator sees.
        """
        denied, claims = await authorize(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "body is not valid JSON"}, status_code=400)
        try:
            task = task_from_a2a(body)
        except ParamsError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        stamp_verified_sub(task, claims)
        async with lock:
            if task.id in tasks:
                # Never replace a stored task (#197): the id is the caller's
                # choice, so replacing would let any caller overwrite any
                # other caller's task. cabal and kobo mint a fresh id per hop.
                return JSONResponse(
                    {"error": f"task id {task.id!r} already exists; send a new id"},
                    status_code=409,
                )
            tasks[task.id] = task
            owners[task.id] = caller_sub(claims)
        try:
            task = await run_handler(handler, task)
        except Exception as exc:  # handler bug: readable body, not a bare 500
            return JSONResponse({"error": f"agent error: {exc}"}, status_code=500)
        async with lock:
            store(task)
        return task_to_a2a(task)

    @app.post("/slack/events")
    async def slack_events(request: Request):
        """The Slack transport's HTTP way in (kobo#304). Slack posts to the
        room's relay; the relay verifies Slack's signature and forwards the
        envelope here with the per-agent secret. 503 while the secret is
        unset (the room has not wired this agent), 403 when it differs, 200
        `{"accepted": true}` at once for anything admitted; the agent's turn
        runs after the answer. A repeated event_id is accepted and dropped in
        the transport, so a retry never runs the turn twice. Slack's
        url_verification is the room's to answer, but is answered here too.

        A click and a slash command come through here as well (2026-11-14):
        the relay forwards a `block_actions` payload as Slack sent it and a
        command's form fields as one object with `type: slash_command`. The
        transport tells them from events by that `type` and runs a repeated
        `trigger_id` once.
        """
        if slack is None:
            return JSONResponse(
                {"error": "this agent takes Slack events over Socket Mode, not HTTP"},
                status_code=404,
            )
        secret = slack.relay_secret
        if not secret:
            return JSONResponse(
                {"error": "SLACK_RELAY_SECRET is not set on this agent"}, status_code=503
            )
        given = request.headers.get("x-kobo-relay-secret") or ""
        if not hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8")):
            return JSONResponse({"error": "relay secret does not match"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body is not a Slack envelope"}, status_code=400)
        if body.get("type") == "url_verification":
            return {"challenge": body.get("challenge", "")}
        slack.receive(body, handler)
        return {"accepted": True}

    @app.post("/")
    async def rpc(request: Request):
        denied, claims = await authorize(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            return error(None, -32700, "parse error")
        rpc_id = body.get("id")
        method = methods.get(body.get("method"))
        if method is None:
            return error(rpc_id, -32601, f"method not found: {body.get('method')!r}")
        try:
            result = await method(body.get("params") or {}, claims)
        except ParamsError as exc:
            return error(rpc_id, -32602, str(exc))
        except Exception as exc:  # handler bug or backend down: JSON-RPC, not HTTP 500
            return error(rpc_id, -32000, f"agent error: {exc}")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    return app


class A2aTransport:
    def __init__(
        self,
        card: dict | None = None,
        host: str = "127.0.0.1",
        port: int = 9200,
        events: EventBus | None = None,
        verifier=None,
        name: str | None = None,
        slack=None,
    ):
        self.name = name  # the bundle, for the open-listener warning (#204)
        # The Slack transport when it takes events over HTTP (kobo#304); the
        # Agent attaches it after building both transports. None = no route.
        self.slack = slack
        self.card = card
        self.host = host
        self.port = port
        self.events = events
        self.verifier = verifier

    async def serve(self, handler: Handler) -> None:
        import uvicorn

        app = build_a2a_app(
            handler,
            card=self.card,
            events=self.events,
            verifier=self.verifier,
            slack=self.slack,
        )
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        print(self.listening_line())
        warning = self.open_warning()
        if warning:
            # Once: through logging when the process configured it, else on stderr.
            # With no handler, log.warning would reach stderr through logging's
            # lastResort and the line would print twice.
            if log.hasHandlers():
                log.warning(warning)
            else:
                print(warning, file=sys.stderr)
        await uvicorn.Server(config).serve()

    def listening_line(self) -> str:
        """The boot line, with the auth regime in force (#204)."""
        regime = "JWT auth" if self.verifier is not None else "NO AUTH"
        return f"(a2a listening on {self.host}:{self.port}, {regime})"

    def open_warning(self) -> str | None:
        """A warning when anyone who can reach the port can drive the agent (#204):
        no auth on an address other than loopback, e.g. the image's 0.0.0.0."""
        if self.verifier is not None or _is_loopback(self.host):
            return None
        return (
            f"WARNING: bundle {self.name or '(unnamed)'} serves a2a on "
            f"{self.host}:{self.port} with no auth. Anyone who can reach this port can "
            "use every tool the allowlist admits. Add an auth block (jwks_url, issuer, "
            "audience) to the a2a transport in agent.yaml."
        )


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False
