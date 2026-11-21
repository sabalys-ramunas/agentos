"""`openajar dash`. The lead mounts this with `app.add_typer(openajar.tui.cli.app)`.

Textual and httpx are imported only when the command runs, so `openajar --help` stays fast.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=False, pretty_exceptions_show_locals=False)


@app.command()
def dash(
    url: Annotated[str | None, typer.Option(
        "--url", help="Node URL, for example http://127.0.0.1:7400 (env OPENAJAR_URL).",
        show_default=False)] = None,
    token: Annotated[str | None, typer.Option(
        "--token", help="Bearer token (env OPENAJAR_TOKEN is better: a flag shows up in ps).",
        show_default=False)] = None,
    shots_dir: Annotated[Path | None, typer.Option(
        "--shots-dir", help="Where screenshots and preview images go (default ~/openajar-shots).",
        show_default=False)] = None,
    refresh: Annotated[float, typer.Option(
        "--refresh", min=2.0, help="Seconds between full reloads (live updates arrive in between).")] = 30.0,
    demo: Annotated[bool, typer.Option(
        "--demo", help="Run against a built-in fake node with sample data (no node needed).")] = False,
    node: Annotated[str | None, typer.Option(
        "--node", help="On your computer: the connection to open (default: the only one).",
        show_default=False)] = None,
) -> None:
    """Terminal dashboard: workspaces, lanes, Needs you, activity, runs and brains.

    Finds the node from --url/--token, then OPENAJAR_URL/OPENAJAR_TOKEN, then node.toml in
    OPENAJAR_HOME when run on the node. On your computer it uses the connection `openajar
    connect` made (--node to choose one) with your owner credential from the Keychain, so you
    can approve there what your agents asked.
    """
    from openajar.tui.connect import Connection, resolve

    transport = None
    if demo:
        from openajar.dev.tuidemo import DEMO_TOKEN, FakeNode

        node = FakeNode()
        transport = node.transport()
        connection = Connection(url="http://demo.openajar", token=DEMO_TOKEN, url_source="--demo",
                                token_source="--demo")
    else:
        try:
            connection = resolve(url=url, token=token, node=node, owner=owner_credential)
        except ValueError as exc:
            typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
            raise typer.Exit(2) from None
        if not connection.has_token:
            typer.secho(
                "error: no credential. On your computer run `openajar connect <ssh-host>` first; on the node "
                "set OPENAJAR_HOME (node.toml holds mcp_token); or pass --token or set OPENAJAR_TOKEN.",
                err=True, fg=typer.colors.RED)
            raise typer.Exit(2)

    from openajar.tui.app import DashApp

    DashApp(connection, transport=transport, shots_dir=shots_dir, full_every=refresh).run()


def owner_credential(node: str | None) -> tuple[str, str, str] | None:
    """The panel URL through the tunnel and the owner's device credential of a connection made by
    `openajar connect` on this computer; None when there is none. Raises ValueError (with what to
    do) when --node names nothing, or there are several and none was chosen."""
    try:
        from openajar.client.errors import ClientError
        from openajar.client.ops import Client
    except ModuleNotFoundError:
        return None
    c = Client(out=lambda _line: None)
    try:
        if node is None and not c.connections():
            return None
        conn = c.pick(node)
        return f"http://127.0.0.1:{conn.local_port}", c.owner_device(conn), conn.name
    except ClientError as exc:
        raise ValueError(exc.message + (f". {exc.hint[0].upper()}{exc.hint[1:]}" if exc.hint else "")) from None
