"""`agentos env ...`: terminal and service lanes from the node's shell.

These commands run the env runtime in this process, as the user agentos runs as on the node.
While agentosd owns it, `remove` and `doctor` go through agentosd's API (as a person, with the
one-time code `agentos.commands.base.operator_presence` writes), and the commands that need the runtime
itself say so and how to go on.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sys
from pathlib import Path
from typing import Annotated

import typer

from agentos.core import catalog
from agentos.core.config import home_from_env
from agentos.core.errors import AgentosError
from agentos.core.models import parse_lane_id

app = typer.Typer(no_args_is_help=True, help="Terminal and service lanes: isolation, programs, shells.")
images_app = typer.Typer(no_args_is_help=True, help="Base images built from templates.")
services_app = typer.Typer(no_args_is_help=True, help="Supervised services in service lanes.")
app.add_typer(images_app, name="images")
app.add_typer(services_app, name="services")

_state: dict = {"home": None}


@app.callback()
def main(home: Annotated[Path | None, typer.Option(help="AGENTOS_HOME (default: $AGENTOS_HOME)")] = None) -> None:
    """Terminal and service lanes: isolation, programs, shells."""
    _state["home"] = home


def _manager():
    from agentos.env.manager import EnvManager
    from agentos.vms.ssh import SshLauncher

    manager = EnvManager(_state["home"] or home_from_env())
    manager.remote_launcher = SshLauncher()  # VM lanes run over ssh, as in the daemon (agentos.vms)
    return manager


def _run(coro_fn, *, start: bool = True):
    async def go():
        mgr = _manager()
        try:
            if start:
                await mgr.start()
            return await coro_fn(mgr)
        finally:
            if start:
                await mgr.shutdown()
    try:
        return asyncio.run(go())
    except AgentosError as e:
        _error(e)
        raise typer.Exit(1) from None


def _error(e: AgentosError) -> None:
    from agentos.env.manager import RuntimeOwned
    if isinstance(e, RuntimeOwned):
        typer.echo(f"error: {e.code}: {e.message}\n  fix: {e.fix}", err=True)
        return
    typer.echo(catalog.cli_error(e.code, e.message), err=True)


def _daemon(method: str, path: str, *, person: bool = False):
    """Call the running agentosd for this home, or None when it does not run here (or is too old to
    know the route): then the caller runs the env runtime in this process (finding 30)."""
    from agentos.core.config import config_path
    home = _state["home"] or home_from_env()
    if not config_path(home).exists():
        return None
    from agentos.commands.base import daemon_request, read_config
    cfg = read_config(home)
    node = getattr(cfg, "name", None) or "node-1"
    return daemon_request(cfg, method, f"/api/nodes/{node}/environments{path}", absent_ok=True, person=person)


def _out(obj, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(obj, indent=2, default=str))


@app.command()
def create(
    lane: Annotated[str, typer.Argument(help="workspace/name")],
    preset: Annotated[str, typer.Option(help="workbench (Shared desk) | sandbox (Scratch) | service | locked "
                                                 "(Locked down)")] = "sandbox",
    kind: Annotated[str, typer.Option(help="terminal | service")] = "terminal",
    network: Annotated[str | None, typer.Option(help="shared | isolated | off")] = None,
    allow: Annotated[list[str] | None, typer.Option(help="a host the lane may reach (repeat)")] = None,
    filesystem: Annotated[str | None, typer.Option(
        help="shared (writes go straight to the lane), overlay (writes kept in a layer on top of the base) "
             "or ephemeral (a temporary disk, empty at each run)")] = None,
    processes: Annotated[str | None, typer.Option(help="shared | isolated")] = None,
    programs: Annotated[str | None,
                        typer.Option(help="base (no installs) or overlay (installs allowed)")] = None,
    volume: Annotated[list[str] | None, typer.Option(help="named volume mounted at /volumes/NAME (repeat)")] = None,
    image: Annotated[str | None, typer.Option(help="base image, e.g. dev-box@1")] = None,
    memory_mb: Annotated[int | None, typer.Option()] = None,
    cpus: Annotated[float | None, typer.Option()] = None,
    pids: Annotated[int | None, typer.Option()] = None,
    disk_mb: Annotated[int | None, typer.Option()] = None,
    cmd: Annotated[str | None, typer.Option(help="service command (run with /bin/sh -c)")] = None,
    restart: Annotated[str, typer.Option(help="always | on-failure | never")] = "always",
    health_cmd: Annotated[str | None, typer.Option(help="health check command")] = None,
    health_http: Annotated[str | None, typer.Option(help="health probe URL on the lane loopback")] = None,
    proxy: Annotated[str | None, typer.Option(help="exit proxy label (isolated network)")] = None,
    allow_program: Annotated[list[str] | None, typer.Option(help="a program a locked-down lane may run "
                                                                 "(repeat)")] = None,
    can_read_node_secrets: Annotated[bool, typer.Option(
        "--can-read-node-secrets",
        help="say yes to a lane that reaches the node (a workbench lane, or one sharing the node's filesystem "
             "or processes): a command in it can read the vault, the node token and every lane's browser "
             "profile")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create a terminal or service lane from a preset, with optional dial overrides."""
    overrides = {k: v for k, v in {
        "network": network, "egress_allowlist": allow or None, "filesystem": filesystem, "processes": processes,
        "programs": programs, "volumes": volume or None, "base_image": image, "memory_mb": memory_mb,
        "cpus": cpus, "pids": pids, "disk_mb": disk_mb}.items() if v is not None}
    service = None
    if cmd:
        kind = "service"
        health = None
        if health_cmd or health_http:
            health = {"cmd": ["/bin/sh", "-c", health_cmd] if health_cmd else None, "http": health_http}
        service = {"command": ["/bin/sh", "-c", cmd], "restart": restart, "health": health}
    if kind == "service" and preset == "sandbox" and "network" not in overrides:
        preset = "service"
    mgr = _manager()
    try:
        info = mgr.create(lane, kind=kind, preset=preset, overrides=overrides, service=service, exit_proxy=proxy,
                          allowed_programs=allow_program, node_secrets_ok=can_read_node_secrets)
    except AgentosError as e:
        _error(e)
        raise typer.Exit(1) from None
    if as_json:
        _out(info.model_dump(mode="json"), True)
        return
    typer.echo(f"created {info.lane.kind.value} lane {lane} (preset {info.lane.preset.value})")
    typer.echo(f"isolation: {info.isolation}")


