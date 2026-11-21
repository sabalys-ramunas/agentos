"""`openajar dev`: a fake node on this machine. Imported by openajar.cli for every command, so the node
itself (openajar.dev.node, the daemon's composition) is imported only when `up` runs."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Annotated

import typer

DEFAULT_PORT = 7450

app = typer.Typer(no_args_is_help=True,
                  help="A fake openajar node on this machine: no browser, no model, no account.")


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # as uvicorn binds: a connection of the last run still closing (TIME_WAIT) does not make the
        # port busy, a server listening on it does
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def connect_clients(port: int, token: str) -> list[str]:
    """Register this computer's Claude Code and Codex with the fake node (and copy the skills), as
    `openajar connect` does for a real one. The lines to print, or [] when neither is installed."""
    import shutil

    from openajar.client.codex import installed as codex_installed
    from openajar.client.errors import ClientError
    from openajar.client.ops import Client
    from openajar.dev.node import AGENT_SUBJECT

    if not shutil.which("claude") and not codex_installed(Path.home()):
        return []  # nothing to register: store no token anywhere
    try:
        result = Client(out=lambda _: None).connect_dev(port=port, token=token, subject=AGENT_SUBJECT)
    except ClientError as exc:
        return [f"Could not register the clients here: {exc.message}"]
    done = [line for line in result.clients if line.state in ("registered", "already registered", "installed",
                                                                "updated", "already installed")]
    if not any(line.client in ("Claude Code", "Codex") for line in done):
        return []
    lines = ["Connected on this computer (the token stays in this computer's store, not in their config):"]
    lines += [f"  {line.client}: {line.state} {line.detail}".rstrip() for line in result.clients]
    first = "Claude Code" if any(line.client == "Claude Code" for line in done) else "Codex"
    lines += [f'Try it: start a new {first} session and ask "open an openajar environment and run uname -a".',
              "T3 Code: its Claude and Codex threads see openajar-dev too. Start a new thread.",
              "Remove them later with `openajar connect --remove dev`."]
    return lines


def web_source() -> bool:
    """Is the web app's source here to build (a checkout with web/)?"""
    return (Path(__file__).resolve().parents[3] / "web" / "package.json").is_file()


def banner(node, connected: list[str] | None = None) -> str:
    where = "a temporary folder, removed on exit" if node.temporary else str(node.home)
    lines = [
        "openajar dev is running: a fake node on this machine (fake lanes, no model, no account).",
        "",
        f"  home     {where}",
        f"  panel    {node.panel_link}",
        "           (sign in once: the link works once and expires in 10 minutes)",
        f"  MCP      {node.mcp_url}",
        "",
    ]
    if connected:
        lines += [*connected, ""]
    else:
        lines += ["Connect Claude Code with:", f"  {node.claude_add}", ""]
    if not node.panel_built and web_source():
        lines += [
            "The web app is not built yet, so the link shows a placeholder. Build it once with",
            "`pnpm install && pnpm build` in web/, then start openajar dev again.",
            "",
        ]
    elif not node.panel_built:
        lines += [
            "This checkout has no web app, so the link shows a placeholder. Watch an environment",
            "through the live view link the agent gives you, or with",
            "`openajar environments list` and `openajar open --terminal`.",
            "",
        ]
    lines.append("Press Ctrl-C to stop. Every lane closes on the way out.")
    return "\n".join(lines)


@app.command("up")
def up(
    home: Annotated[Path | None, typer.Option(
        help="Keep the node's files here (default: a temporary folder, removed on exit).")] = None,
    port: Annotated[int, typer.Option(help="MCP and panel port on 127.0.0.1.", min=1, max=65535)] = DEFAULT_PORT,
    seed: Annotated[bool, typer.Option("--seed", help="Add two workspaces, four lanes, a saved flow, a captcha, "
                                                      "a pending approval and a question.")] = False,
    connect: Annotated[bool | None, typer.Option(
        "--connect/--no-connect", help="Register Claude Code and Codex on this computer with the fake node "
                                       "(default: only without --home and on the default port, so a second "
                                       "node never writes into their folders).")] = None,
) -> None:
    """Run a fake node on this machine.

    The real policy, vault, runner, brains, workspaces, panel and MCP server, with lanes that show
    fake pages. Nothing starts a browser, calls a model or leaves this machine."""
    from openajar.dev.node import run

    if not port_is_free(port):
        typer.secho(f"error: port {port} is busy; pick another with --port", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)

    register = connect if connect is not None else (home is None and port == DEFAULT_PORT)

    def show(node) -> None:
        connected = connect_clients(node.port, node.agent_token) if register else None
        typer.echo(banner(node, connected))

    try:
        asyncio.run(run(home=home, port=port, seed_lanes=seed, announce=show))
    except KeyboardInterrupt:
        pass
