"""The service that keeps the local node running: a launchd agent on macOS
(~/Library/LaunchAgents/dev.agentos.local.plist) or a systemd user unit on Linux
(~/.config/systemd/user/agentos-local.service). Either runs `agentos local serve --home <home>`,
starts it at login, and starts it again if it stops. Its output goes to <home>/logs/local.log.

start, stop and status are what `agentos local` and the `agentos setup` wizard call. Every action
on launchd or systemd goes through a `Run` (a command list in, a CompletedProcess out), so tests
pass a fake one and nothing is ever loaded on the computer that runs them.
"""

from __future__ import annotations

import os
import plistlib
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentos.client.ports import port_in_use
from agentos.client.state import Paths, current_platform
from agentos.client.tunnel import systemd_quote
from agentos.local import home as local_home
from agentos.local import wake
from agentos.local.home import Busy, LocalError
from agentos.local.paths import log_path, url

LABEL = "dev.agentos.local"
UNIT = "agentos-local.service"
CMD_TIMEOUT = 30
START_WAIT_S = 30.0  # how long start waits for the node to answer on its port
STOP_WAIT_S = 20.0  # how long a restart waits for the old node to let go of its port
THROTTLE_S = 10  # launchd waits at least this long before it starts the node again
# launchd finishes a bootout in the background, so a bootstrap right after `agentos local stop` (or a restart
# by `agentos setup`) can fail with "5: Input/output error" for a few seconds: try again, waiting longer each time
BOOTSTRAP_TRIES = 6
BOOTSTRAP_WAIT_S = 0.5

Run = Callable[[list[str]], subprocess.CompletedProcess]


@dataclass
class ServiceState:
    running: bool
    pid: int | None
    port: int | None
    url: str | None
    log_path: Path
    note: str


@dataclass
class Loaded:
    """What launchd or systemd says about the service."""
    installed: bool
    loaded: bool
    running: bool
    pid: int | None = None
    detail: str | None = None


def run_command(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise LocalError(f"{Path(cmd[0]).name} did not answer in {CMD_TIMEOUT} seconds") from None
    except OSError as exc:
        raise LocalError(f"could not run {cmd[0]}: {exc.strerror}") from None


def _last_line(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or proc.stdout or "").strip()
    return text.splitlines()[-1] if text else f"exit {proc.returncode}"


def serve_command(home: Path) -> list[str]:
    """What the service runs: this Python's agentos, so it is the agentos that ran `start`."""
    return [sys.executable, "-m", "agentos.cli", "local", "serve", "--home", str(home)]


def service_env(home: Path) -> dict[str, str]:
    """The service's environment: this home, the PATH of the shell that ran `start` (launchd and
    systemd give a bare one, and the node runs ssh, git and docker), and Claude Code's folder when it is
    not the default."""
    env = {"AGENTOS_HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
           "PYTHONUNBUFFERED": "1"}
    if os.environ.get("CLAUDE_CONFIG_DIR"):  # where Claude Code keeps its skills, which the service refreshes
        env["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
    return env


class Manager(Protocol):
    kind: str

    def unit_path(self) -> Path: ...

    def install(self, home: Path) -> None:
        """Write the unit and (re)start it."""

    def stop(self) -> bool:
        """Stop it, and keep it from starting again at login, until the next install."""

    def state(self) -> Loaded: ...


class Launchd:
    kind = "launchd"

    def __init__(self, paths: Paths, run: Run = run_command, *, launchctl: str = "launchctl", uid: int | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.paths, self.run, self.launchctl, self.sleep = paths, run, launchctl, sleep
        self.domain = f"gui/{os.getuid() if uid is None else uid}"

    def unit_path(self) -> Path:
        return self.paths.launch_agents_dir / f"{LABEL}.plist"

    def render(self, home: Path) -> bytes:
        log = str(log_path(home))
        return plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": serve_command(home),
            "EnvironmentVariables": service_env(home),
            "WorkingDirectory": str(home),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": THROTTLE_S,
            # Interactive, never Background: launchd runs a Background job in the Darwin background state
            # (efficiency cores, throttled disk and network) and so does every git and ssh it starts. On
            # 2026-11-16 that made a 13 MB clone take 212 s instead of 2 s, a mirror fetch 254 s and a
            # first MCP handshake miss a client's 30 s connect window. The service is idle while nothing
            # calls it, so Interactive costs nothing then.
            "ProcessType": "Interactive",
            "StandardOutPath": log,
            "StandardErrorPath": log,
        })

    def throttled(self) -> bool:
        """Is the installed plist one that runs the node as a Background job (ProcessType Background, as
        `agentos local start` wrote it before 2026-11-16)? launchd applies a plist when it loads it, so
        only a new install moves a running node out of the throttling (see render)."""
        try:
            return plistlib.loads(self.unit_path().read_bytes()).get("ProcessType") == "Background"
        except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
            return False

    def install(self, home: Path) -> None:
        path = self.unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.render(home))
        path.chmod(0o644)
        if self.state().loaded:
            self.run([self.launchctl, "bootout", f"{self.domain}/{LABEL}"])
        proc = self.run([self.launchctl, "bootstrap", self.domain, str(path)])
        for attempt in range(1, BOOTSTRAP_TRIES):
            if proc.returncode == 0:
                break
            # "5: Input/output error": a stale copy is still loaded, or the last bootout is not finished yet
            self.run([self.launchctl, "bootout", f"{self.domain}/{LABEL}"])
            self.sleep(BOOTSTRAP_WAIT_S * attempt)
            proc = self.run([self.launchctl, "bootstrap", self.domain, str(path)])
        if proc.returncode != 0:
            raise LocalError(f"launchctl could not load {path.name}: {_last_line(proc)}",
                             f"look at `launchctl print {self.domain}/{LABEL}`")

    def stop(self) -> bool:
        stopped = self.run([self.launchctl, "bootout", f"{self.domain}/{LABEL}"]).returncode == 0
        path = self.unit_path()
        if path.exists():  # a plist left in LaunchAgents is loaded again at the next login
            path.unlink()
            return True
        return stopped

    def state(self) -> Loaded:
        installed = self.unit_path().exists()
        proc = self.run([self.launchctl, "print", f"{self.domain}/{LABEL}"])
        if proc.returncode != 0:
            return Loaded(installed=installed, loaded=False, running=False)
        out = proc.stdout or ""
        state = re.search(r"^\s*state = (.+)$", out, re.MULTILINE)
        pid = re.search(r"^\s*pid = (\d+)$", out, re.MULTILINE)
        last = re.search(r"^\s*last exit code = (.+)$", out, re.MULTILINE)
        running = bool(state and state.group(1).strip() == "running")
        detail = f"last exit {last.group(1).strip()}" if not running and last and "never" not in last.group(1) else None
        return Loaded(installed=installed, loaded=True, running=running, pid=int(pid.group(1)) if pid else None,
                      detail=detail)


