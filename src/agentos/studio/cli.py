"""`agentos studio`: the studio tools on this computer. Imported by agentos.cli for every command,
so the server itself (agentos.studio.local) is imported only when `up` runs.

`agentos studio profile ...` is `agentos profile ...` on the profiles studio keeps on this
computer (~/.agentos/studio/node), so the owner approves what an agent proposed without setting
AGENTOS_HOME by hand: `agentos studio profile approve personal/shop --version 2`."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer

from agentos.profiles import cli as profiles_cli

app = typer.Typer(no_args_is_help=True, help="Studio on this computer: check the pages you build, with a real browser.")


@app.command("up")
def up(
    port: Annotated[int, typer.Option(help="MCP port on 127.0.0.1.", min=1, max=65535)] = 7460,
    skill: Annotated[bool, typer.Option("--skill/--no-skill",
                                        help="Put the agentos-studio skill where Claude Code finds it.")] = True,
) -> None:
    """Run the studio tools (ui_check, ui_diff, system_check, ...) on this computer, with a real browser.

    Keeps its captures and runs in ~/.agentos/studio/, puts the agentos-studio skill in Claude
    Code's skills folder when Claude Code is here, and prints the line that connects Claude Code.
    There are no browser lanes here: lane tools answer unavailable."""
    from agentos.dev.cli import port_is_free
    from agentos.studio.local import instance_path, run, running

    other = running()
    if other is not None:
        # Two studios on one home would share its store and revoke each other's agent token.
        typer.secho(f"error: a studio already runs from this home (pid {other.get('pid')}, port {other.get('port')}); "
                    f"its connect line is in {instance_path()}. Use it, stop it (kill {other.get('pid')}), or run "
                    "this one with another HOME.", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)
    if not port_is_free(port):
        typer.secho(f"error: port {port} is busy; pick another with --port", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)
    try:
        asyncio.run(run(port=port, announce=typer.echo, skill=skill))
    except KeyboardInterrupt:
        pass


def studio_node_home() -> Path:
    """Where `agentos studio up` keeps its node files (agentos.studio.local.node_home)."""
    return Path.home() / ".agentos" / "studio" / "node"


profile_app = typer.Typer(no_args_is_help=True,
                          help="Profiles on this computer's studio: list, show, approve or reject what an agent proposed.")


@profile_app.callback()
def _studio_profiles() -> None:
    """The same commands as `agentos profile`, on the profiles `agentos studio up` keeps here."""
    os.environ["AGENTOS_HOME"] = str(studio_node_home())


profile_app.registered_commands = list(profiles_cli.app.registered_commands)
app.add_typer(profile_app, name="profile")
