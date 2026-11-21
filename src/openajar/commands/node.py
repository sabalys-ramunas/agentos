"""`openajar node` (init, rotate-token, serve, join) and `openajar config` (show, example)."""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Annotated

import typer

from openajar.commands.base import (
    GRANT_FOR_AGENTS,
    connect_lines,
    fail,
    init_node,
    initialized_config,
    missing_module,
    read_config,
)
from openajar.core.config import (
    NodeConfig,
    config_path,
    home_from_env,
    save_config,
)
from openajar.ops.restart import restart_command
from openajar.ops.templates import templates_command

node_app = typer.Typer(no_args_is_help=True, help="Set up and run this node.")


config_app = typer.Typer(no_args_is_help=True, help="This node's settings.")


@config_app.command("show")
def config_show(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable.")] = False,
) -> None:
    """Every setting with its resolved value and where it came from: env, node.toml or the default.
    Secrets are never printed."""
    import json

    from openajar.core.config_report import config_report, render

    home = home_from_env()
    cfg = read_config(home) if config_path(home).exists() else NodeConfig(home=home)
    rows = config_report(cfg, home)
    if as_json:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return
    typer.echo(f"node.toml: {config_path(home)}{'' if config_path(home).exists() else ' (missing: defaults shown)'}")
    for line in render(rows):
        typer.echo(line)


@config_app.command("example")
def config_example() -> None:
    """A commented node.toml with every setting, what it means and its default."""
    from openajar.core.config_report import example

    for line in example():
        typer.echo(line)


@node_app.command("init")
def node_init(
    name: Annotated[str | None, typer.Option(help="Node name shown by whoami.")] = None,
    mcp_port: Annotated[int | None, typer.Option(help="MCP port on 127.0.0.1.", min=1, max=65535)] = None,
    display_base: Annotated[int | None, typer.Option(help="First X display number for lanes.", min=1)] = None,
    vnc_base: Annotated[int | None, typer.Option(help="Unused: x11vnc listens on a private unix socket. Accepted so old scripts still run.", min=1, max=65535)] = None,
    viewer_base: Annotated[int | None, typer.Option(help="First noVNC viewer port for lanes.",
                                                    min=1, max=65535)] = None,
    max_warm_lanes: Annotated[int | None, typer.Option(help="Browsers kept running at once.", min=1)] = None,
    new_token: Annotated[bool, typer.Option("--new-token", help="Replace the MCP token.")] = False,
    ssh_host: Annotated[str | None, typer.Option(help="SSH alias of this node, for tunnel hints.")] = None,
) -> None:
    """Create OPENAJAR_HOME, write node.toml with an MCP token, and print how to connect.

    Safe to run again: an existing node.toml keeps its token unless --new-token is given.
    """
    home = home_from_env()
    existed = config_path(home).exists()
    overrides = {
        "name": name,
        "mcp_port": mcp_port,
        "display_base": display_base,
        "vnc_base": vnc_base,
        "viewer_base": viewer_base,
        "max_warm_lanes": max_warm_lanes,
        "ssh_host": ssh_host,
    }
    cfg, path = init_node(home, overrides, new_token=new_token)

    typer.echo(f"{'Updated' if existed else 'Created'} {path}")
    typer.echo("")
    from openajar.node.tools import ssh_host as default_ssh_host
    host = default_ssh_host(cfg)
    typer.echo("Connect from your Mac (keeps a tunnel up, keeps the token in the Keychain, registers")
    typer.echo("the server with Claude Code):")
    typer.echo(f"  openajar connect {host}")
    typer.echo("")
    typer.echo("Or by hand. Keep the tunnel open in one terminal:")
    tunnel, add = connect_lines(cfg, host)
    typer.echo(f"  {tunnel}")
    typer.echo("Give your agents their own grant here on the node (it prints the token once):")
    typer.echo(f"  {GRANT_FOR_AGENTS}")
    typer.echo("Then register the server once with that token:")
    typer.echo(f"  {add}")
    typer.echo("Keep the node token in node.toml to yourself: a token never approves what its own agents asked.")


