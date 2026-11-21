"""`openajar panel ...`, mounted by the lead in openajar.cli."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from openajar.api.auth import CODE_TTL_SECONDS, create_login_code

app = typer.Typer(no_args_is_help=True, help="The web panel.")


@app.callback()
def panel() -> None:
    """The web panel."""


@app.command()
def login(
    base_url: Annotated[str | None, typer.Option(help="Where you open the panel, e.g. http://localhost:7400.")] = None,
    home: Annotated[Path | None, typer.Option(envvar="OPENAJAR_HOME", help="Node data directory.")] = None,
    ttl: Annotated[int, typer.Option(help="Seconds the link stays valid.")] = CODE_TTL_SECONDS,
) -> None:
    """Print a one-time sign-in link for the panel (valid for 10 minutes, works once)."""
    link = create_login_code(home, base_url=base_url, ttl_seconds=ttl)
    minutes = max(1, ttl // 60)
    typer.echo(f"Open this link within {minutes} minutes. It works once:\n\n  {link.url}\n")
    typer.echo("From your computer, reach the node first: ssh -N -L 7400:localhost:7400 <your node>")
