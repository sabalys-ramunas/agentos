"""`openajar controller ...` and `openajar up`.

    openajar controller init          controller.toml with an agent token (safe to run again)
    openajar controller serve         run the controller (agents on /mcp, nodes on /v1/node/connect)
    openajar controller join-token    a single-use join token (1 hour) and the `openajar node join` line
    openajar controller login         a one-time sign-in link for the owner's panel
    openajar controller nodes         enrolled nodes
    openajar controller agent-token   the agent token and how to connect an agent (--new rotates it)
    openajar controller freeze        freeze every node of the mesh (openajar.freeze); unfreeze lifts it
    openajar up                       a controller and a local node on this box, joined, in one process

The commands that read or change state open the controller's SQLite file directly, so they
work whether or not `serve` is running (like `openajar panel login`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
import threading
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from openajar.controller.config import (
    DEFAULT_PORT,
    ControllerConfig,
    config_file,
    controller_home,
    load_controller_config,
    save_controller_config,
)
from openajar.core.config import config_problems, home_from_env

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False,
                  help="The controller: one MCP URL for every node.")


def fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    return typer.Exit(code)


def ensure_config(home=None, **overrides) -> ControllerConfig:
    """Load or create controller.toml; fills in the agent and owner tokens when missing."""
    home = home or controller_home()
    try:
        cfg = load_controller_config(home)
        changed = {k: v for k, v in overrides.items() if v is not None}
        if not cfg.agent_token:
            changed["agent_token"] = "aga_" + secrets.token_urlsafe(32)
        if not cfg.owner_token:
            changed["owner_token"] = secrets.token_urlsafe(32)
        if changed or not config_file(home).exists():
            cfg = ControllerConfig(**{**cfg.model_dump(), **changed})
            save_controller_config(cfg)
    except ValidationError as exc:
        raise fail(f"{config_file(home)} has invalid settings: {config_problems(exc)}") from None
    except ValueError as exc:
        raise fail(f"{config_file(home)}: {exc}") from None
    return cfg


def _controller(cfg: ControllerConfig):
    from openajar.controller.app import Controller

    return Controller(cfg)


def connect_host(ssh_host: str | None) -> str:
    """The SSH alias in a connect line: the flag, else what `openajar node init` and every tunnel hint
    use (OPENAJAR_SSH_HOST, then node.toml, then this machine's hostname)."""
    if ssh_host:
        return ssh_host
    from openajar.core.config import config_path, load_config
    from openajar.node.tools import ssh_host as default_ssh_host

    node_cfg = None
    try:
        if config_path(home_from_env()).exists():
            node_cfg = load_config(home_from_env())
    except Exception:  # noqa: BLE001 - a broken node.toml: the env var or the hostname still answer
        node_cfg = None
    return default_ssh_host(node_cfg)


def connect_lines(cfg: ControllerConfig, ssh_host: str | None = None) -> list[str]:
    host = connect_host(ssh_host)
    return [
        "Connect Claude Code from your Mac (tunnel, Keychain and registration in one step):",
        f"  openajar connect {host} --remote-home {cfg.home} --name {cfg.name}",
        "Or by hand, with a tunnel to this box kept open:",
        f"  ssh -N -L {cfg.port}:localhost:{cfg.port} {host}",
        (f"  claude mcp add --scope user --transport http openajar {cfg.mcp_url} "
         f"--header \"Authorization: Bearer {cfg.agent_token}\""),
    ]


@app.command("init")
def init(
    port: Annotated[int | None, typer.Option(help="Port for agents, nodes and the panel.", min=1, max=65535)] = None,
    host: Annotated[str | None, typer.Option(help="Bind address (loopback unless TLS is configured).")] = None,
    public_url: Annotated[str | None, typer.Option(help="What remote nodes dial, for example "
                                                        "wss://controller.example.com.")] = None,
) -> None:
    """Create or update controller.toml (tokens are kept unless missing)."""
    cfg = ensure_config(port=port, host=host, public_url=public_url)
    typer.echo(f"Controller home {cfg.home} (settings in {config_file(cfg.home)})")


@app.command("serve")
def serve(
    port: Annotated[int | None, typer.Option(help="Override the port in controller.toml.", min=0, max=65535)] = None,
    host: Annotated[str | None, typer.Option(help="Override the bind address.")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Run the controller until Ctrl-C."""
    from openajar.controller.app import serve_controller

    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = ensure_config()
    if port is not None or host is not None:
        cfg = cfg.model_copy(update={k: v for k, v in {"port": port, "host": host}.items() if v is not None})

    def started(bound: int, controller) -> None:
        typer.echo(f"Controller up. Agents: {cfg.mcp_url}  Nodes: {cfg.node_url}")

    try:
        asyncio.run(serve_controller(cfg, on_started=started))
    except RuntimeError as exc:
        raise fail(str(exc)) from None
    except KeyboardInterrupt:
        pass


@app.command("join-token")
def join_token(
    name: Annotated[str | None, typer.Option(help="The node's name (lowercase, digits, dashes).")] = None,
    capability: Annotated[list[str] | None, typer.Option(help="Limit the node to these capabilities.")] = None,
    ttl_minutes: Annotated[int, typer.Option(help="Minutes the token stays valid (at most 60).", min=1, max=60)] = 60,
) -> None:
    """Make a single-use join token and print the line to run on the new node."""
    from openajar.core.errors import OpenajarError

    cfg = ensure_config()
    controller = _controller(cfg)
    try:
        row, token = controller.create_join_token(controller.org["id"], name=name, capabilities=capability or None,
                                                  ttl_minutes=ttl_minutes)
    except OpenajarError as exc:
        from openajar.core.catalog import cli_error

        typer.secho(cli_error(exc.code, exc.message), err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    finally:
        controller.close()
    typer.echo(f"Join token {row['id']} (single use, valid for {ttl_minutes} minutes). On the new node run:")
    typer.echo(f"  {controller.join_command(token)}")
    typer.echo("Then restart its agentosd (or run `openajar node serve`). --self-hosted lets this controller do "
               "everything on the node (the controller ceiling); leave it out to keep the hosted limits.")
    if not cfg.public_url:
        typer.echo(f"A node on another machine needs a URL it can reach: set public_url in {config_file(cfg.home)} "
                   "(wss://, behind TLS), or tunnel to this box and use the localhost URL above.")


@app.command("login")
def login() -> None:
    """Print a one-time sign-in link for the panel (valid for 10 minutes)."""
    cfg = ensure_config()
    controller = _controller(cfg)
    try:
        url, _ = controller.login_link()
    finally:
        controller.close()
    typer.echo("Open this link once to sign in (valid for 10 minutes):")
    typer.echo(f"  {url}")


@app.command("nodes")
def nodes() -> None:
    """Enrolled nodes and when each was last seen."""
    from openajar.node.channel import wire

    cfg = ensure_config()
    controller = _controller(cfg)
    try:
        rows = controller.store.nodes(controller.org["id"])
    finally:
        controller.close()
    if not rows:
        typer.echo("No nodes yet. Make a token with `openajar controller join-token`.")
        return
    for node in rows:
        seen = wire.iso(node["last_seen"]) if node.get("last_seen") else "never"
        lanes = len((node.get("state") or {}).get("lanes", []))
        typer.echo(f"{node['name']}  {node['id']}  last seen {seen}  {lanes} lanes  {node.get('version') or ''}")


@app.command("anchors")
def anchors_cmd(
    node: Annotated[str, typer.Argument(help="The node's name or id (see `openajar controller nodes`).")],
    out: Annotated[Path | None, typer.Option("-o", "--out", help="Write them to this file for "
                                             "`openajar audit verify --anchors-file` (default: a summary).")] = None,
) -> None:
    """The audit checkpoints that nodes keep here, each with this controller's signed receipt (its proof of
    storing it)."""
    cfg = ensure_config()
    controller = _controller(cfg)
    try:
        org = controller.org["id"]
        match = [n for n in controller.store.nodes(org) if node in (n["id"], n["name"])]
        if not match:
            raise fail(f"no node {node!r} here")
        node_id = match[0]["id"]
        receipts = controller.store.anchors(org, node_id, limit=1_000_000)
        conflicts = controller.store.anchor_conflicts(org, node_id)
        pubkey = controller.keys.controller_pubkey
    finally:
        controller.close()
    doc = {"node": node_id, "controller_pubkey": pubkey, "receipts": receipts, "conflicts": conflicts}
    if out is not None:
        out.write_text(json.dumps(doc, indent=1) + "\n")
        typer.echo(f"{len(receipts)} anchors written to {out}")
    else:
        chains = sorted({r["anchor"]["chain"] for r in receipts})
        cps = [r for r in receipts if not r["anchor"].get("kind")]
        last = max(cps, key=lambda r: r["received_at"])["anchor"] if cps else None
        typer.echo(f"{len(receipts)} anchors in {len(chains)} chain(s)" +
                   (f", the newest checkpoint {last['n']} (seq {last['seq']})" if last else ""))
        if len(chains) > 1:
            typer.echo(f"the node's log was restarted {len(chains) - 1} time(s): chains {', '.join(chains)}")
    if conflicts:
        typer.secho(f"{len(conflicts)} time(s) the node sent another head for a checkpoint it had anchored: "
                    "its log changed after it was anchored", fg=typer.colors.RED)


@app.command("agent-token")
def agent_token(
    new: Annotated[bool, typer.Option("--new", help="Revoke the current agent token and make another.")] = False,
    ssh_host: Annotated[str | None, typer.Option(help="This box's SSH alias, for the connect hint.")] = None,
) -> None:
    """Show how to connect an agent (the token lives in controller.toml, mode 600)."""
    cfg = ensure_config()
    if new:
        controller = _controller(cfg)
        try:
            row = controller.store.grant_by_token(cfg.agent_token)
            if row is not None:
                asyncio.run(controller.revoke_grant(controller.org["id"], row["id"]))
        finally:
            controller.close()
        cfg = cfg.model_copy(update={"agent_token": "aga_" + secrets.token_urlsafe(32)})
        save_controller_config(cfg)
        typer.echo("Made a new agent token; the old one no longer works. Restart a running controller.")
    for line in connect_lines(cfg, ssh_host):
        typer.echo(line)


def _owner_call(cfg: ControllerConfig, method: str, path: str, body: dict | None = None) -> dict | None:
    """The running controller's answer (owner token), or None when it does not answer."""
    import httpx

    try:
        response = httpx.request(method, f"{cfg.local_url}{path}", json=body, timeout=15.0,
                                 headers={"Authorization": f"Bearer {cfg.owner_token}"})
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        raise fail(f"{data['error'].get('code')}: {data['error'].get('message')}")
    return data if response.status_code < 400 and isinstance(data, dict) else None


@app.command("freeze")
def freeze(reason: Annotated[str, typer.Option("--reason", "-r", help="Why, in one sentence.")]) -> None:
    """Freeze every node under this controller: online nodes at once, offline ones when they connect, before
    anything else runs there. Every agent grant under it is revoked."""
    cfg = ensure_config()
    data = _owner_call(cfg, "POST", "/api/freeze", {"reason": reason})
    if data is None:
        controller = _controller(cfg)
        try:
            org = controller.org["id"]
            if controller.store.mesh_freeze(org) is None:
                controller.store.set_mesh_freeze(org, freeze_id="fz_" + secrets.token_hex(6),
                                                 reason=" ".join(reason.split())[:300] or "mesh freeze",
                                                 by="owner (cli)", revoked=[])
            for g in controller.store.grants(org):
                if g.get("revoked_at") is None and g.get("parent_id") is None:
                    controller.grants.revoke(org, g["id"])
            data = controller.mesh_status(org)
        finally:
            controller.close()
        typer.secho("The controller is not running: the freeze is recorded and every node gets it when it dials "
                    "in, before anything else runs there.", fg=typer.colors.YELLOW)
    _print_mesh(data)


@app.command("unfreeze")
def unfreeze() -> None:
    """Lift the controller's freeze on every node (never a freeze made on a node itself). Restores
    no grant."""
    cfg = ensure_config()
    data = _owner_call(cfg, "POST", "/api/unfreeze", {})
    if data is None:
        controller = _controller(cfg)
        try:
            controller.store.clear_mesh_freeze(controller.org["id"])
        finally:
            controller.close()
        typer.secho("The controller is not running: nodes lift the mesh freeze when they dial in.",
                    fg=typer.colors.YELLOW)
    typer.echo("The mesh freeze is lifted. Nothing was restored: grant agents again (`openajar controller "
               "agent-token --new` for the owner's agent token).")


@app.command("freeze-status")
def freeze_status() -> None:
    """Whether the controller's freeze is on, and which nodes have it."""
    cfg = ensure_config()
    data = _owner_call(cfg, "GET", "/api/freeze")
    if data is None:
        controller = _controller(cfg)
        try:
            data = controller.mesh_status(controller.org["id"])
        finally:
            controller.close()
    _print_mesh(data)


def _print_mesh(data: dict) -> None:
    if not data.get("frozen"):
        typer.echo("The mesh is not frozen.")
        return
    typer.echo(f"The mesh is frozen ({data.get('freeze_id')}) since {data.get('at')} by {data.get('by')}: "
               f"{data.get('reason')}")
    for node in data.get("nodes", []):
        state = "applied" if node.get("applied") else "pending (applied when it dials in)"
        typer.echo(f"  {node['name']}: {state}")
    if data.get("revoked_grants"):
        typer.echo(f"  revoked {len(data['revoked_grants'])} agent grant(s)")


def _free_node_port(controller_port: int | None) -> int | None:
    """On a new box, the node's MCP port: the default, or the next free one when something
    (an old tunnel, another openajar) already listens there. None keeps the default."""
    from openajar.core.config import NodeConfig
    from openajar.dev.cli import port_is_free

    first = NodeConfig.model_fields["mcp_port"].default
    try:
        taken = {DEFAULT_PORT, controller_port or load_controller_config(controller_home()).port}
    except (ValueError, OSError):  # a broken controller.toml: ensure_config says so in a moment
        taken = {DEFAULT_PORT, controller_port}
    for port in range(first, first + 100):
        if port not in taken and port_is_free(port):
            return None if port == first else port
    return None


def _here(here: bool | None) -> bool:
    from openajar.client.state import current_platform

    return here_wanted(here, current_platform(), Path.home())


def here_wanted(here: bool | None, platform: str, home: Path) -> bool:
    """Whether `openajar up` connects this machine's own clients. A Mac cannot be a node yet."""
    import shutil

    from openajar.client.codex import installed as codex_installed

    if here is False:
        return False
    if platform == "macos":
        if here:
            raise fail("a Mac cannot be a node yet (browser lanes need Linux; docs/design/desktop-nodes.md "
                       "is the plan). Try openajar on this Mac with the fake node: `openajar dev up --seed`")
        return False
    return bool(here) or bool(shutil.which("claude")) or codex_installed(home)


def _connect_here(node_cfg: Any) -> None:
    """Runs beside the node once `openajar up` is ready: this machine's clients, straight to 127.0.0.1."""
    from openajar.client.cli import summary
    from openajar.client.errors import ClientError
    from openajar.client.ops import Client

    c = Client(out=lambda line: None)
    try:
        result = c.connect_local(port=node_cfg.mcp_port, token=node_cfg.mcp_token, node_name=node_cfg.name,
                                 home=str(node_cfg.home))
    except ClientError as exc:
        typer.secho(f"Could not connect this machine's agents: {exc.message}", err=True, fg=typer.colors.YELLOW)
        if exc.hint:
            typer.echo(f"  {exc.hint}", err=True)
        return
    typer.echo("")
    typer.echo(f"This machine's agents, straight to {result.conn.url} (no tunnel):")
    summary(c, result)


def up_command(
    controller_port: Annotated[int | None, typer.Option(help="Controller port (default from controller.toml).",
                                                        min=1, max=65535)] = None,
    ssh_host: Annotated[str | None, typer.Option(help="This box's SSH alias, for the connect line.")] = None,
    here: Annotated[bool | None, typer.Option(
        "--here/--not-here", help="Also connect Claude Code and Codex on this machine straight to the node, with "
                                  "their own grant (Linux; the default when either is installed here).",
        show_default=False)] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Set up and start this box, then print a sign-in link.

    The first time, it writes node.toml (as `openajar node init`) and controller.toml.

    It runs a controller and this node, joined, and prints the panel and a one-time sign-in link.

    Safe to run again: settings and tokens are kept. By hand: `openajar connect --by-hand`.

    With Claude Code or Codex on this machine (or `--here`), they connect to it too, with no tunnel."""
    from openajar.commands.base import init_node, read_config
    from openajar.controller.up import run_up
    from openajar.core import logs
    from openajar.core.config import config_path
    from openajar.node.daemon import install_log_scrubbers
    from openajar.node.tools import ssh_host as default_ssh_host

    home = home_from_env()
    local_clients = _here(here)
    made = []
    if not config_path(home).exists():
        init_node(home, {"ssh_host": ssh_host, "mcp_port": _free_node_port(controller_port)})
        made.append(str(config_path(home)))
    if not config_file(controller_home(home)).exists():
        made.append(str(config_file(controller_home(home))))
    node_cfg = read_config(home)  # a broken node.toml: one clean line, not a traceback
    # The same log path and scrubbers as `openajar node serve`: this process runs agentosd too, and
    # a plain basicConfig handler would write tokens, proxy passwords and vault values as they come.
    logs.setup(home, verbose=verbose)
    install_log_scrubbers(node_cfg)  # the vault's values join once the node's runtime is built
    cfg = ensure_config(controller_home(home), port=controller_port)
    # as `openajar node serve`: JSON to journald or logs/agentosd.jsonl, every line scrubbed, and the
    # controller's own tokens too (they stay when the node installs its scrubbers again)
    handlers = logs.setup(node_cfg.home, verbose=verbose)
    install_log_scrubbers(node_cfg, also=(cfg.agent_token, cfg.owner_token))
    if not verbose and sys.stderr.isatty() and not logs.under_systemd(sys.stderr):
        # a person at a terminal: the sign-in link, not every INFO line (the log file keeps them)
        for handler in handlers:
            if type(handler) is logging.StreamHandler:
                handler.setLevel(logging.WARNING)
    if made:
        typer.echo(f"First run: wrote {' and '.join(made)}.")

    def ready(controller_cfg: ControllerConfig, node_name: str) -> None:
        controller = _controller(controller_cfg)  # as `openajar controller login`: the code is in its database
        try:
            link, _ = controller.login_link()
        finally:
            controller.close()
        host = ssh_host or default_ssh_host(node_cfg)  # OPENAJAR_SSH_HOST, then node.toml, then the hostname
        typer.echo("")
        typer.echo(f"openajar is up: controller and node {node_name}, joined.")
        typer.echo(f"  Panel:    {controller_cfg.local_url}")
        typer.echo(f"  Sign in:  {link}")
        typer.echo("            (works once, for 10 minutes; `openajar controller login` makes another)")
        typer.echo(f"  Connect:  openajar connect {host} --remote-home {controller_cfg.home} --name "
                   f"{controller_cfg.name}   (on your computer)")
        typer.echo("Press Ctrl-C to stop. The log: `openajar node logs` (or -v). By hand: `openajar connect "
                   "--by-hand`. Another machine: `openajar controller join-token`.")
        if local_clients:
            threading.Thread(target=_connect_here, args=(node_cfg,), daemon=True, name="up-here").start()

    try:
        asyncio.run(run_up(node_cfg, cfg, on_ready=ready))
    except ModuleNotFoundError as exc:
        raise fail(f"missing Python module {exc.name}; run deploy/install-node.sh (or `uv sync`)") from None
    except RuntimeError as exc:
        raise fail(logs.scrub(str(exc))) from None
    except KeyboardInterrupt:
        pass
    except Exception:
        # Through the log (scrubbed), not as a raw traceback on stderr: it can quote anything.
        logging.getLogger("openajar.up").exception("openajar up stopped on an unexpected error")
        raise typer.Exit(1) from None
