"""`agentos` command: the root, which registers every command group (agentos.commands, the
feature packages' CLIs) and then regroups the top level (agentos.cli_groups).

Lane commands talk to the store through a LaneManager directly, without the
daemon, so they work before `agentos node serve` runs. The lane control commands
(take, handback, stop, deny, view) are the exception: control lives in the running
daemon, so they call its HTTP API with the node token. Heavy modules (the lane
manager, uvicorn, the MCP server, httpx) are imported only by the commands that need them.
The helpers they share are in agentos.commands.base.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from agentos import __version__, lanebench_cli
from agentos.commands.base import cli_runtime, fail, plugins_cli_runtime
from agentos.commands.devices import device_app
from agentos.commands.features import mount_features, wire_feature_clis
from agentos.commands.members import members_app
from agentos.commands.node import config_app, node_app
from agentos.commands.proxy import proxy_app
from agentos.core import catalog
from agentos.core.config import NodeConfig
from agentos.dev import cli as dev_cli
from agentos.studio import cli as studio_cli

# isort: off
# `agentos lane` lists its commands in the order they are made: lanes, then lane_sites, then lane_control
from agentos.commands.lanes import lane_app
from agentos.commands import lane_sites, lane_control  # noqa: F401  (their commands join lane_app)
# isort: on

# feature CLIs (a plugin's among them) and tests read these here
__all__ = ["app", "cli_runtime", "plugins_cli_runtime"]

# Tracebacks never print local variables: they would include the MCP token and proxy passwords.
app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False,
                  help="agentos: a self-hosted computer for your agents.")
app.add_typer(node_app, name="node")
app.add_typer(proxy_app, name="proxy")
app.add_typer(lane_app, name="lane")
app.add_typer(device_app, name="device")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(version: Annotated[bool, typer.Option("--version", help="Print the agentos version and exit.",
                                               callback=_print_version, is_eager=True)] = False) -> None:
    """agentos: a self-hosted computer for your agents."""


@app.command()
def version() -> None:
    """Print the agentos version."""
    typer.echo(__version__)


@app.command()
def tools(
    markdown: Annotated[bool, typer.Option("--markdown", help="The full reference as Markdown.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable, one object per tool.")] = False,
) -> None:
    """The MCP tools this build serves, listed from the same definitions the server uses."""
    import json
    import tempfile

    from agentos.core.runtime import Runtime
    from agentos.mcp.providers import feature_providers, tool_providers
    from agentos.mcp.reference import reference_markdown, reference_rows
    from agentos.plugins.entries import PLUGINS

    runtime = Runtime(config=NodeConfig(home=Path(tempfile.mkdtemp(prefix="agentos-tools-"))))
    from agentos.core.tools import ToolSpec

    # a plugin's tools are served only while it is on, and this build has them whatever a node chose
    providers = [*tool_providers(), *feature_providers(tuple(m for plugin in PLUGINS for m in plugin.tools))]
    # providers may also return MCP prompts (PromptSpec); the reference lists the tools
    specs = [spec for provider in providers for spec in provider(runtime) if isinstance(spec, ToolSpec)]
    if markdown:
        typer.echo(reference_markdown(specs), nl=False)
    elif as_json:
        typer.echo(json.dumps(reference_rows(specs), indent=2))
    else:
        for row in reference_rows(specs):
            summary = " ".join(row["description"].split()).split(". ")[0].rstrip(".")
            typer.echo(f"{row['name']:<24} {row['effect']:<15} {summary[:90]}")


@app.command()
def errors(
    code: Annotated[str | None, typer.Argument(help="Show one code (for example busy).")] = None,
    markdown: Annotated[bool, typer.Option("--markdown", help="The full catalog as Markdown text.")]
    = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable, one object per code.")] = False,
) -> None:
    """Every error code: what it means, what an agent should do, and how to fix it."""
    import dataclasses
    import json

    if code is not None:
        found = catalog.entry(code)
        if found is None:
            raise fail(f"no error code {code!r}; `agentos errors` lists them")
        if as_json:
            typer.echo(json.dumps(dataclasses.asdict(found), indent=2))
            return
        typer.echo(f"{found.code}: {found.meaning}")
        typer.echo(f"  agent: {found.agent}")
        typer.echo(f"  fix:   {found.fix}")
        typer.echo(f"  retry: {found.retry_text()}")
        return
    if markdown:
        typer.echo(catalog.markdown(catalog.scan()), nl=False)
    elif as_json:
        typer.echo(json.dumps([dataclasses.asdict(e) for _, e in sorted(catalog.entries().items())], indent=2))
    else:
        for item in sorted(catalog.entries().values(), key=lambda e: e.code):
            typer.echo(f"{item.code:<26} {item.meaning[:90]}")


SETUP_PATHS = (
    ("Try it on this computer first, with a fake node (nothing to install)", "agentos dev up --seed"),
    ("On a Linux server you reach over SSH (agentos installs the node there)", "agentos connect root@my-server"),
    ("On this Linux machine (it becomes the node; not a Mac yet)", "agentos up --here"),
)


# `agentos setup` is here, not in agentos.client.cli: it starts a controller or a dev node, and both
# of those CLIs build on the client's
@app.command()
def setup() -> None:
    """Get started: choose where your agents' browsers run, and connect Claude Code and Codex to it."""
    from agentos.client.cli import connect
    from agentos.client.cli import fail as client_fail
    from agentos.client.errors import ClientError
    from agentos.client.state import current_platform

    typer.echo("agentos: where should your agents' logged-in browsers run?")
    for i, (what, command) in enumerate(SETUP_PATHS, 1):
        typer.echo(f"  {i}  {what}")
        typer.echo(f"       {command}")
    if not sys.stdin.isatty():
        typer.echo("Run one of those commands to start.")
        return
    choice = typer.prompt("Choose 1, 2 or 3", default="1", show_default=True).strip()
    if choice == "2":
        host = typer.prompt("The server's SSH address, for example root@203.0.113.7").strip()
        connect(ssh_host=host)
        return
    if choice == "3":
        if current_platform() == "macos":
            typer.echo("A Mac cannot be a node yet (browser lanes need Linux). Try 1 first: agentos dev up --seed")
            raise typer.Exit(1)
        from agentos.controller.cli import up_command

        up_command(controller_port=None, ssh_host=None, here=True, verbose=False)
        return
    if choice != "1":
        raise client_fail(ClientError(f"{choice!r} is not 1, 2 or 3", "run `agentos setup` again"))
    from agentos.dev.cli import DEFAULT_PORT, up

    up(home=None, port=DEFAULT_PORT, seed=True, connect=True)


