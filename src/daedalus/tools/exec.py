"""exec.run: run a command in the agent's own workspace (tenishai/agents#51, Fates step 2).

Its own module, not builtin.py: that module is read-only by rule, and write access
to shared resources belongs to the gateway (liminal). This tool runs where the
agent's checkout is, which no gateway can reach, and nowhere else.

The tool never spawns processes itself; it calls a Workspace (architect pattern
5c9f764a10b9e4c2), so a remote sandbox backend can replace LocalWorkspace later
without touching the tool. Every backend states the boundary it actually provides
(architect f1d4882337f6ceeb).

Guards, all at load or before the process starts:
- enabled only with approvals.ceiling.exec: true in the bundle (agent.py);
- the command is an argv list, never a shell string; an optional `commands`
  allowlist names the programs that may run, as bare names resolved on PATH;
- the working directory must resolve under the configured roots;
- the child gets an allowlisted environment (a few basics plus names the bundle
  lists), never the agent's own. That keeps secrets from being handed to it, but it
  is not a boundary: the command runs as the same user, so it can read whatever the
  agent can, including other processes' environments under /proc (measured in the
  runtime image). Running untrusted code safely needs a workspace that doesn't share
  the agent's user or secrets;
- a wall-clock timeout kills the whole process group, and each output stream is
  capped, keeping its head and its tail (test runners print the verdict last).
Network egress is not controlled here (daedalus#114).
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM")
_DEFAULT_TIMEOUT = 300
_DEFAULT_MAX_OUTPUT = 20_000  # bytes kept per stream: half head, half tail
_DRAIN_AFTER_EXIT = 2.0  # seconds to collect output once the process has exited


@dataclass
class ExecResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    seconds: float


class Workspace(Protocol):
    """Where exec.run's commands actually run."""

    isolation: str

    async def run(
        self, argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float, max_output: int
    ) -> ExecResult: ...


class _Capture:
    """Keeps the first and last `cap // 2` bytes of a stream and counts the rest."""

    def __init__(self, cap: int):
        self.half = max(cap // 2, 1)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.half - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            del self.tail[: max(len(self.tail) - self.half, 0)]

    def text(self) -> str:
        head = self.head.decode(errors="replace")
        elided = self.total - len(self.head) - len(self.tail)
        if elided <= 0:
            return head + self.tail.decode(errors="replace")
        return f"{head}\n[... {elided} bytes elided ...]\n{self.tail.decode(errors='replace')}"


class LocalWorkspace:
    """Runs commands as the agent, in the agent's own container or host."""

    isolation = (
        "none: runs as the agent, in its own container or host; "
        "a container is not a security boundary"
    )

    async def run(
        self, argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float, max_output: int
    ) -> ExecResult:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, so a timeout kills the whole tree
        )
        out, err = _Capture(max_output), _Capture(max_output)

        async def pump(stream, capture):
            while chunk := await stream.read(65536):
                capture.add(chunk)

        pumps = asyncio.gather(pump(proc.stdout, out), pump(proc.stderr, err))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            timed_out = True
        _kill_group(proc.pid)  # also reaps a child that outlived its parent holding the pipes
        await proc.wait()
        try:
            await asyncio.wait_for(pumps, _DRAIN_AFTER_EXIT)
        except TimeoutError:
            pumps.cancel()
        return ExecResult(
            returncode=None if timed_out else proc.returncode,
            stdout=out.text(),
            stderr=err.text(),
            timed_out=timed_out,
            seconds=time.monotonic() - started,
        )


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _positive_int(name: str, value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number, not {value!r}") from None
    if number <= 0:
        raise ValueError(f"{name} must be positive, not {number}")
    return number


class ExecToolProvider:
    """exec.run, confined to configured roots, through a Workspace."""

    def __init__(
        self,
        roots: list[str],
        commands: list[str] | None = None,
        env: list[str] | None = None,
        timeout: int | str = _DEFAULT_TIMEOUT,
        max_output: int | str = _DEFAULT_MAX_OUTPUT,
        backend: str = "local",
        workspace: Workspace | None = None,
    ):
        if not roots:
            raise ValueError("exec builtin requires at least one root")
        self.roots = [Path(root).resolve() for root in roots]
        commands = list(commands or [])
        for command in commands:
            if not isinstance(command, str) or not command or "/" in command:
                raise ValueError(f"commands lists bare program names, not {command!r}")
        self.commands = commands
        self.env_names = [*_BASE_ENV, *(env or [])]
        self.timeout = _positive_int("timeout", timeout)
        self.max_output = _positive_int("max_output", max_output)
        if workspace is None:
            if backend != "local":
                raise ValueError(f"unknown exec backend {backend!r} (available: local)")
            workspace = LocalWorkspace()
        self.workspace = workspace

    async def tools(self) -> list[dict]:
        allowed = (
            "Only these programs may run: " + ", ".join(self.commands) + ". "
            if self.commands else ""
        )
        return [
            {
                "name": "exec.run",
                "description": (
                    "Run one command in your workspace and get its exit code and output. "
                    "Pass the command as a list of arguments, not a shell string (no pipes, "
                    "redirects or globs). " + allowed
                    + f"It runs in `cwd` (default: {self.roots[0]}), which must be under the "
                    f"allowed roots, for at most {self.timeout}s. Long output keeps its "
                    f"start and end. Isolation: {self.workspace.isolation}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": 'e.g. ["python", "-m", "pytest", "-q"]',
                        },
                        "cwd": {"type": "string", "description": "Working directory"},
                        "timeout": {
                            "type": "integer",
                            "description": f"Seconds, at most {self.timeout}",
                        },
                    },
                    "required": ["command"],
                },
            }
        ]

    def _cwd(self, raw: str | None) -> Path:
        cwd = Path(raw).resolve() if raw else self.roots[0]
        if not any(cwd.is_relative_to(root) for root in self.roots):
            allowed = ", ".join(str(root) for root in self.roots)
            raise PermissionError(f"cwd {raw!r} is outside the allowed roots ({allowed})")
        if not cwd.is_dir():
            raise ValueError(f"cwd {str(cwd)!r} is not a directory")
        return cwd

    async def call(self, name: str, payload: dict) -> str:
        if name != "exec.run":
            raise ValueError(f"unknown tool {name!r}")
        command = payload.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) for arg in command)
        ):
            raise ValueError("command must be a non-empty list of strings")
        if self.commands and command[0] not in self.commands:
            raise PermissionError(
                f"{command[0]!r} is not an allowed program (allowed: {', '.join(self.commands)})"
            )
        cwd = self._cwd(payload.get("cwd"))
        timeout = self.timeout
        if payload.get("timeout") is not None:
            timeout = min(_positive_int("timeout", payload["timeout"]), self.timeout)
        env = {k: os.environ[k] for k in self.env_names if k in os.environ}
        try:
            result = await self.workspace.run(
                command, cwd=cwd, env=env, timeout=timeout, max_output=self.max_output
            )
        except FileNotFoundError:
            raise ValueError(f"program not found: {command[0]!r}") from None
        if result.timed_out:
            status = f"timed out after {timeout}s; the process group was killed"
        else:
            status = f"exit {result.returncode}"
        return (
            f"{status} ({result.seconds:.1f}s)\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