@node_app.command("rotate-token")
def node_rotate_token() -> None:
    """Replace the node's own token (node.toml mcp_token), so copies of the old one stop working.

    The node token is the owner's: whoever holds it can do everything, issue grants included.
    `openajar connect` reads it over SSH for a few calls and never stores it; connections made
    before that held it on the computer. Rotate it once every connection has its own grant.
    """
    from openajar.api.auth import PanelStore

    cfg = initialized_config()
    cfg = cfg.model_copy(update={"mcp_token": secrets.token_urlsafe(32)})
    path = save_config(cfg)
    typer.echo(f"Made a new node token in {path}.")
    ended = PanelStore.for_home(cfg.home).delete_device_sessions()  # whoever held the old token could mint these
    if ended:
        typer.echo(f"Ended {ended} saved computer credential{'s' if ended != 1 else ''} the old token made. On each "
                   "computer, run `openajar connect --upgrade` for a new one.")
    typer.echo("The old one works until agentosd restarts. Restart it now:")
    typer.echo("  openajar node restart     (it names the open lanes and asks first if others hold any)")
    typer.echo("Connections with their own agent grant keep working. One that still holds the node token")
    typer.echo("stops: on that computer, run `openajar connect --upgrade` (it reads the new token over SSH).")


@node_app.command("serve")
def node_serve(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Run agentosd: the lanes and the MCP server (systemd runs this)."""
    from openajar.core import logs
    from openajar.node.daemon import install_log_scrubbers, serve

    cfg = initialized_config()
    if not cfg.mcp_token:
        raise fail("node.toml has no mcp_token; run `openajar node init`")
    logs.setup(cfg.home, verbose=verbose)  # JSON to journald or logs/agentosd.jsonl (openajar.core.logs)
    install_log_scrubbers(cfg)  # the vault's values join once the runtime is built
    try:
        asyncio.run(serve(cfg))
    except ModuleNotFoundError as exc:
        raise missing_module(exc) from None
    except Exception:
        # Through the log (scrubbed), not as a raw traceback on stderr: it can quote anything.
        logging.getLogger("openajar.node").exception("agentosd stopped on an unexpected error")
        raise typer.Exit(1) from None


@node_app.command("join")
def node_join(
    controller_url: Annotated[str, typer.Argument(help="The controller, for example wss://controller.example.com "
                                                       "(plain ws:// only to localhost).")],
    join_token: Annotated[str, typer.Argument(help="A join token from `openajar controller join-token` or the cloud.")],
    name: Annotated[str | None, typer.Option(help="This node's name in the controller (default: from node.toml).")]
    = None,
    capability: Annotated[list[str] | None, typer.Option(help="Capabilities to advertise (default browser, "
                                                              "terminal, service).")] = None,
    self_hosted: Annotated[bool, typer.Option("--self-hosted", help="The controller is your own (`openajar "
                                                                     "controller serve`): let it do everything here. "
                                                                     "Without it, a controller may post up to commit, "
                                                                     "use no specials and only watch live views.")]
    = False,
) -> None:
    """Join this node to a controller. agentosd keeps the channel up from its next start."""
    from openajar.node.channel.enroll import EnrollmentFailed, EnrollmentRefused, enroll
    from openajar.policy.ceiling import ControllerCeiling

    cfg = initialized_config()
    previous = cfg.controller_url if cfg.enrolled else None
    ceiling = ControllerCeiling.full() if self_hosted else ControllerCeiling()
    left: dict = {}
    try:
        cfg = asyncio.run(enroll(cfg, controller_url, join_token, name=name, capabilities=capability or None,
                                 ceiling=ceiling, ceiling_by="self_hosted" if self_hosted else "join", left=left))
    except ValueError as exc:
        raise fail(str(exc), code=2) from None
    except EnrollmentRefused as exc:
        hint = {"already_used": "make a new token; each one works once",
                "expired": "make a new token; they last one hour",
                "conflict": "join with another --name, or remove the old node in the controller first"}
        raise fail(f"the controller refused this node ({exc.code}): {exc.message}. "
                   f"{hint.get(exc.code, 'check the token and try again')}") from None
    except EnrollmentFailed as exc:
        raise fail(str(exc)) from None
    if previous and previous != cfg.controller_url:
        typer.echo(f"Left {previous}.")
    typer.echo(f"Joined {cfg.controller_url} as node {cfg.node_id} (org {cfg.org_id}).")
    if left:
        typer.echo(f"Left {left['issuer']}: its grants no longer work here and the {len(left['standing'])} "
                   "standing approval(s) it sent were revoked.")
    shown = ceiling.report()
    typer.echo(f"Controller ceiling: effects up to {shown['max_effect']}, specials "
               f"{', '.join(shown['specials']) or 'none'}, streams {', '.join(shown['streams'])}. "
               "Change it with the controller_* settings in node.toml (`openajar config example`).")
    typer.echo("Restart agentosd to connect (`openajar node restart`, or `openajar node serve`).")


node_app.command("templates")(templates_command)
node_app.command("restart")(restart_command)