app.add_typer(config_app, name="config")
app.add_typer(members_app, name="members")

# Each feature package ships a typer `app`, mounted when installed: a package missing from this
# build simply has no commands.
mount_features(app)
wire_feature_clis()
app.add_typer(dev_cli.app, name="dev")  # agentos dev up: a fake node on this machine (agentos.dev)
app.add_typer(studio_cli.app, name="studio")  # agentos studio up: the studio tools with a real browser, on a laptop
app.add_typer(lanebench_cli.app, name="lanebench")  # the local lane benchmark (tests/lanebench)

try:  # agentos up: a controller and this node, joined, in one process
    from agentos.controller.cli import up_command as _up_command
except ModuleNotFoundError as _exc:
    if not (_exc.name or "").startswith("agentos.controller"):
        raise
else:
    app.command("up")(_up_command)

try:  # doctor, backup, migrate, upgrade, health, and `lane refresh-fingerprint`
    from agentos.ops import cli as _ops_cli
except ModuleNotFoundError as _exc:
    if not (_exc.name or "").startswith("agentos.ops"):
        raise
else:
    _ops_cli.mount(app, lane_group=lane_app)

# The task-first top level (about 14 groups); every old name stays a hidden alias (agentos.cli_groups).
from agentos.cli_groups import regroup as _regroup

_regroup(app)

if __name__ == "__main__":
    app()
