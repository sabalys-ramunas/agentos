"""`agentos` command.

Lane commands talk to the store through a LaneManager directly, without the
daemon, so they work before `agentos node serve` runs. The lane control commands
(take, handback, stop, deny, view) are the exception: control lives in the running
daemon, so they call its HTTP API with the node token. Heavy modules (the lane
manager, uvicorn, the MCP server, httpx) are imported only by the commands that need them.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import tomllib
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from agentos import __version__, lanebench_cli
from agentos.core import catalog
from agentos.core.config import (
    NodeConfig,
    config_path,
    config_problems,
    home_from_env,
    load_config,
    load_proxies,
    save_config,
    save_proxies,
)
from agentos.core.errors import AgentosError, InvalidRequest
from agentos.core.models import LaneSpec, ProxyConfig, parse_lane_id
from agentos.dev import cli as dev_cli
from agentos.ops.restart import restart_command
from agentos.ops.templates import templates_command
from agentos.studio import cli as studio_cli

# Tracebacks never print local variables: they would include the MCP token and proxy passwords.
app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False,
                  help="agentos: a self-hosted computer for your agents.")
node_app = typer.Typer(no_args_is_help=True, help="Set up and run this node.")
proxy_app = typer.Typer(no_args_is_help=True, help="Proxies that lanes exit through.")
lane_app = typer.Typer(no_args_is_help=True, help="Browser lanes: one logged-in account each.")
device_app = typer.Typer(no_args_is_help=True,
                         help="Devices: one fixed browser fingerprint and one proxy shared by a company's "
                              "lanes.")
app.add_typer(node_app, name="node")
app.add_typer(proxy_app, name="proxy")
app.add_typer(lane_app, name="lane")
app.add_typer(device_app, name="device")

PROXY_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,40}")  # used with fullmatch
PROXY_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")


def fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    return typer.Exit(code)


def fail_coded(code: str, message: str, fix: str | None = None) -> typer.Exit:
    """`error: <code>: <message>` and the error catalog's fix (or the one the node sent)."""
    text = catalog.cli_error(code, message)
    if fix and "\n" not in text:
        text = f"{text}\n  fix: {fix}"
    typer.secho(text, err=True, fg=typer.colors.RED)
    return typer.Exit(1)


def ensure_dir(path: Path, mode: int = 0o700, *, tighten: bool = True) -> None:
    """Create `path` with `mode`. An existing directory is tightened to `mode` only when
    `tighten` is set: agentos owns its secrets and lanes dirs, but not a home it was pointed at."""
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    if tighten or not existed:
        path.chmod(mode)


def read_config(home: Path) -> NodeConfig:
    """load_config with a clean error instead of a traceback (never echoing values)."""
    path = config_path(home)
    try:
        return load_config(home)
    except tomllib.TOMLDecodeError as exc:
        raise fail(f"{path} is not valid TOML ({exc}); fix or remove it") from None
    except ValidationError as exc:
        raise fail(f"{path} has invalid settings: {config_problems(exc)}") from None
    except OSError as exc:
        raise fail(f"cannot read {path}: {exc.strerror or exc}") from None


def initialized_config() -> NodeConfig:
    home = home_from_env()
    if not config_path(home).exists():
        raise fail(f"no node.toml in {home}; run `agentos node init` first (or set AGENTOS_HOME)")
    return read_config(home)


def valid_lane_id(value: str) -> str:
    try:
        parse_lane_id(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    return value


def missing_module(exc: ModuleNotFoundError) -> typer.Exit:
    if (exc.name or "").startswith("agentos."):
        return fail(f"{exc.name} is not part of this build yet, so lanes cannot run here")
    return fail(f"missing Python module {exc.name}; run deploy/install-node.sh (or `uv sync`)")


def run_manager(coro_factory):
    """Run one LaneManager call from the CLI and turn agentos errors into a clean exit."""
    from agentos.node import daemon

    cfg = initialized_config()
    try:
        manager = daemon.build_lane_manager(cfg)
        return asyncio.run(coro_factory(manager))
    except ModuleNotFoundError as exc:
        raise missing_module(exc) from None
    except AgentosError as exc:
        raise fail_coded(exc.code, exc.message) from None
    except sqlite3.DatabaseError as exc:
        raise fail(f"the lane database in {cfg.home} cannot be read ({exc}); restore it from a backup") from None


GRANT_FOR_AGENTS = "agentos access grants issue --subject claude-code --workspace '*' --ceiling commit --ttl 90d"


def connect_lines(cfg: NodeConfig, ssh_host: str) -> list[str]:
    """The by-hand way: a tunnel, and Claude Code with a grant of its own (never the node token,
    which is the owner's: a token never decides what its own grant family asked for)."""
    port = cfg.mcp_port
    tunnel = f"ssh -N -L {port}:localhost:{port} {ssh_host}"
    url = f"http://localhost:{port}/mcp"
    add = (f'claude mcp add --scope user --transport http agentos {url} '
           '--header "Authorization: Bearer <grant token>"')
    return [tunnel, add]


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
    from agentos.mcp.reference import reference_markdown, reference_rows
    from agentos.node.daemon import feature_providers, tool_providers
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
    panel_json: Annotated[bool, typer.Option("--panel-json", help="What the panel maps codes to "
                                                                  "(panel/src/api/errors.json).")] = False,
) -> None:
    """Every error code: what it means, what an agent should do, and how to fix it."""
    import dataclasses
    import json

    if panel_json:
        typer.echo(json.dumps(catalog.panel_json(), indent=2, sort_keys=True))
        return
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


# ---- config ----

config_app = typer.Typer(no_args_is_help=True, help="This node's settings.")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable.")] = False,
) -> None:
    """Every setting with its resolved value and where it came from: env, node.toml or the default.
    Secrets are never printed."""
    import json

    from agentos.core.config_report import config_report, render

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
    from agentos.core.config_report import example

    for line in example():
        typer.echo(line)


# ---- node ----


def init_node(home: Path, overrides: dict[str, Any] | None = None, *, new_token: bool = False) -> tuple[NodeConfig, Path]:
    """What `agentos node init` writes, without what it prints (`agentos up` runs it on a new box):
    AGENTOS_HOME, its secrets and lanes folders, and node.toml with an MCP token. An existing
    node.toml keeps its token unless `new_token`."""
    cfg = read_config(home) if config_path(home).exists() else NodeConfig(home=home)
    changed = {key: value for key, value in (overrides or {}).items() if value is not None}
    if new_token or not cfg.mcp_token:
        changed["mcp_token"] = secrets.token_urlsafe(32)
    try:
        cfg = NodeConfig(**{**cfg.model_dump(), **changed})
    except ValidationError as exc:
        raise fail(f"invalid options: {config_problems(exc)}", code=2) from None

    ensure_dir(home, tighten=False)
    ensure_dir(cfg.secrets_dir)
    ensure_dir(home / "lanes")
    stamp_fresh_node_db(home)
    return cfg, save_config(cfg)


def stamp_fresh_node_db(home: Path) -> None:
    """A new box starts at the latest node schema: agentos.db is made by its migrations, so
    `agentos migrate status` has nothing pending. An existing one is left to `agentos migrate run`."""
    import dataclasses

    from agentos.ops import migrate as migrations

    if (home / migrations.NODE.db).exists():
        return
    migrations.migrate(home, [dataclasses.replace(migrations.NODE, create=True)], backup=False)


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
    """Create AGENTOS_HOME, write node.toml with an MCP token, and print how to connect.

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
    from agentos.node.tools import ssh_host as default_ssh_host
    host = default_ssh_host(cfg)
    typer.echo("Connect from your Mac (keeps a tunnel up, keeps the token in the Keychain, registers")
    typer.echo("the server with Claude Code):")
    typer.echo(f"  agentos connect {host}")
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
    `agentos connect` reads it over SSH for a few calls and never stores it; connections made
    before that held it on the computer. Rotate it once every connection has its own grant.
    """
    from agentos.api.auth import PanelStore

    cfg = initialized_config()
    cfg = cfg.model_copy(update={"mcp_token": secrets.token_urlsafe(32)})
    path = save_config(cfg)
    typer.echo(f"Made a new node token in {path}.")
    ended = PanelStore.for_home(cfg.home).delete_device_sessions()  # whoever held the old token could mint these
    if ended:
        typer.echo(f"Ended {ended} saved computer credential{'s' if ended != 1 else ''} the old token made. On each "
                   "computer, run `agentos connect --upgrade` for a new one.")
    typer.echo("The old one works until agentosd restarts. Restart it now:")
    typer.echo("  agentos node restart     (it names the open lanes and asks first if others hold any)")
    typer.echo("Connections with their own agent grant keep working. One that still holds the node token")
    typer.echo("stops: on that computer, run `agentos connect --upgrade` (it reads the new token over SSH).")