@app.command("list")
def list_(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List terminal and service lanes."""
    infos = _manager().list()
    if as_json:
        _out([i.model_dump(mode="json") for i in infos], True)
        return
    if not infos:
        typer.echo("no terminal or service lanes")
    for i in infos:
        lane = i.lane
        extra = f"  quarantined: {i.quarantine_reason}" if i.quarantine_reason else ""
        typer.echo(f"{lane.id:32} {lane.kind.value:8} {lane.preset.value:9} {i.state.value:11} "
                   f"net={lane.profile.network} fs={lane.profile.filesystem} tier={lane.policy.tier}{extra}")


@app.command()
def remove(lane: str, yes: Annotated[bool, typer.Option("--yes", help="do not ask")] = False) -> None:
    """Delete a lane and everything written inside it."""
    if not yes:
        typer.confirm(f"Delete {lane} and its files?", abort=True)
    try:
        workspace, name = parse_lane_id(lane)
    except ValueError as e:
        typer.echo(f"error: invalid: {e}", err=True)
        raise typer.Exit(2) from None
    if _daemon("DELETE", f"/{workspace}/{name}", person=True) is None:  # agentosd does not run here
        _run(lambda m: m.remove(lane))
    typer.echo(f"removed {lane}")


@app.command()
def reset(lane: str) -> None:
    """Clear a quarantine (the block a lane gets after it fails its network check) and leave the lane not
    running until its next use."""
    info = _run(lambda m: m.reset(lane))
    typer.echo(f"{lane} is {info.state.value}")


@app.command("exec", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def exec_(
    ctx: typer.Context,
    lane: str,
    timeout: Annotated[float, typer.Option(help="seconds")] = 60,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run a command in a lane: agentos env exec LANE -- CMD ..."""
    import shlex
    words = list(ctx.args)
    if not words:
        typer.echo("error: give a command after --", err=True)
        raise typer.Exit(2)
    cmd = words[0] if len(words) == 1 else shlex.join(words)
    stdin = None if sys.stdin.isatty() else sys.stdin.buffer.read()
    res = _run(lambda m: m.exec(lane, cmd, timeout_s=timeout, stdin=stdin))
    if as_json:
        _out(res.model_dump(), True)
        return
    for text, enc, stream in ((res.stdout, res.stdout_encoding, sys.stdout),
                              (res.stderr, res.stderr_encoding, sys.stderr)):
        if enc == "base64":
            stream.buffer.write(base64.b64decode(text))
        else:
            stream.write(text)
        stream.flush()
    if res.timed_out:
        typer.echo(f"agentos: timed out after {timeout:g}s", err=True)
    raise typer.Exit(res.exit_code if res.exit_code is not None else 128)


@app.command()
def shell(lane: str) -> None:
    """Attach this terminal to a new shell in the lane (Ctrl-] closes it)."""
    if not sys.stdin.isatty():
        typer.echo("error: shell needs an interactive terminal", err=True)
        raise typer.Exit(2)

    async def attach(mgr):
        import termios
        import tty
        cols, rows = os.get_terminal_size()
        s = await mgr.shell_open(lane, cols=cols, rows=rows, term=os.environ.get("TERM", "xterm-256color"))
        q = s.subscribe(replay=False)
        loop = asyncio.get_running_loop()
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        done = asyncio.Event()

        def on_stdin():
            data = os.read(fd, 4096)
            if not data or b"\x1d" in data:
                done.set()
                return
            asyncio.ensure_future(s.write(data))

        def on_winch():
            c, r = os.get_terminal_size()
            s.resize(c, r)

        tty.setraw(fd)
        loop.add_reader(fd, on_stdin)
        loop.add_signal_handler(signal.SIGWINCH, on_winch)
        try:
            while not done.is_set():
                get = asyncio.ensure_future(q.get())
                stop = asyncio.ensure_future(done.wait())
                finished, _ = await asyncio.wait({get, stop}, return_when=asyncio.FIRST_COMPLETED)
                if get in finished:
                    data = get.result()
                    if data is None:
                        break
                    os.write(sys.stdout.fileno(), data)
                else:
                    get.cancel()
                stop.cancel()
        finally:
            loop.remove_reader(fd)
            loop.remove_signal_handler(signal.SIGWINCH)
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            await mgr.shell_close(s.id)
        return s.exit_code

    rc = _run(attach)
    typer.echo(f"\r\nshell closed (exit {rc})")


@app.command()
def install(
    lane: str,
    packages: Annotated[list[str], typer.Argument()],
    source: Annotated[str, typer.Option(help="apt | pip | npm")] = "apt",
) -> None:
    """Install packages into a lane's overlay (its own layer of files on top of the base) from a fixed
    source."""
    out = _run(lambda m: m.install(lane, packages, source))
    if out["ok"]:
        typer.echo(f"installed into {lane}: " + ", ".join(f"{k} {v}" for k, v in out["installed"].items()))
    else:
        typer.echo(f"install failed (exit {out['exit_code']}):\n{out['output_tail']}", err=True)
        raise typer.Exit(1)


@app.command()
def doctor(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """What this node can do for env lanes (and the result of an orphan sweep)."""
    async def go(m):
        return m.node_report()
    report = _daemon("GET", "/doctor")
    if report is None:  # agentosd does not run here
        report = _run(go)
    if as_json:
        _out(report, True)
        return
    for k, v in report.items():
        typer.echo(f"{k}: {v}")


# ------------------------------------------------------------------ images
@images_app.command("list")
def images_list(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Templates and built versions."""
    mgr = _manager()
    built = mgr.images()
    if as_json:
        _out({"templates": [t.model_dump() for t in mgr.templates().values()],
              "built": [i.model_dump(mode="json") for i in built]}, True)
        return
    for t in mgr.templates().values():
        vers = [i for i in built if i.name == t.name]
        tag = "placeholder" if t.placeholder else (", ".join(f"@{i.version}" for i in vers) or "not built")
        typer.echo(f"{t.name:12} {tag:20} {t.description}")
    for i in built:
        typer.echo(f"  {i.ref:16} {i.content_hash[:19]}  {i.size_bytes // 1048576} MB  built {i.built_at:%Y-%m-%d %H:%M}")


@images_app.command("build")
def images_build(name: str) -> None:
    """Build the next version of a base image from its template."""
    info = _run(lambda m: m.build_image(name))
    typer.echo(f"built {info.ref} {info.content_hash} ({info.size_bytes // 1048576} MB)")


# ------------------------------------------------------------------ services
@app.command("confinement")
def confinement(lane: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Run the isolation check inside an isolated lane, as the lane's own user: can it reach the node's
    secrets (AGENTOS_HOME, the vault, the live views, other lanes' profiles)? Exit 1 if so."""
    from agentos.brains import confine
    from agentos.core.config import load_config

    home = _state["home"] or home_from_env()
    r = _run(lambda mgr: confine.check_env_lane(mgr, lane, home, config=load_config(home)))
    if as_json:
        typer.echo(json.dumps(r.to_dict(), indent=2))
    else:
        typer.echo(f"{'ok' if r.ok else 'REFUSED'}: {r.reason}")
        for item in r.reachable:
            typer.echo(f"  reachable: {item}")
    if not r.ok:
        raise typer.Exit(1)


@services_app.command("start")
def services_start(
    lane: str,
    ttl: Annotated[float | None, typer.Option(help="stop after this many seconds")] = None,
    foreground: Annotated[bool, typer.Option(help="supervise it here until Ctrl-C")] = False,
) -> None:
    """Start a service (saved as wanted; agentosd keeps it running)."""
    if not foreground:
        from agentos.env.services import expiry
        mgr = _manager()
        try:
            mgr.check_runnable(mgr._service_lane(lane))  # one this node can never start is refused (finding 31)
        except AgentosError as e:
            _error(e)
            raise typer.Exit(1) from None
        mgr.store.set_service_desired(lane, "running", expiry(ttl))
        typer.echo(f"{lane}: wanted running. agentosd starts it; or run with --foreground to supervise here.")
        return

    async def go(m):
        await m.service_start(lane, ttl_s=ttl)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, stop.set)
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        typer.echo(f"supervising {lane}; Ctrl-C stops it")
        while not stop.is_set() and m.services[lane].running:
            try:
                await asyncio.wait_for(stop.wait(), 2)
            except TimeoutError:
                pass
        await m.service_stop(lane)
        m.store.set_service_desired(lane, "running")
    _run(go)


@services_app.command("stop")
def services_stop(lane: str) -> None:
    """Stop a service and mark it stopped."""
    mgr = _manager()
    try:
        mgr._service_lane(lane)
    except AgentosError as e:
        _error(e)
        raise typer.Exit(1) from None
    mgr.store.set_service_desired(lane, "stopped")
    typer.echo(f"{lane}: wanted stopped")


@services_app.command("status")
def services_status(lane: str, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Desired and recorded state of a service."""
    mgr = _manager()
    try:
        st = mgr.service_status(lane)
    except AgentosError as e:
        _error(e)
        raise typer.Exit(1) from None
    if as_json:
        _out(st.model_dump(mode="json"), True)
        return
    typer.echo(f"{lane}: desired {st.desired}, state {st.state}, health {st.health}, restarts {st.restarts}")
    if st.detail:
        typer.echo(f"  {st.detail}")


@services_app.command("logs")
def services_logs(lane: str, lines: Annotated[int, typer.Option()] = 100) -> None:
    """The last lines of a service's log."""
    mgr = _manager()
    try:
        typer.echo(mgr.service_logs(lane, lines))
    except AgentosError as e:
        _error(e)
        raise typer.Exit(1) from None
