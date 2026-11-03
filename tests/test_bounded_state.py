"""The a2a task store and the Slack thread map are bounded (#202)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from daedalus.task import Task, TaskState
from daedalus.transports.a2a import build_a2a_app
from tests.test_transport_a2a_ownership import CountingHandler, get, peer


class StaysWorking:
    async def handle_task(self, task: Task) -> Task:
        task.state = TaskState.WORKING
        return task


def test_finished_tasks_past_the_cap_are_dropped_oldest_first():
    client = TestClient(build_a2a_app(CountingHandler(), max_tasks=3))
    for i in range(5):
        assert peer(client, None, f"t{i}", f"q{i}").status_code == 200
    for i in (0, 1):
        assert "error" in get(client, None, f"t{i}"), f"t{i} should be evicted"
    for i in (2, 3, 4):
        assert get(client, None, f"t{i}")["result"]["id"] == f"t{i}"


def test_a_running_task_is_never_dropped():
    client = TestClient(build_a2a_app(StaysWorking(), max_tasks=2))
    for i in range(4):
        peer(client, None, f"w{i}", "x")
    for i in range(4):
        assert get(client, None, f"w{i}")["result"]["id"] == f"w{i}"


def test_slack_threads_are_dropped_least_recently_used_first():
    from daedalus.transports.slack import _THREAD_CAP, SlackTransport

    transport = SlackTransport("C1", bot_token=None, app_token=None)
    for i in range(_THREAD_CAP):
        transport._remember_thread(("C1", str(i)), Task(context_id=str(i)))
    transport._remember_thread(("C1", "0"), Task(context_id="0"))  # used again
    transport._remember_thread(("C1", "new"), Task(context_id="new"))
    assert len(transport._threads) == _THREAD_CAP
    assert ("C1", "0") in transport._threads
    assert ("C1", "1") not in transport._threads
    assert ("C1", "new") in transport._threads
