from daedalus.task import REASON_KEY, Message, ReasonCode, Task, TaskState


def test_from_text_builds_submitted_task_with_one_user_message():
    task = Task.from_text("hello", context_id="thread-1")
    assert task.state is TaskState.SUBMITTED
    assert task.context_id == "thread-1"
    assert task.history == [Message(role="user", text="hello")]
    assert task.user_text() == "hello"
    assert task.agent_text() == ""


def test_reply_appends_agent_message_and_completes():
    task = Task.from_text("hi")
    result = task.reply("hello there")
    assert result is task
    assert task.state is TaskState.COMPLETED
    assert task.agent_text() == "hello there"
    assert task.reason_code is None


def test_reply_with_reason_code_round_trips():
    task = Task.from_text("deploy prod")
    task.reply("needs a human", state=TaskState.INPUT_REQUIRED, reason=ReasonCode.ESCALATION)
    assert task.state is TaskState.INPUT_REQUIRED
    assert task.metadata[REASON_KEY] == 704
    assert task.reason_code is ReasonCode.ESCALATION
    # survives serialization — codes ride in metadata, not the envelope
    rehydrated = Task.model_validate(task.model_dump())
    assert rehydrated.reason_code is ReasonCode.ESCALATION


def test_user_text_returns_latest_user_message():
    task = Task.from_text("first")
    task.reply("ack")
    task.history.append(Message(role="user", text="second"))
    assert task.user_text() == "second"
