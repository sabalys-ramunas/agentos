"""`openajar web`: the web app the local service serves at http://127.0.0.1:7410/.

  openajar web status    what `/` serves, and whether the installed copy matches this openajar
  openajar web install   download this release's web app, check its sha256, unpack it under the home
  openajar web update    the same, only when it is wanted and not current (the installer runs it)
  openajar web remove    take it away; upgrades leave it away until `openajar web install`

The steps are in openajar.setup.webapp; `openajar setup web` asks the same question in the setup.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from openajar.core import webapp

app = typer.Typer(no_args_is_help=True, help=(
    "The web app at http://127.0.0.1:7410/: a prebuilt copy for this release, optional. "
    "Install, update, remove, status."))

HomeOption = Annotated[Path | None, typer.Option("--home", help="Where openajar keeps its files (default: ~/.openajar).")]


def _home(home: Path | None) -> Path:
    from openajar.setup.cli import resolve_home

    return resolve_home(home)


def describe(st: webapp.WebStatus) -> str:
    """One line: what `/` serves now."""
    if st.source == "env":
        return f"served from OPENAJAR_PANEL_DIR ({st.path})"
    if st.source == "checkout":
        return f"served from this checkout's own build ({st.path})"
    if st.source == "installed":
        if st.mismatched:
            return (f"version {st.installed} is installed, but this openajar is {st.expected}: "
                    "openajar web install brings it to match")
        return f"version {st.installed}, installed in {st.path}"
    if st.wanted is False:
        return "not installed (you said no): openajar web install adds it"
    return "not installed: openajar web install adds it"


@app.command("status")
def status(home: HomeOption = None) -> None:
    """What the local service serves at /, and whether it matches this openajar."""
    st = webapp.status(_home(home))
    typer.echo(f"Web app: {describe(st)}")
    raise typer.Exit(1 if st.mismatched else 0)


def _run_install(where: Path, version: str | None) -> None:
    from openajar.setup import webapp as installer

    try:
        got = installer.install(where, version)
    except (installer.WebAppMissing, installer.WebAppRefused) as e:
        typer.secho(f"error: {e.message}", err=True, fg=typer.colors.RED)
        typer.echo("  The node works without it. Try again later with `openajar web install`.", err=True)
        raise typer.Exit(1) from None
    from openajar.setup.webstep import app_url

    typer.echo(f"Installed the web app {got.version} ({got.files} files, sha256 {got.sha256[:12]}).")
    typer.echo(f"Open {app_url(where)}")


@app.command("install")
def install(home: HomeOption = None,
            version: Annotated[str | None, typer.Option(
                help="Another release's app (default: this openajar's own version).")] = None) -> None:
    """Download this release's web app from GitHub, check its sha256, and unpack it under the home."""
    _run_install(_home(home), version)


@app.command("update")
def update(home: HomeOption = None) -> None:
    """Install or update the web app to match this openajar, unless you said no or OPENAJAR_NO_WEB=1."""
    from openajar.setup import webapp as installer

    where = _home(home)
    if os.environ.get("OPENAJAR_NO_WEB") == "1":
        typer.echo("Web app: skipped (OPENAJAR_NO_WEB=1).")
        return
    try:
        what, got = installer.update(where)
    except (installer.WebAppMissing, installer.WebAppRefused) as e:
        typer.echo(f"Web app: not updated ({e.message}). The node works without it; "
                   "`openajar web install` tries again.")
        raise typer.Exit(1) from None
    if what == "declined":
        typer.echo("Web app: not installed (you said no). `openajar web install` adds it.")
    elif what == "current":
        typer.echo(f"Web app: {webapp.status(where).expected} is current.")
    elif got is not None:
        typer.echo(f"Web app: installed {got.version}.")


@app.command("remove")
def remove(home: HomeOption = None) -> None:
    """Remove the installed web app. The node, MCP and live views keep working."""
    from openajar.setup import webapp as installer

    had = installer.remove(_home(home))
    typer.echo("Removed the web app." if had else "No web app was installed.")
    typer.echo("Updates leave it away. `openajar web install` adds it again.")
