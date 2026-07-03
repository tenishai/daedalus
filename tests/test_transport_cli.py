from daedalus.task import Task, TaskState
from daedalus.transports.cli import run_once


class EchoHandler:
    async def handle_task(self, task: Task) -> Task:
        return task.reply(f"echo: {task.user_text()}")


async def test_run_once_returns_completed_task():
    task = await run_once(EchoHandler(), "ping", context_id="ci")
    assert task.state is TaskState.COMPLETED
    assert task.agent_text() == "echo: ping"
    assert task.context_id == "ci"