class Systemd:
    kind = "systemd"

    def __init__(self, paths: Paths, run: Run = run_command, *, systemctl: str = "systemctl"):
        self.paths, self.run, self.systemctl = paths, run, systemctl

    def unit_path(self) -> Path:
        return self.paths.systemd_user_dir / UNIT

    def render(self, home: Path) -> bytes:
        exec_start = " ".join(systemd_quote(arg) for arg in serve_command(home))
        env = "\n".join(f"Environment={systemd_quote(f'{k}={v}')}" for k, v in service_env(home).items())
        log = systemd_quote(str(log_path(home)))
        return (
            "# Written by `agentos local start`; `agentos local stop` stops it.\n"
            "[Unit]\n"
            "Description=agentos local node\n"
            "StartLimitIntervalSec=0\n"
            "\n"
            "[Service]\n"
            f"ExecStart={exec_start}\n"
            f"{env}\n"
            f"WorkingDirectory={systemd_quote(str(home))}\n"
            f"StandardOutput=append:{log}\n"
            f"StandardError=append:{log}\n"
            "Restart=always\n"
            "RestartSec=10\n"
            "TimeoutStopSec=30\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        ).encode()

    def _ctl(self, *args: str) -> subprocess.CompletedProcess:
        return self.run([self.systemctl, "--user", *args])

    def install(self, home: Path) -> None:
        path = self.unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.render(home))
        path.chmod(0o644)
        for args in (("daemon-reload",), ("enable", UNIT), ("restart", UNIT)):
            proc = self._ctl(*args)
            if proc.returncode != 0:
                raise LocalError(f"systemctl --user {' '.join(args)} failed: {_last_line(proc)}",
                                 "is a systemd user session running? (`systemctl --user status`)")

    def stop(self) -> bool:
        return self._ctl("disable", "--now", UNIT).returncode == 0

    def state(self) -> Loaded:
        installed = self.unit_path().exists()
        if not installed:
            return Loaded(installed=False, loaded=False, running=False)
        active = (self._ctl("is-active", UNIT).stdout or "").strip()
        pid = None
        shown = (self._ctl("show", "-p", "MainPID", "--value", UNIT).stdout or "").strip()
        if shown.isdigit() and int(shown) > 0:
            pid = int(shown)
        return Loaded(installed=True, loaded=active in ("active", "activating"), running=active == "active", pid=pid,
                      detail=None if active == "active" else active or None)


def manager(*, user_home: Path | None = None, run: Run = run_command, platform: str | None = None) -> Manager:
    paths = Paths(user_home or Path.home())
    if (platform or current_platform()) == "macos":
        return Launchd(paths, run)
    return Systemd(paths, run)


def answers(port: int) -> bool:
    """Something listens on 127.0.0.1:port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


@dataclass
class Deps:
    """What start, stop and status act through. The defaults are this computer's; tests pass fakes."""
    manager: Manager | None = None
    busy: Busy = port_in_use
    answers: Callable[[int], bool] = answers
    sleep: Callable[[float], None] = time.sleep
    wait_s: float = START_WAIT_S

    def svc(self) -> Manager:
        if self.manager is None:
            self.manager = manager()
        return self.manager


