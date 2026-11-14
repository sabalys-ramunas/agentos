"""`agentos engage digest`: one day per account, for the owner on the node (agentos.engage.digest).

    agentos engage digest [biz-a/instagram] [--day 2026-11-04] [--json]

Without a day it shows yesterday in each account's time zone. The text is the same data as the
JSON (and as the engage_digest tool). Draft text is never printed: `agentos engage drafts` shows
the drafts themselves.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer


def digest(surface: Annotated[str | None, typer.Argument(help="One account, or leave out for all.")] = None,
           day: Annotated[str | None, typer.Option("--day", help="The day as YYYY-MM-DD (default: yesterday).")]
           = None,
           as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")] = False) -> None:
    """One day of engagement per account: sessions, operations, drafts waiting, holds and caps."""
    from agentos.engage import digest as dg
    from agentos.engage.cli import _service

    with _service() as svc:
        out = dg.digest(svc, surface, dg.parse_day(day))
    if as_json:
        typer.echo(json.dumps(out, indent=2, default=str))
        return
    if not out["surfaces"]:
        typer.echo("No engagement programs yet.")
        return
    typer.echo("\n\n".join(dg.render_text(r) for r in out["surfaces"]))


def register(app: typer.Typer) -> None:
    app.command("digest")(digest)
