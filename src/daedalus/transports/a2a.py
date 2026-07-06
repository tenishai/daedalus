"""The a2a transport — a daedalus bundle as a callable A2A peer.

Serves the bundle's agent card at /.well-known/agent-card.json and accepts
A2A JSON-RPC (message/send, tasks/get). Non-streaming, unauthenticated,
bound to loopback by default — card signing and JWT verification land with
the cabal integration round. Requires the a2a extra: pip install
'daedalus[a2a]'.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, Request  # module imported only when the extra is present
from fastapi.responses import JSONResponse, StreamingResponse

from daedalus.events import EventBus
from daedalus.protocols import Handler
from daedalus.task import Message, Task, TaskState


class ParamsError(ValueError):
    pass


def task_to_a2a(task: Task) -> dict:
    return {
        "kind": "task",
        "id": task.id,
        "contextId": task.context_id or task.id,
        "status": {"state": task.state.value},
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


async def sse_stream(events: EventBus):
    """Replay recent events, then stream live — each as an SSE data frame."""
    replay, queue = events.subscribe()
    try:
        for event in replay:
            yield f"data: {json.dumps(event, default=str)}\n\n"
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event, default=str)}\n\n"
    finally:
        events.unsubscribe(queue)


def build_a2a_app(
    handler: Handler, card: dict | None = None, events: EventBus | None = None
) -> FastAPI:
    """Separable from serving so tests drive it with a TestClient."""
    app = FastAPI()
    tasks: dict[str, Task] = {}
    lock = asyncio.Lock()

    def error(rpc_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}

    @app.get("/.well-known/agent-card.json")
    async def agent_card() -> dict:
        return card or {}

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/events")
    async def event_stream():
        """The runtime trace over SSE — the kaminos trace pane's feed.
        Opens with a replay of recent events, then streams live."""
        if events is None:
            return JSONResponse({"error": "no event stream wired"}, status_code=404)
        return StreamingResponse(sse_stream(events), media_type="text/event-stream")

    async def message_send(params: dict) -> dict:
        message = params.get("message")
        if not isinstance(message, dict):
            raise ParamsError("params.message is required")
        text = text_of(message)
        task_id = message.get("taskId")
        async with lock:
            if task_id:
                task = tasks.get(task_id)
                if task is None:
                    raise ParamsError(f"unknown taskId {task_id!r}")
                task.history.append(Message(role="user", text=text))
                task.state = TaskState.SUBMITTED
            else:
                task = Task.from_text(text, context_id=message.get("contextId"))
                tasks[task.id] = task
        task = await handler.handle_task(task)
        async with lock:
            tasks[task.id] = task
        return task_to_a2a(task)

    async def tasks_get(params: dict) -> dict:
        task_id = params.get("id")
        async with lock:
            task = tasks.get(task_id)
        if task is None:
            raise ParamsError(f"unknown task id {task_id!r}")
        return task_to_a2a(task)

    methods = {"message/send": message_send, "tasks/get": tasks_get}

    @app.post("/")
    async def rpc(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            return error(None, -32700, "parse error")
        rpc_id = body.get("id")
        method = methods.get(body.get("method"))
        if method is None:
            return error(rpc_id, -32601, f"method not found: {body.get('method')!r}")
        try:
            result = await method(body.get("params") or {})
        except ParamsError as exc:
            return error(rpc_id, -32602, str(exc))
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    return app


class A2aTransport:
    def __init__(
        self,
        card: dict | None = None,
        host: str = "127.0.0.1",
        port: int = 9200,
        events: EventBus | None = None,
    ):
        self.card = card
        self.host = host
        self.port = port
        self.events = events

    async def serve(self, handler: Handler) -> None:
        import uvicorn

        app = build_a2a_app(handler, card=self.card, events=self.events)
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        print(f"(a2a listening on {self.host}:{self.port})")
        await uvicorn.Server(config).serve()
