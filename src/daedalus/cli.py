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
@click.argument("name", required=False)
def guide(name: str | None) -> None:
    """List the bundled guides, or print one as markdown (start with: guide welcome)."""
    from daedalus.docs import MANIFEST, read_guide

    if not name:
        click.echo("Bundled guides (daedalus guide <name>):\n")
        width = max(len(n) for n in MANIFEST)
        for n, line in MANIFEST.items():
            click.echo(f"  {n:<{width}}  {line}")
        return
    try:
        click.echo(read_guide(name), nl=False)
    except KeyError:
        raise click.ClickException(
            f"no bundled guide named {name!r}; choose one of: {', '.join(MANIFEST)}"
        ) from None


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


_ENGINE_HELP = (
    "What runs the model and agent loop: litellm (default; daedalus's own loop, any "
    "model LiteLLM routes) or claude-code (Claude Code is the agent; needs Claude Code "
    "and the claude-code extra). Also DAEDALUS_ENGINE."
)


def _load(bundle_path: str, **overrides) -> Agent:
    try:
        return Agent.load(bundle_path, **overrides)
    except BundleError as exc:
        raise click.ClickException(str(exc)) from exc


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


#: The six operator-gate events, named exactly rather than matched by suffix.
#:
#: A SUFFIX TEST WAS TOO LOOSE AND SWALLOWED TWO STRANGERS (#49 r2 pre-pass).
#: `kind.endswith(("_allowed", "_refused"))` also caught
#: `system_task_refused` (carries `marker`) and a SECOND, pre-existing
#: `tool_refused` at agent.py:557 (carries `error`) -- neither is an operator
#: gate and neither has a `sub`, so both rendered as `for sub=None`. A looser
#: match placed after exact-match arms inherits everything they did not claim.
#:
#: THE `sub` TEST IS NOT BELT-AND-BRACES. `tool_refused` is emitted from TWO
#: call sites with different payloads -- the operator gate's (agent.py:171)
#: and the tool-execution failure at :557 -- so the NAME alone cannot tell
#: them apart. That collision is pre-existing and out of scope here; keying on
#: the field that only one of them carries is what makes this branch correct
#: without renaming an event the kaminos pane already consumes.
_OPERATOR_GATE_EVENTS = frozenset(
    f"{lane}_{decision}"
    for lane in ("teach", "knowledge", "tool")
    for decision in ("allowed", "refused")
)


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
    elif kind == "bare_tool_name":
        click.echo(f"  [tools] model sent {event['given']!r}; matched to {event['resolved']}")
    elif kind == "model_resolved":
        if event.get("source") != "id":
            fell = f", fell back from {event['tier']}" if event.get("fell_back") else ""
            used = event.get("used") or event["tier"]
            click.echo(f"  [model] tier {used} -> {event['engine']} {event['id']}{fell}")
        else:
            click.echo(f"  [model] {event['engine']} {event['id']} (pinned)")
    elif kind == "recall":
        click.echo(f"  [memory] recalled {event['count']} item(s)")
    elif kind == "budget":
        click.echo(f"  [budget] -{event['spent']} tokens, {event['remaining']} remaining")
    elif kind == "truncated":
        click.echo(f"  [truncated] reply cut by max_tokens ({event['max_tokens']})")
    elif kind in _OPERATOR_GATE_EVENTS and "sub" in event:
        # THE OPERATOR GATE, RENDERED, because otherwise it is not observable
        # at all (#49 r2). `docker/entrypoint.sh` runs with -v, so this
        # function is live in every pod -- but it was a closed if/elif chain
        # that printed NOTHING for teach/knowledge/tool allowed-or-refused.
        # The sub survived only in a 100-event in-memory ring and a live SSE
        # attachment, so daedalus#47's step 1 ("read the logs and enumerate
        # what real callers present") had nothing to read. The one line that
        # makes that plan executable belongs in this diff, not in the issue.
        lane, _, decision = kind.rpartition("_")
        click.echo(f"  [{lane}] {decision} for sub={event['sub']}")


def _usage_line(event: dict) -> str | None:
    """The one-line usage/cost summary for an `engine_finished` event, in the
    `[budget]` style (see `_print_event` above) -- but printed every run, not only
    under -v (daedalus#135): the Fates run `daedalus` in `--rm` containers, so this
    line is the only record of what a run cost once the container is gone.

    Tokens lead because they are what's actually measured; `total_cost_usd` is the
    SDK's list-price estimate, and on a subscription (CLAUDE_CODE_OAUTH_TOKEN)
    nothing was billed per token, so it's labelled an estimate rather than a cost.

    Returns None when there's nothing worth reporting (no token counts, no turns,
    no cost) rather than printing zeros that were never measured.
    """
    models = ", ".join(event.get("models") or []) or "(model unknown)"
    parts = []
    if "input_tokens" in event:
        # Anthropic's input_tokens counts only what follows the last cache breakpoint;
        # the input a run actually sent is all three counters together (prompt-caching
        # docs: total = cache_read + cache_creation + input_tokens). A cached Opus
        # review is mostly cache reads, so leading with input_tokens alone understated
        # it by two orders of magnitude.
        read, write = event["cache_read_input_tokens"], event["cache_creation_input_tokens"]
        uncached = event["input_tokens"]
        parts.append(
            f"{read + write + uncached:,} tokens in "
            f"({read:,} cache read, {write:,} cache write, {uncached:,} uncached), "
            f"{event['output_tokens']:,} out"
        )
    if event.get("turns") is not None:
        turns = event["turns"]
        parts.append(f"{turns} turn" if turns == 1 else f"{turns} turns")
    if event.get("total_cost_usd") is not None:
        parts.append(f"~${event['total_cost_usd']:.2f} at list price")
    if not parts:
        return None
    return f"[usage] {event['engine']} {models}: " + ", ".join(parts)


def _print_usage(event: dict) -> None:
    """Unconditional counterpart to `_print_event`: wired into `daedalus run`
    regardless of -v, on stderr, so a launcher that only captures stderr still sees
    it (daedalus#135)."""
    if event["type"] != "engine_finished":
        return
    line = _usage_line(event)
    if line:
        click.echo(line, err=True)


@main.command()
@click.argument("bundle_path")
@click.option("-m", "--message", default=None, help="One-shot: send MESSAGE, print the reply.")
@click.option("-v", "--verbose", is_flag=True, help="Trace tool calls, results, and reasoning.")
@click.option("--engine", default=None, help=_ENGINE_HELP)
@click.option(
    "--recall",
    default=None,
    help="With -m: recall personal memory on this text instead of MESSAGE.",
)
def run(
    bundle_path: str, message: str | None, verbose: bool, engine: str | None, recall: str | None
) -> None:
    """Serve the bundle's declared transports, or one-shot with -m."""
    if recall is not None and message is None:
        raise click.UsageError("--recall applies to a one-shot run; pass -m too.")

    def on_event(event: dict) -> None:
        if verbose:
            _print_event(event)
        _print_usage(event)  # always, even without -v: see _print_usage

    agent = _load(bundle_path, on_event=on_event, engine=engine)
    if message is not None:

        async def one_shot():
            try:
                return await run_once(agent, message, recall=recall)
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
@click.option("--engine", default=None, help=_ENGINE_HELP)
def chat(bundle_path: str, verbose: bool, engine: str | None) -> None:
    """Interactive chat with the bundle, regardless of its declared transports.

    A human is present, so tier-2 gates prompt here; headless `run` keeps
    the fail-closed DenyAll default.
    """
    agent = _load(
        bundle_path,
        approver=CliApprover(),
        on_event=_print_event if verbose else None,
        engine=engine,
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