@node_app.command("serve")
def node_serve(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Run agentosd: the lanes and the MCP server (systemd runs this)."""
    from agentos.core import logs
    from agentos.node.daemon import install_log_scrubbers, serve

    cfg = initialized_config()
    if not cfg.mcp_token:
        raise fail("node.toml has no mcp_token; run `agentos node init`")
    logs.setup(cfg.home, verbose=verbose)  # JSON to journald or logs/agentosd.jsonl (agentos.core.logs)
    install_log_scrubbers(cfg)  # the vault's values join once the runtime is built
    try:
        asyncio.run(serve(cfg))
    except ModuleNotFoundError as exc:
        raise missing_module(exc) from None
    except Exception:
        # Through the log (scrubbed), not as a raw traceback on stderr: it can quote anything.
        logging.getLogger("agentos.node").exception("agentosd stopped on an unexpected error")
        raise typer.Exit(1) from None


@node_app.command("join")
def node_join(
    controller_url: Annotated[str, typer.Argument(help="The controller, for example wss://controller.example.com "
                                                       "(plain ws:// only to localhost).")],
    join_token: Annotated[str, typer.Argument(help="A join token from `agentos controller join-token` or the cloud.")],
    name: Annotated[str | None, typer.Option(help="This node's name in the controller (default: from node.toml).")]
    = None,
    capability: Annotated[list[str] | None, typer.Option(help="Capabilities to advertise (default browser, "
                                                              "terminal, service).")] = None,
    self_hosted: Annotated[bool, typer.Option("--self-hosted", help="The controller is your own (`agentos "
                                                                     "controller serve`): let it do everything here. "
                                                                     "Without it, a controller may post up to commit, "
                                                                     "use no specials and only watch live views.")]
    = False,
) -> None:
    """Join this node to a controller. agentosd keeps the channel up from its next start."""
    from agentos.node.channel.enroll import EnrollmentFailed, EnrollmentRefused, enroll
    from agentos.policy.ceiling import ControllerCeiling

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
               "Change it with the controller_* settings in node.toml (`agentos config example`).")
    typer.echo("Restart agentosd to connect (`agentos node restart`, or `agentos node serve`).")


node_app.command("templates")(templates_command)
node_app.command("restart")(restart_command)


# ---- proxy ----


def _is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def parse_proxy(label: str, spec: str) -> ProxyConfig:
    """`host:port[:user[:pass]]`. The password may itself contain colons."""
    if spec.startswith("["):
        raise typer.BadParameter("IPv6 proxy hosts are not supported yet; use the proxy's IPv4 address or hostname")
    parts = spec.split(":", 3)
    if len(parts) < 2 or not parts[0]:
        raise typer.BadParameter("expected host:port:user:pass (user and pass are optional)")
    host, port_text = parts[0], parts[1]
    if _is_ipv6(host) or "[" in host or "]" in host:
        raise typer.BadParameter("IPv6 proxy hosts are not supported yet; use the proxy's IPv4 address or hostname")
    if len(host) > 253 or not PROXY_HOST_RE.fullmatch(host):
        raise typer.BadParameter("host must be an IPv4 address or a hostname")
    try:
        port = int(port_text)
    except ValueError:
        raise typer.BadParameter(f"port must be a number, got {port_text!r}") from None
    if not 0 < port < 65536:
        raise typer.BadParameter(f"port out of range: {port}")
    username = parts[2] if len(parts) > 2 and parts[2] else None
    password = parts[3] if len(parts) > 3 and parts[3] else None
    if password is not None and username is None:
        raise typer.BadParameter("a proxy password needs a username (host:port:user:pass)")
    if username and password is None:
        password = typer.prompt(f"Password for proxy {label}", hide_input=True)
    return ProxyConfig(label=label, host=host, port=port, username=username, password=password)


@proxy_app.command("add")
def proxy_add(
    label: Annotated[str, typer.Argument(help="Short name lanes refer to, for example lt1.")],
    spec: Annotated[str, typer.Argument(help="host:port:user:pass (leave out :pass to be prompted).")],
    yes: Annotated[bool, typer.Option("--yes", help="Point an existing label at another proxy, although the lanes "
                                                    "and devices using it then reach their sites from a new IP.")]
    = False,
) -> None:
    """Store a proxy in secrets/proxies.json (chmod 600). The password is never printed. Pointing
    an existing label at another host, port or user needs --yes: every lane and device using it
    gets a new IP."""
    if not PROXY_LABEL_RE.fullmatch(label):
        raise typer.BadParameter("lowercase letters, digits, dot, dash or underscore", param_hint="LABEL")
    cfg = initialized_config()
    proxy = parse_proxy(label, spec)
    proxies = load_proxies(cfg)
    existed = label in proxies
    old = proxies.get(label)
    if old is not None and (old.host, old.port, old.username) != (proxy.host, proxy.port, proxy.username) and not yes:
        from agentos.ops.layout import proxy_users

        users = proxy_users(cfg.home, label)
        who = f" ({'; '.join(users)})" if users else ""
        raise fail(f"proxy {label} already points at {old.host}:{old.port}. Pointing it elsewhere is a new IP for "
                   f"everything that uses it{who}: logged-in accounts may be challenged. Nothing changed. If you "
                   f"still want it: agentos proxy add {label} ... --yes")
    proxies[label] = proxy
    save_proxies(cfg, proxies)
    auth = "with credentials" if proxy.username else "without credentials"
    typer.echo(f"{'Updated' if existed else 'Added'} proxy {label}: {proxy.host}:{proxy.port} {auth}")


@proxy_app.command("list")
def proxy_list() -> None:
    """Proxies by label (credentials are never shown)."""
    proxies = load_proxies(initialized_config())
    if not proxies:
        typer.echo("No proxies. Add one with `agentos proxy add <label> host:port:user:pass`.")
        return
    for p in sorted(proxies.values(), key=lambda p: p.label):
        auth = "with credentials" if p.username else "without credentials"
        typer.echo(f"{p.label}  {p.host}:{p.port}  {auth}")


# ---- lane ----


def check_proxy(cfg: NodeConfig, label: str | None) -> None:
    if label and label not in load_proxies(cfg):
        raise fail(f"no proxy {label!r}; add it with `agentos proxy add {label} host:port:user:pass`")


LaneIdArg = Annotated[str, typer.Argument(help="workspace/name, for example personal/reddit.",
                                          callback=valid_lane_id)]
ProxyOpt = Annotated[str | None, typer.Option(help="Proxy label from `agentos proxy add`.")]
DeviceOpt = Annotated[str | None, typer.Option(help="Device from `agentos device create`: the lane launches with "
                                                    "its fingerprint and proxy, and keeps its own profile.")]


@lane_app.command("create")
def lane_create(
    lane_id: LaneIdArg,
    proxy: ProxyOpt = None,
    device: DeviceOpt = None,
    engine: Annotated[str, typer.Option(help="Browser engine.")] = "camoufox",
) -> None:
    """Create a lane with an empty profile. It stays off (not running) until first use. Log in later through
    lane_view."""
    check_proxy(initialized_config(), proxy)
    try:
        spec = LaneSpec(id=lane_id, proxy_label=proxy, engine=engine, device=device)
    except ValueError as exc:
        raise fail(_first_validation_error(exc)) from None
    info = run_manager(lambda manager: manager.create(spec))
    on = f", device {info.spec.device}" if info.spec.device else ""
    typer.echo(f"Created lane {info.spec.id} (engine {info.spec.engine}, proxy {info.spec.proxy_label or 'none'}{on})")


@lane_app.command("import")
def lane_import(
    lane_id: LaneIdArg,
    profile: Annotated[Path, typer.Option(help="Existing browser profile directory to copy in.",
                                          exists=True, file_okay=False, dir_okay=True, readable=True)],
    fingerprint: Annotated[Path | None, typer.Option(help="Browser fingerprint as JSON, to copy in.",
                                                     exists=True, file_okay=True, dir_okay=False)] = None,
    proxy: ProxyOpt = None,
    engine: Annotated[str, typer.Option(help="Browser engine.")] = "camoufox",
) -> None:
    """Create a lane from an existing logged-in profile (for example, one from the first prototype)."""
    check_proxy(initialized_config(), proxy)
    spec = LaneSpec(id=lane_id, proxy_label=proxy, engine=engine)
    info = run_manager(lambda manager: manager.import_profile(spec, profile, fingerprint))
    pinned = "with its pinned fingerprint" if fingerprint else "fingerprint pinned on first launch"
    typer.echo(f"Imported lane {info.spec.id} from {profile} ({pinned})")


@lane_app.command("delete")
def lane_delete(lane_id: LaneIdArg, yes: Annotated[bool, typer.Option("--yes", help="Do not ask.")] = False) -> None:
    """Remove a lane that is not running. Its profile and files move to AGENTOS_HOME/trash, nothing
    is erased."""
    if not yes:
        typer.confirm(f"Remove lane {lane_id}? Its files move to the trash folder.", abort=True)
    trash = run_manager(lambda manager: manager.delete(lane_id, by="owner"))
    typer.echo(f"Removed lane {lane_id}." + (f" Its files are in {trash}." if trash else ""))


@lane_app.command("set-proxy")
def lane_set_proxy(
    lane_id: LaneIdArg,
    label: Annotated[str, typer.Argument(help="Proxy label from `agentos proxy list`, or none.")],
    yes: Annotated[bool, typer.Option("--yes", help="Go ahead although the lane's site will see a new IP.")]
    = False,
) -> None:
    """Move a lane that is not running to another proxy. The lane's sites see a new IP next time, so do this
    rarely. A lane that has launched needs --yes."""
    new = None if label.lower() == "none" else label
    if new:
        check_proxy(initialized_config(), new)
    info = run_manager(lambda manager: manager.set_proxy(lane_id, new, confirm=yes, by="owner"))
    typer.echo(f"Lane {lane_id} now uses proxy {info.spec.proxy_label or 'none'}.")


def _lane_rows_from_daemon(cfg: NodeConfig) -> list[tuple[str, ...]] | None:
    """The lanes as the running agentosd sees them (whether a browser runs is known only there), or
    None when it is not running."""
    from datetime import datetime

    listed = daemon_request(cfg, "GET", "/api/lanes", absent_ok=True)
    if not isinstance(listed, list):
        return None
    rows = []
    for lane in listed:
        last = "never"
        if lane.get("last_used"):
            try:
                last = datetime.fromisoformat(str(lane["last_used"])).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                last = str(lane["last_used"])
        proxy = lane.get("proxy") or {}
        rows.append((str(lane.get("id")), str(lane.get("state")), str(lane.get("engine") or "-"),
                     str(lane.get("device") or "own"), str(proxy.get("label") or "-"), last))
    return rows


@lane_app.command("list")
def lane_list() -> None:
    """Lanes on this node: from the running agentosd (which knows which browsers run), else from the store."""
    cfg = initialized_config()
    rows = _lane_rows_from_daemon(cfg)
    note = None
    if rows is None:
        from agentos.node.lanes.specs import _browsers_using

        async def lanes(manager):
            return manager.list()

        rows = []
        for info in run_manager(lanes):
            last = info.last_used.strftime("%Y-%m-%d %H:%M") if info.last_used else "never"
            state = info.state.value
            with contextlib.suppress(Exception):  # no process table to read here: the store's state stands
                if state == "cold" and _browsers_using(cfg.profile_dir(info.spec.id)):
                    state, note = "running", ("agentosd is not running, but a browser still runs on a lane marked "
                                              "running. Start agentosd, or stop that browser.")
            rows.append((info.spec.id, state, info.spec.engine, info.spec.device or "own",
                         info.spec.proxy_label or "-", last))
    if not rows:
        typer.echo("No lanes. Create one with `agentos lane create <workspace/name>`.")
        return
    rows = [("LANE", "STATE", "ENGINE", "DEVICE", "PROXY", "LAST USED"), *rows]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())
    if note:
        typer.echo(note)


@lane_app.command("set-device")
def lane_set_device(
    lane_id: LaneIdArg,
    device: Annotated[str, typer.Argument(help="Device from `agentos device list`, or none (the lane becomes a "
                                               "device of its own again).")],
    yes: Annotated[bool, typer.Option("--yes", help="Go ahead although the lane's site will see a new computer.")]
    = False,
) -> None:
    """Move a lane that is not running onto a device, or off it. A lane that has launched gets the device's
    fingerprint (the browser details a site sees), which is a new computer to its site: expect a re-login
    or a checkpoint (the site asks the account owner a question). That needs --yes. To share a lane's own
    fingerprint instead, make the device from it: agentos device create <name> --from-lane <lane>."""
    target = None if device.lower() == "none" else device

    async def plan(manager):
        return manager.plan_device_move(lane_id, target)

    cfg = initialized_config()
    query = "?" + urllib.parse.urlencode({"to": target or "none"})
    shown = daemon_request(cfg, "GET", lane_api_path(lane_id, "device-move") + query, absent_ok=True)
    if not isinstance(shown, dict):
        shown = run_manager(plan)
    for line in shown["changes"]:
        typer.echo(f"  {line}.")
    for line in shown["warnings"]:
        typer.echo(f"  Warning: {line}.")
    if shown["from"] == shown["to"]:
        return
    if shown["confirm"] and not yes:
        raise fail(f"Nothing changed. If you still want it: agentos lane set-device {lane_id} {device} --yes")
    moved = daemon_request(cfg, "POST", lane_api_path(lane_id, "device"), {"device": target, "confirm": yes},
                           absent_ok=True, person=True)  # a move changes the lane's exit: a person's call
    if isinstance(moved, dict):
        done = moved["plan"]
    else:  # agentosd is not running: in this process, under the same rules
        done = run_manager(lambda manager: manager.set_device(lane_id, target, confirm=yes, by="owner"))
    where = f"device {done['to']}" if done["to"] else "a device of its own"
    typer.echo(f"Lane {lane_id} is now on {where} (proxy {done['proxy']['to'] or 'none'}).")


# ---- devices ----


def _first_validation_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            return str(errors(include_input=False, include_url=False)[0].get("msg", exc)).removeprefix("Value error, ")
        except (IndexError, TypeError):
            pass
    return str(exc)


def _show_pin(pin: dict | None) -> str:
    if not pin:
        return "not pinned yet (its first lane's first launch pins it where the proxy exits)"
    parts = [pin.get("os") or "?", pin.get("screen") or "?", f"Firefox {pin.get('firefox') or '?'}",
             pin.get("timezone") or "?", pin.get("locale") or "?"]
    where = f", pinned from {pin['pinned_exit_ip']} ({pin.get('pinned_country') or '?'})" \
        if pin.get("pinned_exit_ip") else ""
    return " · ".join(str(p) for p in parts) + where


@device_app.command("create")
def device_create(
    name: Annotated[str, typer.Argument(help="A short name, for example biz-a-pc.")],
    proxy: ProxyOpt = None,
    from_lane: Annotated[str | None, typer.Option("--from-lane", help="Take this lane's fixed fingerprint "
                                                                      "and proxy (the lane joins the "
                                                                      "device, unchanged).",
                                                  callback=lambda v: valid_lane_id(v) if v else v)] = None,
    note: Annotated[str | None, typer.Option(help="What the device is for.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="With --from-lane: go ahead although other lanes of their own "
                                                    "leave through the same proxy.")] = False,
) -> None:
    """Make a device: one browser fingerprint (the details a site sees of its computer) and one proxy for a
    company's lanes, like one office PC with a browser profile per account. Decide this before the first
    logins. One device per proxy: a proxy that another device or a lane of its own uses is refused (make
    the device --from-lane that lane instead)."""
    if proxy is None and from_lane is None:
        raise fail("give the device its proxy (--proxy <label>), or make it from a lane its site already knows "
                   "(--from-lane <workspace/name>)")
    cfg = initialized_config()
    check_proxy(cfg, proxy)
    info = daemon_request(cfg, "POST", "/api/browser-devices", {"name": name, "proxy_label": proxy,
                                                                "from_lane": from_lane, "note": note,
                                                                "confirm": yes}, absent_ok=True,
                          person=yes)  # --yes puts a second computer on accounts' exit: a person's call
    if not isinstance(info, dict):  # agentosd is not running: in this process, under the same rules
        info = run_manager(lambda manager: manager.create_device(name, proxy_label=proxy, from_lane=from_lane,
                                                                 note=note, confirm=yes, by="owner"))
    typer.echo(f"Created device {info['name']} (proxy {info['proxy'] or 'none'}).")
    if from_lane:
        typer.echo(f"  It took {from_lane}'s fingerprint, so {from_lane} stays the computer its site knows.")
        typer.echo(f"  Add the company's other lanes: agentos lane create <workspace/name> --device {name}")
    else:
        typer.echo(f"  Add lanes: agentos lane create <workspace/name> --device {name}. The first launch pins it.")


@device_app.command("list")
def device_list(as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False) -> None:
    """Devices on this node, their proxy and the lanes on each."""

    async def devices(manager):
        return manager.devices()

    found = run_manager(devices)
    if as_json:
        typer.echo(json.dumps(found, indent=1))
        return
    if not found:
        typer.echo("No devices. Make one per company: agentos device create <name> --proxy <label> "
                   "(docs/PROXIES.md).")
        return
    rows = [("DEVICE", "PROXY", "PINNED", "LANES")]
    for d in found:
        rows.append((d["name"], d["proxy"] or "-", "yes" if d["pinned"] else "no",
                     ", ".join(lane["id"] for lane in d["lanes"]) or "-"))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())


@device_app.command("show")
def device_show(
    name: Annotated[str, typer.Argument(help="Device name.")],
    offline: Annotated[bool, typer.Option("--offline", help="Do not probe the proxy (no network at all).")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """One device: its fixed browser fingerprint, its lanes, and its proxy's health (the proxy answers, its
    exit is where it was, and that place matches the device's fingerprint)."""
    from agentos.stealth.lanecheck import LaneChecker, LaneReport, format_report

    async def show(manager):
        return manager.device_info(name)

    info = run_manager(show)
    cfg = initialized_config()
    try:
        report = asyncio.run(LaneChecker(cfg, network=not offline, write_cache=False).check_device(name))
        health = report.to_dict()
    except AgentosError as exc:
        raise fail_coded(exc.code, exc.message) from None
    if as_json:
        typer.echo(json.dumps({**info, "health": health}, indent=1))
        return
    typer.echo(f"Device {info['name']}" + (f" ({info['note']})" if info.get("note") else ""))
    typer.echo(f"  proxy  {info['proxy'] or 'none'}")
    typer.echo(f"  pin    {_show_pin(info['pin'])}")
    if info.get("from_lane"):
        typer.echo(f"  from   {info['from_lane']}")
    typer.echo("  lanes  " + (", ".join(f"{lane['id']} ({lane['state']})" for lane in info["lanes"]) or "none"))
    typer.echo("")
    typer.echo(format_report(LaneReport.from_dict(health), color=sys.stdout.isatty()))


@device_app.command("remove")
def device_remove(
    name: Annotated[str, typer.Argument(help="Device name.")],
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask.")] = False,
) -> None:
    """Remove a device no lane uses. Its pin moves to AGENTOS_HOME/trash, nothing is erased."""
    if not yes:
        typer.confirm(f"Remove device {name}? Its pin moves to the trash folder.", abort=True)
    done = daemon_request(initialized_config(), "DELETE", f"/api/browser-devices/{name}", absent_ok=True)
    if isinstance(done, dict):
        typer.echo(f"Removed device {name}." + (f" Its files are in {done['trash']}." if done.get("trash") else ""))
        return
    moved = run_manager(lambda manager: manager.remove_device(name, by="owner"))
    typer.echo(f"Removed device {name}." + (f" Its files are in {moved}." if moved else ""))


SITES_ACTIONS = ("show", "add", "remove", "clear", "suggest", "strict", "observe")


@lane_app.command("sites")
def lane_sites(
    lane_id: LaneIdArg,
    action: Annotated[str, typer.Argument(help="show, add, remove, clear, suggest, strict or observe.")] = "show",
    values: Annotated[list[str] | None, typer.Argument(
        help="Domain patterns (instagram.com, *.instagram.com, frame:www.google.com, accounts.google.@cc), a "
             "platform for suggest, on/off for strict, or on <minutes>/off for observe.",
        show_default=False)] = None,
) -> None:
    """The lane's site list: the only hosts its pages may load documents from (every navigation, redirect,
    frame and popup, checked on the node). Empty means no restriction on this lane. `frame:<host>` allows a
    host only inside a frame (a captcha), never as a page; `accounts.google.@cc` is that host on any of
    Google's country domains. `suggest` without a platform lists the suggested lists; with one, it adds
    that list (with the sign-in and captcha hosts every login can need). `strict on` also blocks refused
    requests before they leave while an agent acts, at the cost of the HTTP cache for that moment. While
    agentosd runs, a change reaches a running lane at once. `observe on <minutes>` (default 60, at most
    240) records every non-GET request of the lane by its shape (its host, method, path, and the names of
    its operation and fields, never a value), so you can check the lane's rules against a throwaway
    account's real traffic."""
    from agentos.node import egress

    if action not in SITES_ACTIONS:
        raise fail(f"unknown action {action!r}; use one of: {', '.join(SITES_ACTIONS)}")
    values = list(values or [])

    async def current(manager):
        return manager.get(lane_id)

    if action == "show":
        if values:
            raise fail("show takes no values")
        info = run_manager(current)
        show_sites(lane_id, info.spec.sites, info.spec.egress_strict, egress)
        return
    if action == "observe":
        if not values or values[0] not in ("on", "off") or len(values) > 2 or (values[0] == "off" and len(values) > 1):
            raise fail("observe takes on [minutes] or off")
        run_manager(current)  # the lane exists
        lane_dir = initialized_config().lane_dir(lane_id)
        lane_dir.mkdir(parents=True, exist_ok=True)
        try:
            minutes = int(values[1]) if len(values) > 1 else 60
            until = egress.write_observe(lane_dir / egress.OBSERVE_NAME, minutes if values[0] == "on" else None)
        except ValueError as e:
            raise fail("observe takes a whole number of minutes") from e
        except InvalidRequest as e:
            raise fail(e.message) from e
        if until is None:
            typer.echo(f"Lane {lane_id}: not observing requests.")
        else:
            at = datetime.fromtimestamp(until, UTC).isoformat(timespec="minutes")
            typer.echo(f"Lane {lane_id}: every non-GET request is traced by its shape until {at} "
                       f"(a running lane notices within two seconds).")
        return
    if action == "suggest" and not values:
        typer.echo("Suggested site lists (apply one with `agentos lane sites "
                   f"{lane_id} suggest <platform>`; nothing applies them on its own):")
        for platform, sites in egress.SUGGESTED_SITES.items():
            typer.echo(f"  {platform}: {', '.join(sites)}")
        return
    if action == "add":
        if not values:
            raise fail("add needs one or more domain patterns, for example instagram.com *.instagram.com")
        body: dict = {"op": "add", "sites": values}
    elif action == "remove":
        if not values:
            raise fail("remove needs the patterns to remove")
        body = {"op": "remove", "sites": values}
    elif action == "clear":
        body = {"op": "set", "sites": []}
    elif action == "suggest":
        if len(values) != 1 or values[0] not in egress.SUGGESTED_SITES:
            raise fail(f"suggest takes one platform: {', '.join(egress.SUGGESTED_SITES)}")
        body = {"op": "add", "sites": list(egress.SUGGESTED_SITES[values[0]])}
    else:  # strict
        if len(values) != 1 or values[0] not in ("on", "off"):
            raise fail("strict takes on or off")
        body = {"op": "add", "sites": [], "strict": values[0] == "on"}
    sites, strict, running = change_lane_sites(lane_id, body)
    show_sites(lane_id, sites, strict, egress, trace=False)
    if running:
        typer.echo("The running lane has the new list now.")


@lane_app.command("gate-log")
def lane_gate_log(
    lane_id: LaneIdArg,
    since: Annotated[str | None, typer.Option("--since", help="Only request shapes seen since then: 30m, "
                                                              "24h, 7d or an ISO date.")] = None,
    allow_read: Annotated[str | None, typer.Option("--allow-read", metavar="SHAPE",
                                                   help="Add a shape to the node's read list (it publishes "
                                                        "nothing).")] = None,
    as_commit: Annotated[str | None, typer.Option("--as-commit", metavar="NAME",
                                                  help="Make --shape a named commit rule, like "
                                                       "post.media.")] = None,
    shape: Annotated[str | None, typer.Option("--shape", help="The request shape that --as-commit makes a "
                                                              "rule.")] = None,
    remove: Annotated[str | None, typer.Option("--remove", metavar="SHAPE", help="Undo a promotion.")] = None,
    all_lanes: Annotated[bool, typer.Option("--all-lanes", help="Promote for every lane of the node, not this "
                                                                "lane only.")] = False,
    observed: Annotated[bool, typer.Option("--observed", help="Also list what observe mode saw that no "
                                                              "commit or write rule matched (add those with "
                                                              "--as-commit only).")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """What the commit gate (the check that holds a publish until a person approves exactly what it publishes)
    classified without a rule while agents drove this lane: each request shape (its host, method, path, and
    the names of its operation and fields, never a value), with how it was classified and how often.
    `--allow-read <shape>` adds a shape that publishes nothing to the node's read list; `--as-commit <name>
    --shape <shape>` makes it a named commit rule (the gate files it under that name). Either one applies
    to exactly that host, path and method, on this lane only unless `--all-lanes`. Stored on this node
    (AGENTOS_HOME/gate-shapes.json), each change written to the action log; every lane's gate reads it
    within seconds. `--observed` adds what observe mode saw that no commit or write rule matched (a publish
    the gate takes for a read): name one with `--as-commit`. The owner only."""
    from agentos.node import gateshapes

    chosen = [x for x in (allow_read, as_commit, remove) if x]
    if len(chosen) > 1:
        raise fail("use one of --allow-read, --as-commit or --remove")
    if (as_commit is None) != (shape is None):
        raise fail("--as-commit NAME goes with --shape SHAPE")
    if all_lanes and not (allow_read or as_commit):
        raise fail("--all-lanes goes with --allow-read or --as-commit")
    body: dict | None = None
    if allow_read:
        body = {"allow_read": allow_read, "all_lanes": all_lanes}
    elif as_commit:
        body = {"as_commit": as_commit, "shape": shape, "all_lanes": all_lanes}
    elif remove:
        body = {"remove": remove}
    cfg = initialized_config()
    path = lane_api_path(lane_id, "gate-log")
    try:
        if body is None:
            params = {k: v for k, v in (("since", since), ("observed", "1" if observed else None)) if v}
            query = "?" + urllib.parse.urlencode(params) if params else ""  # a zone's + is not a space
            done = daemon_request(cfg, "GET", path + query, absent_ok=True)
            if not isinstance(done, dict):
                run_manager(lambda manager: _lane_exists(manager, lane_id))
                promoted = gateshapes.load(gateshapes.store_path(cfg.home))
                rows = gateshapes.trace_rows(cfg.lane_dir(lane_id))
                done = {"lane": lane_id, "promoted": promoted,
                        "shapes": gateshapes.log(rows, since=gateshapes.since_of(since), promoted=promoted,
                                                 observed=observed),
                        "identities": gateshapes.identities(rows, since=gateshapes.since_of(since))}
        else:
            done = daemon_request(cfg, "POST", path, body, absent_ok=True, person=True)
            if not isinstance(done, dict):
                done = _gate_shape_locally(cfg, lane_id, body)
    except AgentosError as e:
        raise fail(e.message) from e
    if as_json:
        typer.echo(json.dumps(done, indent=2, default=str))
    elif body is None:
        show_gate_log(done, since)
    else:
        entry = done.get("promoted") or done.get("removed") or {}
        what = "the read allowlist" if entry.get("as") == "read" else f"commit rule {entry.get('rule')}"
        verb = "Removed" if "removed" in done else "Promoted"
        where = "" if verb == "Removed" else f" {gateshapes.scope(entry.get('lanes'))}"
        typer.echo(f"{verb} {entry.get('method')} {entry.get('host')}{entry.get('path')}"
                   f"{' ' + entry['op'] if entry.get('op') else ''} {'from' if verb == 'Removed' else 'to'} {what}"
                   f"{where}. Every lane's gate reads it within seconds; the action log has the change.")


@lane_app.command("rules")
def lane_rules(
    lane_id: Annotated[str | None, typer.Argument(help="One lane (workspace/name); every lane when left out.")] = None,
    stale_days: Annotated[float, typer.Option("--stale-days", help="Flag a verified rule quiet this many days while "
                                                                   "its site sends unknown writes.")] = 14,
    show_all: Annotated[bool, typer.Option("--all", help="List every unknown request shape, not only those "
                                                         "that look like writes.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Rule health: when each commit rule (a publish) and write rule (a change that needs only draft
    permission) last matched on this node; the request shapes no rule covers that each site sent (gated,
    let through or observed); and what looks like drift: a verified rule that went quiet while its site
    sends unknown requests of the same kind, a burst of new unknown writes, a lane on a site with no rules
    (its publishes are not gated). Read from the lanes' request logs (names and shapes only, never a
    value). Fix a stale rule without a code change: `agentos lane gate-log <lane> --as-commit
    <kind>.<detail> --shape <id>`."""
    from agentos.node import rulehealth

    if lane_id is not None:
        valid_lane_id(lane_id)
    cfg = initialized_config()

    async def specs(manager):
        return [info.spec for info in manager.list()]

    done = rulehealth.for_node(cfg.home, run_manager(specs), lane=lane_id, stale_days=stale_days)
    if as_json:
        typer.echo(json.dumps(done, indent=2, default=str))
        return
    show_rule_health(done, lane_id, show_all=show_all)


def show_rule_health(done: dict, lane_id: str | None, *, show_all: bool = False) -> None:
    where = f"lane {lane_id}" if lane_id else "this node"
    since = done.get("since")
    typer.echo(f"Effect rules on {where}, from the egress traces" + (f" (since {since})." if since else
                                                                      " (no trace rows yet)."))
    flags = done.get("flags") or []
    if flags:
        typer.echo("Needs you:")
        for f in flags:
            typer.echo(f"  ! {f['message']}")
    else:
        typer.echo("No drift seen: no quiet verified rule with unknown writes on its site, and no lane on a site "
                   "without rules.")
    typer.echo("Commit and write rules (last matched, matches in the traces):")
    rules = sorted(done.get("rules") or [], key=lambda r: (r["site"], not (r.get("verified") or r.get("owner")),
                                                           r["id"]))
    width = max((len(r["id"]) for r in rules), default=10)
    for r in rules:
        state = (f"last {r['last']}  x{r['count']}  {', '.join(r['lanes'])}" if r.get("last")
                 else "no match in the traces")
        mark = "owner" if r.get("owner") else "verified" if r.get("verified") else "unverified"
        typer.echo(f"  {r['id'].ljust(width)}  {r['effect']:<6}  {mark:<10}  {state}")
    shapes = [e for e in done.get("unknown") or [] if show_all or e.get("write_like")]
    if shapes:
        typer.echo("Shapes no rule names" + ("" if show_all else " that look like writes (--all lists every one)")
                   + ":")
        for e in shapes[:40]:
            decided = ", ".join(f"{k} {v}" for k, v in e.get("decisions", {}).items())
            kind = f"  like a {e['kind']}" if e.get("kind") else ""
            promoted = "  [promoted]" if e.get("promoted") else ""
            typer.echo(f"  {e['id']}  {e['site']}  {e['method']} {e['host']}{e['path']}"
                       f"{'  op ' + e['op'] if e.get('op') else ''}  x{e['count']} ({decided}){kind}{promoted}")
            typer.echo(f"      seen {e.get('first')} to {e.get('last')} on {', '.join(e.get('lanes') or [])}")
        if len(shapes) > 40:
            typer.echo(f"  and {len(shapes) - 40} more (--json lists them all)")


@lane_app.command("pace")
def lane_pace(
    lane_id: Annotated[str | None, typer.Argument(help="workspace/name, for example biz-a/instagram.")] = None,
    preset: Annotated[bool, typer.Option("--preset", help="Apply the recommended pace for a new account: "
                                                          "08:00-22:00 on the lane's clock, 45 minutes "
                                                          "apart, 3 a day.")] = False,
    hours: Annotated[list[str] | None, typer.Option(
        "--hours", help="Waking hours on the lane's clock, like 08:00-22:00, 'mon-fri 09:00-18:00' or "
                        "22:00-02:00 (repeat it for several windows).")] = None,
    gap: Annotated[int | None, typer.Option("--gap", help="At least this many minutes between posts.")] = None,
    per_day: Annotated[int | None, typer.Option("--per-day", help="Posts a lane-local day.")] = None,
    timezone: Annotated[str | None, typer.Option(
        "--timezone", help="A time zone, like America/New_York (the IANA name): overrides the lane's fixed "
                           "zone, or with --workspace sets the default for its lanes that have none. 'auto' "
                           "removes a lane's; 'off' removes a workspace's.")] = None,
    workspace: Annotated[str | None, typer.Option(
        "--workspace", help="A workspace's default timezone: shows it, or sets it with --timezone.")] = None,
    off: Annotated[bool, typer.Option("--off", help="Remove the lane's pace: it is not paced.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """A lane's posting pace: its waking hours, read on the lane's own clock (its proxy's region from its
    fixed browser fingerprint, unless you set a timezone), the gap between posts and the posts a day. A
    person driving the lane by hand is never paced; runs and agents are, including runs you start. A
    publish outside those hours is refused (lane_paced), whatever is approved; a queued or scheduled run
    moves to the next allowed time. Without options, shows the pace, whose clock it reads and when the lane
    may post next. Off for every lane until you set one."""
    if workspace is not None:
        if lane_id or preset or hours or gap is not None or per_day is not None or off:
            raise fail("--workspace goes with --timezone only")
        cfg = initialized_config()
        path = f"/api/pace/workspaces/{workspace}"
        if timezone is None:  # show it: never change a default the owner did not name
            done = daemon_request(cfg, "GET", path, absent_ok=True)
            if not isinstance(done, dict):
                done = _pace_locally(cfg, lambda svc: {"workspace": workspace,
                                                       "timezone": svc.pace.workspace_zone(workspace)})
        else:
            zone = None if timezone.strip().lower() in ("off", "auto", "") else timezone
            done = daemon_request(cfg, "PUT", path, {"timezone": zone}, absent_ok=True)
            if not isinstance(done, dict):
                done = _pace_locally(cfg, lambda svc: svc.set_workspace_zone(workspace, zone, by="owner:cli"))
        hint = "" if timezone is not None else (" Set it with --timezone America/New_York, or remove it with "
                                                "--timezone off.")
        typer.echo(json.dumps(done, indent=2) if as_json else
                   f"Workspace {workspace}: lanes with no pin and no timezone of their own read "
                   f"{done.get('timezone') or 'the node zone'}.{hint}")
        return
    if not lane_id:
        raise fail("name a lane (workspace/name), or --workspace with --timezone")
    valid_lane_id(lane_id)
    changes = [preset, bool(hours), gap is not None, per_day is not None, timezone is not None, off]
    if preset and (hours or gap is not None or per_day is not None):
        raise fail("--preset sets hours, gap and posts a day; leave the others out, or set them without it")
    if off and any(changes[:5]):
        raise fail("--off removes the pace; leave the other options out")
    cfg = initialized_config()
    path = lane_api_path(lane_id, "pace")
    body: dict | None = None
    if any(changes):
        current = daemon_request(cfg, "GET", path, absent_ok=True)
        if not isinstance(current, dict):
            current = _pace_locally(cfg, lambda svc: svc.lane_pace(lane_id))
        pace = dict(current.get("pace") or {})
        if preset:
            pace.update(current.get("preset") or {})
        if hours:
            pace["active_hours"] = hours
        if gap is not None:
            pace["min_gap_minutes"] = gap
        if per_day is not None:
            pace["commits_per_day"] = per_day
        if timezone is not None:
            pace["timezone"] = None if timezone in ("auto", "") else timezone
        body = {"pace": None if off else {k: v for k, v in pace.items() if v is not None} or None}
    try:
        if body is None:
            done = daemon_request(cfg, "GET", path, absent_ok=True)
            if not isinstance(done, dict):
                done = _pace_locally(cfg, lambda svc: svc.lane_pace(lane_id))
        else:
            done = daemon_request(cfg, "PUT", path, body, absent_ok=True, person=True)
            if not isinstance(done, dict):
                done = _pace_locally(cfg, lambda svc: svc.set_lane_pace(lane_id, body["pace"], by="owner:cli"))
    except AgentosError as e:
        raise fail(e.message) from e
    if as_json:
        typer.echo(json.dumps(done, indent=2, default=str))
    else:
        show_pace(done)


def _pace_locally(cfg: NodeConfig, fn):
    """The pace with agentosd stopped: the policy service on its own database."""
    from pydantic import ValidationError

    from agentos.policy.service import PolicyService

    svc = PolicyService(cfg.home, node_config=cfg)
    try:
        return fn(svc)
    except ValidationError as e:
        raise fail(str(e.errors()[0].get("msg"))) from None
    except ValueError as e:
        raise fail(str(e)) from None
    except AgentosError as e:
        raise fail(e.message) from e
    finally:
        svc.close()


def show_pace(p: dict) -> None:
    zone = p.get("zone") or {}
    pace = p.get("pace") or {}
    typer.echo(f"{p['lane']}: lane time is {zone.get('timezone')} ({zone.get('source_words')}), "
               f"now {str(p.get('local_now', ''))[11:16]}")
    if zone.get("note"):
        typer.echo(f"  note: {zone['note']}")
    if not p.get("paced"):
        typer.echo("  not paced: agents may publish at any hour (`agentos lane pace <lane> --preset` sets "
                   "08:00-22:00, 45 minutes apart, 3 a day)")
        return
    parts = []
    if pace.get("active_hours"):
        parts.append("waking hours " + ", ".join(pace["active_hours"]))
    if pace.get("min_gap_minutes"):
        parts.append(f"at least {pace['min_gap_minutes']} minutes apart")
    if pace.get("commits_per_day") is not None:
        parts.append(f"{pace['commits_per_day']} a day")
    typer.echo("  pace: " + "; ".join(parts))
    last = p.get("last_post_at")
    typer.echo(f"  posts today: {p.get('posts_today', 0)}" + (f" (last {last})" if last else ""))
    if p.get("may_post_now"):
        typer.echo("  may post now")
    else:
        typer.echo(f"  next allowed: {p.get('next_allowed_local') or 'none in the next week'} lane time"
                   + (f", {p['next_allowed_at']}" if p.get("next_allowed_at") else ""))


async def _lane_exists(manager, lane_id: str):
    return manager.get(lane_id)


def _gate_shape_locally(cfg: NodeConfig, lane_id: str, body: dict) -> dict:
    """A gate-log change with agentosd stopped: the file, and the audit row in the action log."""
    from agentos.node import gateshapes
    from agentos.policy.service import PolicyService

    run_manager(lambda manager: _lane_exists(manager, lane_id))
    by = "owner:cli"
    if "remove" in body:
        entry, removed = gateshapes.remove(cfg.home, body["remove"]), True
    elif "allow_read" in body:
        entry, removed = gateshapes.promote(cfg.home, cfg.lane_dir(lane_id), body["allow_read"], as_="read",
                                            lane=lane_id, by=by, all_lanes=bool(body.get("all_lanes"))), False
    else:
        entry, removed = gateshapes.promote(cfg.home, cfg.lane_dir(lane_id), body["shape"], as_="commit",
                                            name=body["as_commit"], lane=lane_id, by=by,
                                            all_lanes=bool(body.get("all_lanes"))), False
    policy = PolicyService(cfg.home, node_config=cfg)
    try:
        gateshapes.audit(policy.traces, entry, lane=lane_id, by=by, removed=removed)
        policy.traces.flush()
    finally:
        with contextlib.suppress(Exception):
            policy.traces.close()
    return {"lane": lane_id, "removed" if removed else "promoted": entry}


def show_gate_log(done: dict, since: str | None) -> None:
    from agentos.node import gateshapes

    lane_id, shapes = done.get("lane"), done.get("shapes") or []
    if not shapes:
        typer.echo(f"Lane {lane_id}: the commit gate classified no shape without a rule"
                   f"{' since ' + since if since else ''}.")
    else:
        typer.echo(f"Lane {lane_id}: what the commit gate classified without a rule"
                   f"{' since ' + since if since else ''} (one row a second at most per shape):")
    for e in shapes:
        decided = ", ".join(f"{k} {v}" for k, v in e.get("decisions", {}).items())
        promoted = (f"  [promoted: {e['promoted']} {gateshapes.scope(e.get('promoted_lanes'))}]"
                    if e.get("promoted") else "")
        typer.echo(f"  {e['id']}  {e['method']} {e['host']}{e['path']}{'  op ' + e['op'] if e.get('op') else ''}"
                   f"  x{e['count']} ({decided}){promoted}")
        for why, n in e.get("why", {}).items():
            typer.echo(f"      why: {why} ({n})")
        if e.get("doc_ids"):
            typer.echo(f"      doc_ids: {', '.join(e['doc_ids'])}")
        if e.get("observed_only"):
            typer.echo("      seen in observe mode only (the gate let it go as a read or never drove it): "
                       "--as-commit names it, a read promotion does not apply")
        if e.get("mixed"):
            typer.echo(f"      mixes {e['field_sets']} bodies on one path (field sets): it cannot be promoted as a read "
                       "without an operation")
        if e.get("fields"):
            typer.echo(f"      fields: {', '.join(e['fields'][:30])}{' ...' if len(e['fields']) > 30 else ''}")
        typer.echo(f"      seen {e.get('first')} to {e.get('last')}")
    promoted = done.get("promoted") or []
    if promoted:
        typer.echo("Promoted on this node:")
        for p in promoted:
            what = "read" if p.get("as") == "read" else p.get("rule")
            typer.echo(f"  {p['id']}  {p['method']} {p['host']}{p['path']}{'  op ' + p['op'] if p.get('op') else ''}"
                       f"  -> {what} {gateshapes.scope(p.get('lanes'))}  (by {p.get('by')}, {p.get('at')})")
    if shapes:
        typer.echo("Promote one: --allow-read <shape> (it publishes nothing) or --as-commit <name> --shape <shape>.")
    found = done.get("identities") or []
    if found:
        typer.echo("Publishes that acted as an identity agentos does not know (no surface names it):")
        for e in found:
            who = e.get("id") or "(unreadable)"
            where = f" in {e['field']}" if e.get("field") else ""
            flow = ", a flow refused" if e.get("flow") else ""
            typer.echo(f"  {e.get('site')} {who}{where}  x{e.get('count')} ({', '.join(e.get('rules') or [])}{flow})")
            if e.get("why"):
                typer.echo(f"      why: {e['why']}")
            typer.echo(f"      seen {e.get('first')} to {e.get('last')}")
        typer.echo("If one is a Page or company page you manage, add it as a surface (`agentos connector bind`, or POST /api/surfaces).")


def change_lane_sites(lane_id: str, body: dict) -> tuple[list[str], bool, bool]:
    """Through the running agentosd, so a warm lane's guard has the new list at once; when it is not
    running (or predates the route), in the store, which the next lane start reads. -> (sites,
    strict, applied to a running lane)."""
    from agentos.node.viewer_app import change_sites

    cfg = initialized_config()
    done = daemon_request(cfg, "POST", lane_api_path(lane_id, "sites"), body, absent_ok=True, person=True)
    if isinstance(done, dict):
        return list(done.get("sites") or []), bool(done.get("strict")), bool(done.get("running"))
    result = run_manager(lambda manager: change_sites(manager, lane_id, body, by="owner"))
    return result["sites"], result["strict"], False


def show_sites(lane_id: str, sites: list[str], strict: bool, egress, *, trace: bool = True) -> None:
    from agentos.node import rulehealth

    if sites:
        typer.echo(f"Lane {lane_id} loads documents only from: {', '.join(sites)}")
        unruled = [e for e in sites if not e.startswith(egress.FRAME_PREFIX)
                   and not rulehealth.covered(e, egress.EFFECT_RULES)]
        if unruled:
            typer.echo(f"No effect rules for: {', '.join(unruled)}. Publishes there are not gated (no approval is "
                       "asked); their non-GET requests are traced by shape (agentos lane rules).")
    else:
        typer.echo(f"Lane {lane_id} has no site list: no lane restriction (grants still apply).")
    typer.echo(f"Strict egress: {'on' if strict else 'off'}.")
    until = egress.read_observe(initialized_config().lane_dir(lane_id) / egress.OBSERVE_NAME)
    if until is not None:
        typer.echo(f"Observing requests until {datetime.fromtimestamp(until, UTC).isoformat(timespec='minutes')}.")
    if not trace:
        return
    rows = egress.EgressTrace(initialized_config().lane_dir(lane_id) / egress.TRACE_NAME).read(limit=10)
    if rows:
        typer.echo("Recent egress records:")
        for row in rows:
            kind = row.get("kind")
            what = (row.get("rule") if kind == "blocked" else "observed commit" if kind == "observed_commit"
                    else f"observed {(row.get('shape') or {}).get('op') or 'request'}" if kind == "observed_request"
                    else kind)
            typer.echo(f"  {row.get('at', '?')}  {what}  {row.get('method', '')} {row.get('host', '?')}"
                       f"{row.get('path', '')}")


# ---- lane control (through the running daemon) ----

DAEMON_TIMEOUT = 15.0


def daemon_url(cfg: NodeConfig, path: str) -> str:
    host = cfg.mcp_host if cfg.mcp_host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{cfg.mcp_port}{path}"


def lane_api_path(lane_id: str, action: str) -> str:
    workspace, name = parse_lane_id(lane_id)
    return f"/api/lanes/{workspace}/{name}/{action}"


def _send(method: str, url: str, *, body: dict | None, headers: dict, timeout: float = DAEMON_TIMEOUT):
    """One HTTP request to agentosd (tests route it to the app in-process)."""
    import httpx

    return httpx.request(method, url, json=body, timeout=timeout, headers=headers)


def call_daemon(cfg: NodeConfig, path: str, body: dict) -> dict:
    """POST to the running agentosd with the node token; turn every failure into a clear exit."""
    return daemon_request(cfg, "POST", path, body,
                          missing="this agentosd has no lane control API yet; update it and restart the service")


def operator_presence(cfg: NodeConfig) -> dict[str, str]:
    """The header that makes one request to agentosd a person's (docs/THREAT-MODEL.md): a one-time code
    this CLI writes into panel.db, which only the node's operator can write, as `agentos panel login`
    does. The node token alone is not a person, because an agent may hold it. {} when panel.db is not
    writable here (then the daemon refuses what needs a person, and says so)."""
    from agentos.api.auth import PRESENCE_HEADER, PanelStore, new_login_code

    code = new_login_code()
    try:
        PanelStore.for_home(cfg.home).add_code(code, ttl_seconds=60)
    except (OSError, sqlite3.Error):
        return {}
    return {PRESENCE_HEADER: code}


def daemon_request(cfg: NodeConfig, method: str, path: str, body: dict | None = None, *,
                   missing: str = "this agentosd does not know this route yet; update it and restart the service",
                   absent_ok: bool = False, timeout: float | None = None, person: bool = False):
    """Call the running agentosd's API with the node token; every failure becomes a clear exit.
    `absent_ok`: None when agentosd is not running or does not know the route (the caller falls
    back to the store). `person`: a route that needs a person (Presence.PERSON) gets a one-time
    code from panel.db with it (`operator_presence`)."""
    import httpx

    url = daemon_url(cfg, path)
    headers = {"Authorization": f"Bearer {cfg.mcp_token}", **(operator_presence(cfg) if person else {})}
    try:
        response = _send(method, url, body=body, headers=headers,
                         **({"timeout": timeout} if timeout else {}))
    except httpx.ConnectError:
        if absent_ok:
            return None
        raise fail(f"agentosd is not running on {daemon_url(cfg, '')}. Start it with `agentos node serve` "
                   "(or `systemctl start agentosd`), then try again.") from None
    except httpx.TimeoutException:
        raise fail(f"agentosd did not answer within {timeout or DAEMON_TIMEOUT:g}s") from None
    except httpx.HTTPError as exc:
        raise fail(f"could not reach agentosd: {type(exc).__name__}") from None
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code == 401:
        raise fail("agentosd refused the node token. Check that AGENTOS_HOME points at this node's home.")
    error = data.get("error") if isinstance(data, dict) else None
    if absent_ok and response.status_code in (404, 405) and (
            not isinstance(error, dict) or error.get("code") in (None, "not_found")):
        return None  # an agentosd from before this route
    if isinstance(error, dict):
        fix = error.get("fix") if isinstance(error.get("fix"), str) else None
        raise fail_coded(str(error.get("code") or "error"), str(error.get("message") or ""), fix)
    if response.status_code == 404:
        raise fail(missing)
    if response.status_code >= 400 or not isinstance(data, dict | list):
        raise fail(f"agentosd answered {response.status_code}")
    return data


def control_action(lane_id: str, action: str, note: str | None = None) -> tuple[dict, bool, NodeConfig]:
    cfg = initialized_config()
    body = {"action": action, **({"note": note} if note else {})}
    # a person's (a one-time code in panel.db beside the token): hand back and taking a held lane need one
    lane = daemon_request(cfg, "POST", lane_api_path(lane_id, "control"), body, person=True,
                          missing="this agentosd has no lane control API yet; update it and restart the service")
    return lane, bool(lane.get("changed", True)), cfg


@lane_app.command("take")
def lane_take(lane_id: LaneIdArg) -> None:
    """Take control of a lane: agents stop acting on it until you hand it back."""
    _, changed, cfg = control_action(lane_id, "take")
    if not changed:
        typer.echo(f"You already have control of {lane_id}.")
        return
    minutes = max(1, round(cfg.control_idle_seconds / 60))
    typer.echo(f"You have control of {lane_id}. Agents on it now get human_in_control.")
    typer.echo(f"Watch and drive it with `agentos lane view {lane_id} --control`. Hand it back with "
               f"`agentos lane handback {lane_id}`.")
    if cfg.control_idle_seconds > 0:
        typer.echo(f"Control goes back to agents after {minutes} min without input in the viewer.")


@lane_app.command("handback")
def lane_handback(
    lane_id: LaneIdArg,
    note: Annotated[str | None, typer.Option(
        "--note", help="What to tell the agents, for example \"I fixed the address; post it now\" "
                       "(at most 300 characters). They see it once, with what changed.")] = None,
) -> None:
    """Hand a lane back to agents (also resumes a stopped agent and clears a pending request). The
    next thing an agent sees on the lane says what changed while you had it."""
    if note is not None and len(note) > 300:
        raise fail("--note is at most 300 characters")
    lane, changed, _ = control_action(lane_id, "hand_back", note)
    if lane.get("warning"):
        typer.echo(lane["warning"])
        return
    typer.echo(f"Handed {lane_id} back to agents." if changed else f"Agents already have control of {lane_id}.")


@lane_app.command("stop")
def lane_stop(lane_id: LaneIdArg) -> None:
    """Stop the agent on a lane: its runs there are cancelled and nothing acts until you hand it back."""
    _, changed, _ = control_action(lane_id, "stop_agent")
    if changed:
        typer.echo(f"Stopped agents on {lane_id}. Nothing runs there until you hand it back with "
                   f"`agentos lane handback {lane_id}`.")
    else:
        typer.echo(f"Agents on {lane_id} are already stopped.")


@lane_app.command("deny")
def lane_deny(lane_id: LaneIdArg) -> None:
    """Refuse an agent's request for control back. You keep control."""
    _, changed, _ = control_action(lane_id, "deny_request")
    typer.echo(f"Refused the agent's request for {lane_id}. You keep control." if changed
               else f"No agent is asking for {lane_id} right now.")


LOGIN_TIMEOUT = 120.0   # a login step types, presses and waits for the page (agentos.node.login_assist)
LOGIN_ENDED = ("signed_in", "cancelled", "done", "failed")


def _login_call(cfg: NodeConfig, lane_id: str, action: str = "", body: dict | None = None) -> dict:
    path = lane_api_path(lane_id, "login") + (f"/{action}" if action else "")
    return daemon_request(cfg, "POST", path, body or {}, timeout=LOGIN_TIMEOUT, person=True,  # a person's
                          missing="this agentosd has no assisted login yet; update it and restart the service")


def _login_show(login: dict, shown: dict) -> None:
    """Print what changed since the last print: the step's words, the note, an error."""
    step = login.get("step") or {}
    key = (step.get("id"), login.get("state"))
    if key != shown.get("key"):
        shown["key"] = key
        for text in (step.get("heading"), step.get("message")):
            if text:
                typer.echo(text)
        if step.get("number"):
            typer.secho(f"\n    {step['number']}\n", bold=True)
        if login.get("note"):
            typer.secho(login["note"], fg=typer.colors.CYAN)
    if login.get("error") and login.get("error") != shown.get("error"):
        typer.secho(login["error"], fg=typer.colors.YELLOW, err=True)
    shown["error"] = login.get("error")


def _login_ask(cfg: NodeConfig, lane_id: str, login: dict) -> dict:
    """The step's fields in the terminal (hidden input for secrets), or its buttons as a numbered list."""
    step = login["step"]
    choices = step.get("choices") or []
    if step.get("fields"):
        if choices:
            typer.echo("Other ways to sign in: " + ", ".join(f"[{i}] {c['label']}" for i, c in enumerate(choices, 1)))
            raw = typer.prompt("Press Enter to fill in the form, or pick a number", default="",
                               show_default=False).strip()
            if raw.isdigit() and 1 <= int(raw) <= len(choices):
                return _login_call(cfg, lane_id, "step", {"step": step["id"], "choice": choices[int(raw) - 1]["id"]})
        values: dict[str, str] = {}
        for f in step["fields"]:
            values[f["name"]] = typer.prompt(f["label"], hide_input=bool(f.get("secret")))
        try:
            return _login_call(cfg, lane_id, "step", {"step": step["id"], "values": values})
        finally:
            values.clear()
    for i, c in enumerate(choices, 1):
        typer.echo(f"  [{i}] {c['label']}")
    while True:
        raw = typer.prompt("Pick one (or t to take control, c to cancel)").strip().lower()
        if raw == "c":
            return _login_call(cfg, lane_id, "cancel")
        if raw == "t":
            return _login_call(cfg, lane_id, "take")
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return _login_call(cfg, lane_id, "step", {"step": step["id"], "choice": choices[int(raw) - 1]["id"]})


@lane_app.command("login")
def lane_login(
    lane_id: LaneIdArg,
    url: Annotated[str | None, typer.Option(help="The sign-in page to open. Default: the site's own login page "
                                                 "from the sites table, or the page the lane shows.")] = None,
) -> None:
    """Log a lane in by typing into the terminal: agentos reads each sign-in step, asks you for exactly
    its fields (passwords and codes hidden), and types them into the lane's browser at a person's pace.
    Agents stop on the lane while it runs. Nothing you type is logged or shown to an agent."""
    cfg = initialized_config()
    login = _login_call(cfg, lane_id, "", {"url": url} if url else {})
    shown: dict = {}
    try:
        while True:
            _login_show(login, shown)
            state = login.get("state")
            if state in LOGIN_ENDED:
                break
            if state == "ask":
                login = _login_ask(cfg, lane_id, login)
            elif state == "stopped" or state == "paused":
                typer.echo(f"Take control in the live view (`agentos lane view {lane_id} --control`), then come back.")
                raw = typer.prompt("Press Enter to read the page again, or c to cancel", default="",
                                   show_default=False).strip().lower()
                login = _login_call(cfg, lane_id, "cancel" if raw == "c" else "resume")
            else:  # working, waiting: the node answers when the login changes
                path = lane_api_path(lane_id, "login") + f"?after={login.get('version', 0)}&wait=25"
                login = daemon_request(cfg, "GET", path, timeout=40.0, person=True)
    except (KeyboardInterrupt, typer.Abort):
        typer.echo("")
        login = _login_call(cfg, lane_id, "cancel")
        typer.echo(f"Login on {lane_id} cancelled. Nothing you typed was kept.")
        raise typer.Exit(130) from None
    if login.get("state") == "signed_in" and login.get("can_save"):
        if typer.confirm("Save these to the vault for next time? (the login tool then signs in with no form)",
                         default=True):
            saved = _login_call(cfg, lane_id, "save")
            typer.echo(saved.get("note") or "Saved.")
        else:
            _login_call(cfg, lane_id, "cancel")


@lane_app.command("view")
def lane_view_link(
    lane_id: LaneIdArg,
    control: Annotated[bool, typer.Option("--control", help="A link that can take control, not only watch.")] = False,
    ttl: Annotated[int | None, typer.Option(help="Link lifetime in seconds (default view_token_seconds).",
                                            min=30, max=86400)] = None,
) -> None:
    """Print a signed live-view link for a lane and the SSH tunnel that reaches it."""
    from agentos.node.daemon import ssh_host

    cfg = initialized_config()
    body: dict = {"mode": "control" if control else "view"}
    if ttl is not None:
        body["ttl_s"] = ttl
    link = daemon_request(cfg, "POST", lane_api_path(lane_id, "view-link"), body, person=True,  # the owner's own link
                          missing="this agentosd has no lane control API yet; update it and restart the service")
    port = cfg.mcp_port
    kind = "control link" if control else "view-only link"
    typer.echo(f"Live view of {lane_id} ({kind}, valid until {link.get('expires_at', '?')}):")
    typer.echo(f"  http://localhost:{port}{link.get('url', '')}")
    typer.echo("From your Mac, keep this tunnel open while you use it:")
    typer.echo(f"  ssh -N -L {port}:localhost:{port} {ssh_host(cfg)}")


# ---- members: people who sign in to the panel ----

members_app = typer.Typer(no_args_is_help=True, help="People who sign in to the panel: invite, list, roles, remove.")
app.add_typer(members_app, name="members")

MEMBER_ROLES = ("admin", "approver", "operator", "viewer", "auditor")
ROLE_HELP = ("A role (admin, approver, operator, viewer, auditor), or role@workspace. Repeat it for several; a "
             "bare role applies to every --workspace.")
RoleOpt = Annotated[list[str], typer.Option("--role", "-r", help=ROLE_HELP)]
WorkspaceOpt = Annotated[list[str] | None, typer.Option("--workspace", "-w", help=(
    "A workspace the bare --role values apply to (repeat it; '*' is every workspace)."))]
TtlOpt = Annotated[str | None, typer.Option(help="How long the access lasts: 12h, 90d, 4w (default: until removed).")]
WatchOpt = Annotated[bool, typer.Option("--watch", help="Viewers and approvers may watch the live view too.")]
MaxUsdOpt = Annotated[float | None, typer.Option("--max-usd", min=0, help="Approvers may approve payments up to this.")]


def member_roles(roles: list[str], workspaces: list[str] | None, *, watch: bool, max_usd: float | None) -> list[dict]:
    """--role approver --workspace biz-c (or --role approver@biz-c) as the API's role list."""
    out: list[dict] = []
    for item in roles:
        role, _, scope = item.partition("@")
        role = role.strip().lower()
        if role not in MEMBER_ROLES:
            raise fail(f"{role or item!r} is not a role; use one of {', '.join(MEMBER_ROLES)}")
        scopes = [scope.strip()] if scope else list(workspaces or [])
        if not scopes:
            raise fail(f"say where: --workspace biz-c, or --role {role}@biz-c")
        for ws in scopes:
            options: dict = {}
            if watch and role in ("viewer", "approver"):
                options["watch"] = True
            if max_usd is not None and role == "approver":
                options["max_usd"] = max_usd
            out.append({"scope": ws, "role": role, "options": options})
    return out


def role_words(roles: list[dict]) -> str:
    if not roles:
        return "no roles"
    words = []
    for r in roles:
        where = "every workspace" if r.get("scope") == "*" else r.get("scope")
        extra = []
        opts = r.get("options") or {}
        if opts.get("watch"):
            extra.append("watches")
        if opts.get("max_usd") is not None:
            extra.append(f"pay up to ${opts['max_usd']:g}")
        if r.get("expires_at"):
            extra.append(f"until {str(r['expires_at'])[:10]}")
        words.append(f"{r.get('role')} in {where}" + (f" ({', '.join(extra)})" if extra else ""))
    return ", ".join(words)


def find_member(cfg: NodeConfig, who: str) -> dict:
    for m in daemon_request(cfg, "GET", "/api/members", missing=MEMBERS_MISSING):
        if who in (m.get("handle"), m.get("id")):
            return m
    raise fail(f"no member {who}; `agentos members list` shows who is on this node")


MEMBERS_MISSING = "this agentosd has no members yet; update it and restart the service"


def show_qr(link: str, qr: bool | None) -> None:
    import sys

    from agentos import qr as qrcode

    if qr is False or (qr is None and not sys.stdout.isatty()):
        return
    try:
        typer.echo(qrcode.render(link), color=True)  # black on white even when asked for off a terminal
    except qrcode.QrTooLong:
        typer.echo("(the link is too long for a QR code here; send the link itself)")
    typer.echo("")


@members_app.command("list")
def members_list(as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False) -> None:
    """Who signs in to this node, with their roles, and the invites not used yet."""
    import json

    cfg = initialized_config()
    people = daemon_request(cfg, "GET", "/api/members", missing=MEMBERS_MISSING)
    invites = daemon_request(cfg, "GET", "/api/invites", missing=MEMBERS_MISSING)
    if as_json:
        typer.echo(json.dumps({"members": people, "invites": invites}, indent=2))
        return
    width = max([len(m.get("handle", "")) for m in people] + [6])
    for m in people:
        state = "" if m.get("state") == "active" else f"  [{m.get('state')}]"
        devices = m.get("devices", 0)
        typer.echo(f"{m.get('handle', '?'):<{width}}  {role_words(m.get('roles') or [])}  "
                   f"({devices} passkey{'' if devices == 1 else 's'}){state}")
    if invites:
        typer.echo("\nInvites not used yet:")
        for i in invites:
            typer.echo(f"  {i.get('handle'):<{width}}  {role_words(i.get('roles') or [])}  "
                       f"(link expires {str(i.get('expires_at', ''))[:16].replace('T', ' ')} UTC)")


@members_app.command("invite")
def members_invite(
    handle: Annotated[str, typer.Argument(help="Their short name in Activity: va, jonas, client-ostra.")],
    role: RoleOpt,
    workspace: WorkspaceOpt = None,
    ttl: TtlOpt = None,
    name: Annotated[str | None, typer.Option(help="Their name, shown next to their handle.")] = None,
    email: Annotated[str | None, typer.Option(help="Kept as a note; the node sends no email.")] = None,
    watch: WatchOpt = False,
    max_usd: MaxUsdOpt = None,
    qr: Annotated[bool | None, typer.Option("--qr/--no-qr", help="Print a QR code (default: on a terminal).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the invite as JSON (the link included).")] = False,
) -> None:
    """Invite a person: prints a link that works once, for 24 hours, and a QR code for their phone."""
    import json

    cfg = initialized_config()
    body = {"handle": handle, "roles": member_roles(role, workspace, watch=watch, max_usd=max_usd),
            "display_name": name, "email": email, "ttl": ttl}
    invite = daemon_request(cfg, "POST", "/api/invites", body, missing=MEMBERS_MISSING, person=True)
    if as_json:
        typer.echo(json.dumps(invite, indent=2))
        return
    lasting = f", for {ttl}" if ttl else ""
    typer.echo(f"Invite for {invite.get('handle')}: {role_words(invite.get('roles') or [])}{lasting}.")
    typer.echo("Send them this link. It works once, for 24 hours, and is not shown again:\n")
    typer.echo(f"  {invite.get('link')}\n")
    show_qr(str(invite.get("link")), qr)
    typer.echo("They open it on their phone, make a passkey, and see only what these roles allow.")
    typer.echo("Changed your mind? Revoke it in the panel (Settings, Members).")


@members_app.command("role")
def members_role(
    who: Annotated[str, typer.Argument(help="Their handle (or member id).")],
    role: Annotated[list[str] | None, typer.Option("--role", "-r", help=ROLE_HELP)] = None,
    workspace: WorkspaceOpt = None,
    none: Annotated[bool, typer.Option("--none", help="Take every role away in the --workspace values.")] = False,
    ttl: TtlOpt = None,
    watch: WatchOpt = False,
    max_usd: MaxUsdOpt = None,
) -> None:
    """Change someone's roles in the workspaces named; their other workspaces stay as they are.

    The node re-mints their member grants; the change applies at their next click."""
    cfg = initialized_config()
    if none == bool(role):
        raise fail("give the new roles with --role, or --none with --workspace to take them away")
    roles = [] if none else member_roles(role or [], workspace, watch=watch, max_usd=max_usd)
    scopes = list(dict.fromkeys([*(workspace or []), *(r["scope"] for r in roles)]))
    if not scopes:
        raise fail("say where: --workspace biz-c")
    m = find_member(cfg, who)
    body: dict = {"roles": roles, "scopes": scopes}
    if ttl:
        body["ttl"] = ttl
    updated = daemon_request(cfg, "PATCH", f"/api/members/{m['id']}", body, missing=MEMBERS_MISSING, person=True)
    typer.echo(f"{updated.get('handle')}: {role_words(updated.get('roles') or [])}. It applies at their next click.")


@members_app.command("remove")
def members_remove(
    who: Annotated[str, typer.Argument(help="Their handle (or member id).")],
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask.")] = False,
) -> None:
    """Remove a person: sessions end, passkeys and member grants go, their agents' grants are revoked."""
    cfg = initialized_config()
    m = find_member(cfg, who)
    if m.get("owner"):
        raise fail("the owner cannot be removed")
    if not yes:
        typer.confirm(f"Remove {m.get('handle')} ({role_words(m.get('roles') or [])})? They lose access at once, "
                      "on every device, and the agents they connected stop working", abort=True)
    r = daemon_request(cfg, "DELETE", f"/api/members/{m['id']}", missing=MEMBERS_MISSING)
    agents = len(r.get("agent_grants") or [])
    sessions = r.get("sessions_ended", 0)
    typer.echo(f"Removed {m.get('handle')}: {sessions} session{'' if sessions == 1 else 's'} ended, "
               f"{len(r.get('grants') or [])} member grant(s) and {agents} agent grant(s) revoked, "
               "their push topic retired.")


# ---- feature packages ----
# Each feature package ships a typer `app`. Mounted
# here when installed; a package missing from this build simply has no commands.


def _mount_features() -> None:
    import importlib

    mounts = (
        ("agentos.client.cli", None, None),  # connect, disconnect, nodes, status (on the Mac)
        ("agentos.vault.cli", None, "vault"),
        ("agentos.git.cli", None, "git"),  # git key add|list|remove, git remote allow|list|deny
        ("agentos.policy.cli", None, "policy"),
        ("agentos.policy.cli", "grants_app", "grants"),
        ("agentos.policy.cli", "approvals_app", "approvals"),
        ("agentos.policy.cli", "confirmation_app", "confirmation"),
        ("agentos.audit.cli", None, "audit"),  # verify, replay, export, share (the tamper-evident action log)
        ("agentos.runner.cli", None, "runner"),
        ("agentos.runner.cli", "runs_app", "runs"),
        ("agentos.runner.cli", "flows_app", "flows"),
        ("agentos.runner.cli", "flows_app", "flow"),  # agentos flow pull|push|...: the same group
        ("agentos.runner.cli", "schedules_app", "schedules"),
        ("agentos.runner.connectors.cli", None, "connector"),
        ("agentos.runner.siteapis.cli", None, "site-api"),
        ("agentos.runner.workflows.cli", None, "workflows"),
        ("agentos.runner.mail.cli", None, "mail"),
        ("agentos.agenda.cli", None, "agenda"),  # the agenda: planned actions, their approval and proof
        ("agentos.api.cli", None, "panel"),
        ("agentos.tui.cli", None, None),  # agentos dash
        ("agentos.workspaces.cli", None, "workspace"),
        ("agentos.memory.cli", None, "memory"),  # workspace memory, as the owner in person
        ("agentos.profiles.cli", None, "profile"),  # a person's or business's profile, for all its accounts
        ("agentos.engage.cli", None, "engage"),  # engagement programs, plans and caps (dry run)
        ("agentos.plan.cli", None, "plan"),  # plan status | draft: read-only, across lanes and programs
        ("agentos.brains.cli", None, "brains"),
        ("agentos.plugins.cli", None, "plugins"),  # list | show | enable | disable | assign (a person only)
        ("agentos.pilot.cli", None, "pilot"),  # which model does each pilot job
        ("agentos.env.cli", None, "env"),
        ("agentos.vms.cli", None, "vm"),  # setup, types, list, destroy, status, image build (cloud VMs)
        ("agentos.environments.cli", None, "environments"),  # list, show, close, allow, setup (a computer per chat)
        ("agentos.environments.cli", None, "envs"),  # the same group, shorter
        ("agentos.controller.cli", None, "controller"),  # controller serve | join-token | login | nodes
        ("agentos.webbotauth.cli", None, "identity"),  # init | show | hosts | test-sign (Web Bot Auth)
        ("agentos.freeze.cli", None, "freeze"),  # freeze lane | workspace | node, freeze status
        ("agentos.freeze.cli", "unfreeze_app", "unfreeze"),
    )
    for module_name, attr, command in mounts:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name and module_name.startswith(exc.name):
                continue  # this feature is not part of the build
            raise
        sub = getattr(module, attr or "app", None)
        if isinstance(sub, typer.Typer):
            app.add_typer(sub, name=command)  # no name: its commands join the top level
    _mount_plugin_clis()


def _mount_plugin_clis() -> None:
    """A plugin's own commands, named by its manifest, as `agentos <plugin id>`. They are there
    whether the plugin is on or not, and each one refuses when it is off."""
    import importlib

    try:
        from agentos.plugins.entries import PLUGINS
    except ModuleNotFoundError:
        return
    for plugin in PLUGINS:
        if not plugin.cli:
            continue
        try:
            module = importlib.import_module(plugin.cli)
        except ModuleNotFoundError as exc:
            if exc.name and plugin.cli.startswith(exc.name):
                continue  # the plugin's package is not part of this build
            raise
        sub = getattr(module, "app", None)
        if isinstance(sub, typer.Typer):
            app.add_typer(sub, name=plugin.id)


class _CliVault:
    """The vault for one CLI process, opened the way the daemon opens it: a key-file vault
    unseals itself on first use; a passphrase vault asks for its passphrase (on the terminal,
    so a credential piped to stdin is not read as the passphrase) the first time a secret is
    stored or read."""

    def __init__(self, vault):
        self._vault = vault

    def __getattr__(self, name: str):
        return getattr(self._vault, name)

    async def _unsealed(self) -> None:
        header = self._vault.store.header()
        if header is not None and header.mode == "passphrase" and self._vault.state() == "sealed":
            import getpass

            await self._vault.unseal(getpass.getpass("Vault passphrase: "))

    async def put_secret(self, ref, value, **kwargs):
        await self._unsealed()
        return await self._vault.put_secret(ref, value, **kwargs)

    async def get_secret(self, ref, **kwargs):
        await self._unsealed()
        return await self._vault.get_secret(ref, **kwargs)


def cli_runtime():
    """The services a feature CLI shares with the daemon: the node config, and the vault when
    this node has one (`agentos vault init`). Without a vault, crown-jewel credentials are
    refused unless the command is told to use the plain development file."""
    from agentos.core.runtime import Runtime

    config = load_config()
    runtime = Runtime(config=config)
    try:
        from agentos.vault.service import VaultService
    except ModuleNotFoundError:
        return runtime
    vault = VaultService(config.home)
    if vault.store.header() is not None:
        runtime.register("vault", _CliVault(vault))
    return runtime


def plugins_cli_runtime():
    """cli_runtime plus the brains, so `agentos plugins` can read what each brain can do. Brains
    that cannot be opened read as not available, and every job then reads as unmet."""
    runtime = cli_runtime()
    try:
        from agentos.brains.service import build

        runtime.register("brains", build(runtime))
    except ModuleNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001  (a broken brain store must not hide the plugin list)
        logging.getLogger(__name__).warning("the brains could not be opened: %s", type(exc).__name__)
    return runtime


class PluginChanges:
    """`agentos plugins` on the node's action log, which that package cannot import. It opens on
    first use, so a refused command and a plain `list` never touch the log.

    A row that stays waiting in memory dies with this process, so `record` says so and the change
    is undone. The row can still land when `close` tries once more, and a record of a change that
    did not stand is the safe way round."""

    BUSY_TIMEOUT_MS = 5000  # a person is waiting, and the daemon may be writing

    def __init__(self, home: Path):
        self.home = home
        self.traces: Any = None

    def _open(self) -> Any:
        if self.traces is None:
            from agentos.core.errors import ServiceUnavailable
            from agentos.policy.traces import TraceStore

            path = self.home / "traces.db"
            if os.geteuid() == 0 and not path.exists():  # root would own it, and the daemon could not write
                raise ServiceUnavailable("the action log does not exist yet: start agentosd once, then try again")
            self.traces = TraceStore(path, busy_timeout_ms=self.BUSY_TIMEOUT_MS)
        return self.traces

    def record(self, change: Any) -> None:
        from agentos.core.errors import ServiceUnavailable
        from agentos.policy.traces import TraceEvent

        traces = self._open()
        traces.write(TraceEvent(**change.trace()))
        if traces.flush():
            raise ServiceUnavailable("the log is busy or broken, and did not take the row")

    def read(self, plugin: str, limit: int) -> list[dict]:
        from agentos.plugins.models import CHANGE_KIND

        events, _ = self._open().query(kind=CHANGE_KIND, limit=500)
        return [event.public() for event in events if (event.args or {}).get("plugin") == plugin][:limit]

    def close(self) -> None:
        if self.traces is not None:
            self.traces.close()


def _wire_feature_clis() -> None:
    with contextlib.suppress(ModuleNotFoundError):
        from agentos.plugins import cli as plugins_cli

        if plugins_cli.runtime_factory is None:
            plugins_cli.runtime_factory = plugins_cli_runtime
        if plugins_cli.change_log_factory is None:
            plugins_cli.change_log_factory = lambda runtime: PluginChanges(runtime.require("config").home)
    try:
        from agentos.brains import cli as brains_cli
    except ModuleNotFoundError:
        return
    if brains_cli.runtime_factory is None:
        brains_cli.runtime_factory = cli_runtime


_mount_features()
_wire_feature_clis()
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
