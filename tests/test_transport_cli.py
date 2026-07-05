import os

from daedalus.task import Task, TaskState
from daedalus.transports.cli import read_message, run_once


class EchoHandler:
    async def handle_task(self, task: Task) -> Task:
        return task.reply(f"echo: {task.user_text()}")


async def test_run_once_returns_completed_task():
    task = await run_once(EchoHandler(), "ping", context_id="ci")
    assert task.state is TaskState.COMPLETED
    assert task.agent_text() == "echo: ping"
    assert task.context_id == "ci"


def _pipe_stream(data: bytes, close_writer: bool = True):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, data)
    if close_writer:
        os.close(write_fd)
        return os.fdopen(read_fd), None
    return os.fdopen(read_fd), write_fd


def test_pasted_multiline_input_is_one_message():
    """Dogfood finding #7: a pasted Makefile became ~8 separate LLM turns."""
    stream, _ = _pipe_stream(b"here is the file:\n.PHONY: test\n\ntest:\n\tpytest -q\n")
    message = read_message("", stream=stream)
    assert message == "here is the file:\n.PHONY: test\n\ntest:\n\tpytest -q"
    assert read_message("", stream=stream) is None  # EOF afterwards


def test_typed_single_line_stays_single(capsys):
    stream, write_fd = _pipe_stream(b"hello\n", close_writer=False)
    try:
        assert read_message("", stream=stream) == "hello"
        assert "captured" not in capsys.readouterr().out
    finally:
        os.close(write_fd)


def test_eof_returns_none():
    stream, _ = _pipe_stream(b"")
    assert read_message("", stream=stream) is None
