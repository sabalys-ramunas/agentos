"""`agentos pilot`: the limits of a run. Runs are in `agentos runs`, and which brain does each job
in `agentos plugins show browser-worker`."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from agentos.core.config import load_config
from agentos.pilot.service import load_settings, settings_path

app = typer.Typer(no_args_is_help=True, help="The pilot: a cheap worker that drives one lane toward one "
                                             "goal (lane_do).")


@app.command("settings")
def show_settings(
    as_json: Annotated[bool, typer.Option("--json", help="Print as JSON.")] = False,
) -> None:
    """Show the limits of a run, and where its other settings live."""
    home = load_config().home
    settings = load_settings(home)
    if as_json:
        typer.echo(json.dumps(settings.model_dump(), indent=2))
        return
    typer.echo(f"Settings: {settings_path(home)}")
    typer.echo(f"Limits: {settings.max_steps} steps, {settings.wall_s:.0f} s, {settings.no_progress} steps without "
               f"progress, ${settings.max_usd:.2f}, {settings.max_escalations} escalations")
    typer.echo("Whether a publish asks a person is set per lane: agentos flows ask-first --account <lane> on|off")
    typer.echo("The worker on one lane: agentos plugins lane <lane> on|off")
    typer.echo("Which brain does each job: agentos plugins show browser-worker")
