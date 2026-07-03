"""daedalus CLI — create, run, chat."""

from __future__ import annotations

import asyncio

import click

import daedalus
from daedalus.agent import Agent
from daedalus.bundle import BundleError
from daedalus.llm import LLMError
from daedalus.scaffold import create_bundle
from daedalus.task import TaskState
from daedalus.transports.cli import CliTransport, run_once


@click.group()
def main() -> None:
    """Agent construction kit — an agent is a directory."""


@main.command()
def version() -> None:
    click.echo(daedalus.__version__)


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


def _load(bundle_path: str) -> Agent:
    try:
        return Agent.load(bundle_path)
    except BundleError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.argument("bundle_path")
@click.option("-m", "--message", default=None, help="One-shot: send MESSAGE, print the reply.")
def run(bundle_path: str, message: str | None) -> None:
    """Serve the bundle's declared transports, or one-shot with -m."""
    agent = _load(bundle_path)
    if message is not None:
        try:
            task = asyncio.run(run_once(agent, message))
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
def chat(bundle_path: str) -> None:
    """Interactive chat with the bundle, regardless of its declared transports."""
    agent = _load(bundle_path)
    try:
        asyncio.run(CliTransport().serve(agent))
    except KeyboardInterrupt:
        pass
