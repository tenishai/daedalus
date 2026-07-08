"""daedalus CLI — create, run, chat."""

from __future__ import annotations

import asyncio
import json

import click

import daedalus
from daedalus.agent import Agent
from daedalus.bundle import BundleError
from daedalus.llm import LLMError
from daedalus.scaffold import create_bundle
from daedalus.task import TaskState
from daedalus.transports.cli import CliApprover, CliTransport, run_once


@click.group()
def main() -> None:
    """Agent construction kit — an agent is a directory."""


@main.command()
def version() -> None:
    click.echo(daedalus.__version__)


@main.command()
@click.option("--out", default=None, help="Write to a file instead of stdout.")
def schema(out: str | None) -> None:
    """Print the canonical agent.yaml JSON Schema (the published bundle shape)."""
    from pathlib import Path

    from daedalus.bundle import bundle_schema

    text = json.dumps(bundle_schema(), indent=2) + "\n"
    if out:
        Path(out).write_text(text)
        click.echo(f"wrote {out}")
    else:
        click.echo(text, nl=False)


@main.command()
@click.argument("name")
@click.option("--dir", "parent", default=".", help="Parent directory for the new bundle.")
def create(name: str, parent: str) -> None:
    """Scaffold a new agent bundle."""
    try:
        root = create_bundle(name, parent)
    except FileExistsError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"created {root}/")
    click.echo(f"  edit {root}/persona.md, then: daedalus chat {root}")


def _load(bundle_path: str, **overrides) -> Agent:
    try:
        return Agent.load(bundle_path, **overrides)
    except BundleError as exc:
        raise click.ClickException(str(exc)) from exc


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _print_event(event: dict) -> None:
    kind = event["type"]
    if kind == "tools_offered":
        names = ", ".join(event["names"]) or "none (empty allowlist or no provider)"
        click.echo(f"  [tools] offering: {names}")
    elif kind == "tool_call":
        click.echo(f"  [tool] → {event['name']} {_short(json.dumps(event['arguments']))}")
    elif kind == "tool_result":
        click.echo(f"  [tool] ← {_short(event['content'])}")
    elif kind == "reasoning":
        click.echo(f"  [think] {_short(event['text'])}")
    elif kind == "salvage":
        click.echo(f"  [salvage] recovered {event['count']} text-leaked tool call(s)")
    elif kind == "recall":
        click.echo(f"  [memory] recalled {event['count']} item(s)")
    elif kind == "budget":
        click.echo(f"  [budget] -{event['spent']} tokens, {event['remaining']} remaining")


@main.command()
@click.argument("bundle_path")
@click.option("-m", "--message", default=None, help="One-shot: send MESSAGE, print the reply.")
@click.option("-v", "--verbose", is_flag=True, help="Trace tool calls, results, and reasoning.")
def run(bundle_path: str, message: str | None, verbose: bool) -> None:
    """Serve the bundle's declared transports, or one-shot with -m."""
    agent = _load(bundle_path, on_event=_print_event if verbose else None)
    if message is not None:

        async def one_shot():
            try:
                return await run_once(agent, message)
            finally:
                await agent.aclose()

        try:
            task = asyncio.run(one_shot())
        except LLMError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(task.agent_text())
        if task.state is TaskState.FAILED:
            raise SystemExit(1)
        return
    try:
        asyncio.run(agent.serve())
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        pass


@main.command()
@click.argument("bundle_path")
@click.option("-v", "--verbose", is_flag=True, help="Trace tool calls, results, and reasoning.")
def chat(bundle_path: str, verbose: bool) -> None:
    """Interactive chat with the bundle, regardless of its declared transports.

    A human is present, so tier-2 gates prompt here; headless `run` keeps
    the fail-closed DenyAll default.
    """
    agent = _load(
        bundle_path,
        approver=CliApprover(),
        on_event=_print_event if verbose else None,
    )

    async def chat_session():
        try:
            await CliTransport().serve(agent)
        finally:
            await agent.aclose()

    try:
        asyncio.run(chat_session())
    except KeyboardInterrupt:
        pass