def status(home: Path, *, deps: Deps | None = None) -> ServiceState:
    deps = deps or Deps()
    home = Path(home).expanduser()
    cfg = local_home.read(home)
    port = cfg.mcp_port if cfg is not None else None
    if cfg is None:
        return ServiceState(False, None, None, None, log_path(home),
                            f"Not set up yet in {home}. Run `agentos local start`.")
    loaded = deps.svc().state()
    up = loaded.running and deps.answers(port)
    if up:
        head = f"Running on port {port}."
    elif loaded.running:
        head = f"Running, but nothing answers on port {port} yet. It may still be starting: see `agentos local logs`."
    elif loaded.loaded:
        head = "Stopped, and it starts again by itself" + (f" ({loaded.detail})." if loaded.detail else ".") + \
            " See `agentos local logs`."
    else:
        head = "Not running. Start it with `agentos local start`."
    return ServiceState(running=up, pid=loaded.pid if loaded.running else None, port=port,
                        url=url(port) if port else None, log_path=log_path(home), note=_note(head, home))


def open_environments(home: Path) -> list[tuple[str, str]]:
    """(id, title) of every environment of this home that is open: each one's VM is running."""
    from agentos.environments.store import EnvironmentStore

    db = Path(home) / "environments.db"
    if not db.exists():
        return []
    return [(env.id, env.title) for env in EnvironmentStore(db).open()]


def _note(head: str, home: Path) -> str:
    """The status line: `head`, the open environments, and what the last catch-up closed."""
    parts = [head]
    try:
        live = open_environments(home)
    except Exception:  # noqa: BLE001 - a store that cannot be read must not hide the status
        live = []
    if live:
        n = len(live)
        parts.append(f"{n} environment{'s are' if n != 1 else ' is'} open: "
                     + ", ".join(f"{i} ({t})" for i, t in live) + ".")
    caught = wake.note(wake.read_last(home))
    if caught:
        parts.append(caught)
    return " ".join(parts)


def start(home: Path, port: int | None = None, *, deps: Deps | None = None) -> ServiceState:
    """Set the home up, then install and (re)start the service on `port` (None: the remembered port,
    or 7410, or the next free one), and wait until it answers. A service already running on that
    port is left running."""
    deps = deps or Deps()
    home = Path(home).expanduser()
    now = status(home, deps=deps)
    svc = deps.svc()
    stale = bool(getattr(svc, "throttled", lambda: False)())  # an old launchd plist: start it again from a new one
    if now.running and (port is None or port == now.port) and not stale:
        now.note = _note(f"Already running on port {now.port}.", home)
        return now
    if now.running and stale:
        port = port or now.port
    if now.running:
        deps.svc().stop()  # moving to another port: the old one goes first, so the port probe is true
        # launchd returns before the old node has let go of its port: wait for it, or the port probe
        # below says "already in use" and the node is left stopped (seen on a reload from Background)
        waited = 0.0
        while now.port and deps.busy(now.port) and waited < STOP_WAIT_S:
            deps.sleep(0.25)
            waited += 0.25
    chosen = local_home.choose_port(home, port, busy=deps.busy)
    local_home.ensure(home, chosen)
    log_path(home).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    deps.svc().install(home)
    waited = 0.0
    while waited < deps.wait_s:
        state = status(home, deps=deps)
        if state.running:
            return state
        deps.sleep(0.5)
        waited += 0.5
    state = status(home, deps=deps)
    if not state.running:
        state.note = (f"Started, but it did not answer on port {chosen} within {deps.wait_s:g} s. "
                      "See `agentos local logs`.")
    return state


THROTTLED_WORDS = ("macOS runs this node as a Background job (an older `agentos local start` wrote its launchd "
                   "plist so), which slows every clone, fetch and first MCP connect many times over; run "
                   "`agentos local start` once to start it again without that")


def throttled_note(*, deps: Deps | None = None) -> str | None:
    """THROTTLED_WORDS when the installed service is a throttled launchd job, else None (and on Linux)."""
    try:
        svc = (deps or Deps()).svc()
        return THROTTLED_WORDS if getattr(svc, "throttled", lambda: False)() else None
    except Exception:  # noqa: BLE001 - a note, never a reason the node does not start
        return None


def stop(home: Path, *, deps: Deps | None = None) -> ServiceState:
    """Stop the service; it does not start again at login until `agentos local start`. Environments
    keep running on their VMs to their time limit, and the next start closes the ones past it."""
    deps = deps or Deps()
    home = Path(home).expanduser()
    deps.svc().stop()
    state = status(home, deps=deps)
    head = "Stopped. It stays stopped, also after a restart of this computer, until `agentos local start`."
    live = open_environments(home)
    if live:
        head += (f" {len(live)} environment{'s' if len(live) != 1 else ''} stay open and their VMs keep costing money: "
                 "nothing closes them while agentos is stopped. The next start closes the ones past their time "
                 "limit at once.")
    state.note = head
    return state


def log_tail(home: Path, lines: int = 50) -> list[str]:
    try:
        text = log_path(Path(home).expanduser()).read_text(errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]
