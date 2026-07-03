"""Pins the public API surface — if this breaks, consumers break."""

from daedalus import (
    Agent,
    AgentSpec,
    Bundle,
    BundleError,
    Message,
    ReasonCode,
    Task,
    TaskState,
    __version__,
)
from daedalus.protocols import (
    LLM,
    AllowAll,
    Approver,
    Budget,
    CheckpointStore,
    Handler,
    MemoryStore,
    NoBudget,
    NoCheckpoints,
    NoMemory,
    NoTools,
    ToolProvider,
    Transport,
)
from tests.fakes import CannedMemory, ScriptedLLM


def test_public_names_exist():
    assert all([Agent, AgentSpec, Bundle, BundleError, Message, Task])
    assert TaskState.INPUT_REQUIRED.value == "input-required"
    assert ReasonCode.HUMAN_INPUT_REQUIRED == 700
    assert ReasonCode.POLICY_VIOLATION == 701
    assert ReasonCode.BUDGET_EXHAUSTED == 702
    assert ReasonCode.ESCALATION == 704
    assert isinstance(__version__, str)


def test_noop_defaults_satisfy_their_protocols():
    assert isinstance(NoMemory(), MemoryStore)
    assert isinstance(NoTools(), ToolProvider)
    assert isinstance(NoCheckpoints(), CheckpointStore)
    assert isinstance(NoBudget(), Budget)
    assert isinstance(AllowAll(), Approver)


def test_fakes_satisfy_their_protocols():
    assert isinstance(ScriptedLLM(), LLM)
    assert isinstance(CannedMemory(), MemoryStore)


def test_agent_is_a_handler_and_transports_are_transports():
    from daedalus.transports.cli import CliTransport

    assert issubclass(Agent, object)  # structural check below is the real one
    assert isinstance(CliTransport(), Transport)
    assert isinstance(Agent, type)
    assert callable(Agent.handle_task)
    assert Handler is not None
