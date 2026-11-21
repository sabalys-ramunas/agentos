"""The service that keeps the local node running: a launchd agent on macOS
(~/Library/LaunchAgents/dev.openajar.local.plist) or a systemd user unit on Linux
(~/.config/systemd/user/openajar-local.service). Either runs `openajar local serve --home <home>`,
starts it at login, and starts it again if it stops. Its output goes to <home>/logs/local.log.

start, stop and status are what `openajar local` and the `openajar setup` wizard call. Every action
on launchd or systemd goes through a `Run` (a command list in, a CompletedProcess out), so tests
pass a fake one and nothing is ever loaded on the computer that runs them.
"""

from __future__ import annotations

import hashlib
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

from openajar.client.ports import port_in_use
from openajar.client.state import Paths, current_platform
from openajar.client.tunnel import systemd_quote
from openajar.local import home as local_home
from openajar.local import wake
from openajar.local.home import Busy, LocalError
from openajar.local.paths import is_default, log_path, url

LABEL = "dev.openajar.local"
UNIT = "openajar-local.service"
LEGACY_LABEL = "dev.agentos.local"  # legacy-name: the launchd job before the rename
LEGACY_UNIT = "agentos-local.service"  # legacy-name: the systemd user unit before the rename
# another home than ~/.openajar (a test's, a builder's) gets a service of its own, named by its folder, so
# `openajar local start --home X` and `stop --home X` never replace or stop the person's own node


def names_for(home: Path | None) -> tuple[str, str]:
    """(the launchd label, the systemd unit) of the service that runs `home`."""
    if home is None or is_default(home):
        return LABEL, UNIT
    tag = hashlib.sha256(str(Path(home).expanduser().resolve()).encode()).hexdigest()[:8]
    return f"{LABEL}.{tag}", f"openajar-local-{tag}.service"
CMD_TIMEOUT = 30
START_WAIT_S = 30.0  # how long start waits for the node to answer on its port
STOP_WAIT_S = 20.0  # how long a restart waits for the old node to let go of its port
THROTTLE_S = 10  # launchd waits at least this long before it starts the node again
# launchd finishes a bootout in the background, so a bootstrap right after `openajar local stop` (or a restart
# by `openajar setup`) can fail with "5: Input/output error" for a few seconds: try again, waiting longer each time
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
    """What the service runs: this Python's openajar, so it is the openajar that ran `start`."""
    return [sys.executable, "-m", "openajar.cli", "local", "serve", "--home", str(home)]


def service_env(home: Path) -> dict[str, str]:
    """The service's environment: this home, the PATH of the shell that ran `start` (launchd and
    systemd give a bare one, and the node runs ssh, git and docker), and Claude Code's folder when it is
    not the default."""
    env = {"OPENAJAR_HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
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

    def restart(self) -> None:
        """Stop the running node and start it again from the unit as it is (its home, port and environment)."""

    def retire_legacy(self) -> bool:
        """Stop and remove the unit an install before the rename wrote; True when there was one."""


class Launchd:
    kind = "launchd"

    def __init__(self, paths: Paths, run: Run = run_command, *, launchctl: str = "launchctl", uid: int | None = None,
                 sleep: Callable[[float], None] = time.sleep, label: str = LABEL):
        self.paths, self.run, self.launchctl, self.sleep, self.label = paths, run, launchctl, sleep, label
        self.domain = f"gui/{os.getuid() if uid is None else uid}"

    def unit_path(self) -> Path:
        return self.paths.launch_agents_dir / f"{self.label}.plist"

    def render(self, home: Path) -> bytes:
        log = str(log_path(home))
        return plistlib.dumps({
            "Label": self.label,
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
        `openajar local start` wrote it before 2026-11-16)? launchd applies a plist when it loads it, so
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
            self.run([self.launchctl, "bootout", f"{self.domain}/{self.label}"])
        proc = self.run([self.launchctl, "bootstrap", self.domain, str(path)])
        for attempt in range(1, BOOTSTRAP_TRIES):
            if proc.returncode == 0:
                break
            # "5: Input/output error": a stale copy is still loaded, or the last bootout is not finished yet
            self.run([self.launchctl, "bootout", f"{self.domain}/{self.label}"])
            self.sleep(BOOTSTRAP_WAIT_S * attempt)
            proc = self.run([self.launchctl, "bootstrap", self.domain, str(path)])
        if proc.returncode != 0:
            raise LocalError(f"launchctl could not load {path.name}: {_last_line(proc)}",
                             f"look at `launchctl print {self.domain}/{self.label}`")

    def stop(self) -> bool:
        stopped = self.run([self.launchctl, "bootout", f"{self.domain}/{self.label}"]).returncode == 0
        legacy = self.retire_legacy()
        path = self.unit_path()
        if path.exists():  # a plist left in LaunchAgents is loaded again at the next login
            path.unlink()
            return True
        return stopped or legacy

    def restart(self) -> None:
        # -k: launchd ends the running node (SIGTERM, its clean shutdown) and starts the job again at once
        proc = self.run([self.launchctl, "kickstart", "-k", f"{self.domain}/{self.label}"])
        if proc.returncode != 0:
            raise LocalError(f"launchctl could not restart {self.label}: {_last_line(proc)}",
                             f"look at `launchctl print {self.domain}/{self.label}`, or run `openajar local start`")

    def retire_legacy(self) -> bool:
        if self.label != LABEL:  # a second home's service: the old job is the person's own node, never its to stop
            return False
        path = self.paths.launch_agents_dir / f"{LEGACY_LABEL}.plist"
        loaded = self.run([self.launchctl, "print", f"{self.domain}/{LEGACY_LABEL}"]).returncode == 0
        if loaded:
            self.run([self.launchctl, "bootout", f"{self.domain}/{LEGACY_LABEL}"])
        if path.exists():
            path.unlink()
        return loaded or path.exists()

    def state(self) -> Loaded:
        installed = self.unit_path().exists()
        proc = self.run([self.launchctl, "print", f"{self.domain}/{self.label}"])
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

    def __init__(self, paths: Paths, run: Run = run_command, *, systemctl: str = "systemctl", unit: str = UNIT):
        self.paths, self.run, self.systemctl, self.unit = paths, run, systemctl, unit

    def unit_path(self) -> Path:
        return self.paths.systemd_user_dir / self.unit

    def render(self, home: Path) -> bytes:
        exec_start = " ".join(systemd_quote(arg) for arg in serve_command(home))
        env = "\n".join(f"Environment={systemd_quote(f'{k}={v}')}" for k, v in service_env(home).items())
        log = systemd_quote(str(log_path(home)))
        return (
            "# Written by `openajar local start`; `openajar local stop` stops it.\n"
            "[Unit]\n"
            "Description=openajar local node\n"
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
        for args in (("daemon-reload",), ("enable", self.unit), ("restart", self.unit)):
            proc = self._ctl(*args)
            if proc.returncode != 0:
                raise LocalError(f"systemctl --user {' '.join(args)} failed: {_last_line(proc)}",
                                 "is a systemd user session running? (`systemctl --user status`)")

    def stop(self) -> bool:
        legacy = self.retire_legacy()
        return self._ctl("disable", "--now", self.unit).returncode == 0 or legacy

    def restart(self) -> None:
        proc = self._ctl("restart", self.unit)
        if proc.returncode != 0:
            raise LocalError(f"systemctl --user restart {self.unit} failed: {_last_line(proc)}",
                             f"look at `systemctl --user status {self.unit}`, or run `openajar local start`")

    def retire_legacy(self) -> bool:
        if self.unit != UNIT:  # a second home's service: the old unit is the person's own node, never its to stop
            return False
        path = self.paths.systemd_user_dir / LEGACY_UNIT
        if not path.exists():
            return False
        self._ctl("disable", "--now", LEGACY_UNIT)
        path.unlink()
        self._ctl("daemon-reload")
        return True

    def state(self) -> Loaded:
        installed = self.unit_path().exists()
        if not installed:
            return Loaded(installed=False, loaded=False, running=False)
        active = (self._ctl("is-active", self.unit).stdout or "").strip()
        pid = None
        shown = (self._ctl("show", "-p", "MainPID", "--value", self.unit).stdout or "").strip()
        if shown.isdigit() and int(shown) > 0:
            pid = int(shown)
        return Loaded(installed=True, loaded=active in ("active", "activating"), running=active == "active", pid=pid,
                      detail=None if active == "active" else active or None)


def manager(*, user_home: Path | None = None, run: Run = run_command, platform: str | None = None,
            home: Path | None = None) -> Manager:
    """The service of the local node in `home` (None: ~/.openajar), each home its own (names_for)."""
    paths = Paths(user_home or Path.home())
    label, unit = names_for(home)
    if (platform or current_platform()) == "macos":
        return Launchd(paths, run, label=label)
    return Systemd(paths, run, unit=unit)


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

    def svc(self, home: Path | None = None) -> Manager:
        if self.manager is None:
            self.manager = manager(home=home)
        return self.manager


def status(home: Path, *, deps: Deps | None = None) -> ServiceState:
    deps = deps or Deps()
    home = Path(home).expanduser()
    cfg = local_home.read(home)
    port = cfg.mcp_port if cfg is not None else None
    if cfg is None:
        return ServiceState(False, None, None, None, log_path(home),
                            f"Not set up yet in {home}. Run `openajar local start`.")
    loaded = deps.svc(home).state()
    up = loaded.running and deps.answers(port)
    if up:
        head = f"Running on port {port}."
    elif loaded.running:
        head = f"Running, but nothing answers on port {port} yet. It may still be starting: see `openajar local logs`."
    elif loaded.loaded:
        head = "Stopped, and it starts again by itself" + (f" ({loaded.detail})." if loaded.detail else ".") + \
            " See `openajar local logs`."
    else:
        head = "Not running. Start it with `openajar local start`."
    return ServiceState(running=up, pid=loaded.pid if loaded.running else None, port=port,
                        url=url(port) if port else None, log_path=log_path(home), note=_note(head, home))


def open_environments(home: Path) -> list[tuple[str, str]]:
    """(id, title) of every environment of this home that is open: each one's VM is running."""
    from openajar.environments.store import EnvironmentStore

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
    svc = deps.svc(home)
    retire = getattr(svc, "retire_legacy", None)
    if retire is not None and retire():  # the unit an install before the rename wrote: its port comes free
        cfg = local_home.read(home)
        waited = 0.0
        while cfg is not None and cfg.mcp_port and deps.busy(cfg.mcp_port) and waited < STOP_WAIT_S:
            deps.sleep(0.25)
            waited += 0.25
    now = status(home, deps=deps)
    stale = bool(getattr(svc, "throttled", lambda: False)())  # an old launchd plist: start it again from a new one
    if now.running and (port is None or port == now.port) and not stale:
        now.note = _note(f"Already running on port {now.port}.", home)
        return now
    if now.running and stale:
        port = port or now.port
    if now.running:
        deps.svc(home).stop()  # moving to another port: the old one goes first, so the port probe is true
        # launchd returns before the old node has let go of its port: wait for it, or the port probe
        # below says "already in use" and the node is left stopped (seen on a reload from Background)
        waited = 0.0
        while now.port and deps.busy(now.port) and waited < STOP_WAIT_S:
            deps.sleep(0.25)
            waited += 0.25
    chosen = local_home.choose_port(home, port, busy=deps.busy)
    local_home.ensure(home, chosen)
    log_path(home).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    deps.svc(home).install(home)
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
                      "See `openajar local logs`.")
    return state


def restart(home: Path, *, deps: Deps | None = None) -> ServiceState | None:
    """Start the running service again, so it runs the openajar installed now (the installer calls this after
    it replaced the tool: the old node kept running the old code). The unit stays as it is, so the home, the
    port and the environment do too; an old Background plist is written again (start). None when the service
    is not loaded: there is nothing to restart, and nothing is started."""
    deps = deps or Deps()
    home = Path(home).expanduser()
    svc = deps.svc(home)
    loaded = svc.state()
    cfg = local_home.read(home)
    if not loaded.loaded or cfg is None:
        return None
    live = _open_or_none(home)
    if getattr(svc, "throttled", lambda: False)():
        state = start(home, deps=deps)
    else:
        svc.restart()
        state = _wait_for_a_new_node(home, loaded.pid, deps)
    if not state.running:
        state.note = (f"Restarted the local service, but it did not answer on port {cfg.mcp_port} within "
                      f"{deps.wait_s:g} s. See `openajar local logs`.")
        return state
    state.note = restarted_words(state.port or cfg.mcp_port, live)
    return state


def _open_or_none(home: Path) -> list[tuple[str, str]]:
    try:
        return open_environments(home)
    except Exception:  # noqa: BLE001 - a store that cannot be read must not stop the restart
        return []


def _wait_for_a_new_node(home: Path, old_pid: int | None, deps: Deps) -> ServiceState:
    """Wait until a node answers that is not the one before (launchd and systemd may report the old pid for a
    moment), up to deps.wait_s."""
    waited = 0.0
    while True:
        state = status(home, deps=deps)
        if state.running and (old_pid is None or state.pid != old_pid):
            return state
        if waited >= deps.wait_s:
            state.running = state.running and old_pid is None
            return state
        deps.sleep(0.5)
        waited += 0.5


def restarted_words(port: int, live: list[tuple[str, str]]) -> str:
    """What a restart says: one line, and one more when environments were open, with what the restart
    interrupted on them. Their machines and jobs are not touched (a job runs on its VM, detached, and the new
    node follows it again), but the old node's clean shutdown closes each environment's browser lane (the
    browser on the VM stops, and opens again from its profile on the next browser call), ends the tunnels and
    a recording in progress, and drops every connection to the node for a few seconds."""
    from openajar import __version__

    head = f"Restarted the local service on port {port}, so it runs openajar {__version__} (same home, same port)."
    if not live:
        return head
    n = len(live)
    return (f"{head}\n{n} environment{'s stay' if n != 1 else ' stays'} open and {'their' if n != 1 else 'its'} "
            "jobs keep running. An open browser there closed and opens again on the next browser call (its open "
            "pages are gone); a live view in the web app tries again by itself; a recording or tool call that was "
            "running stopped, and an upload or download link not used yet no longer works.")


THROTTLED_WORDS = ("macOS runs this node as a Background job (an older `openajar local start` wrote its launchd "
                   "plist so), which slows every clone, fetch and first MCP connect many times over; run "
                   "`openajar local start` once to start it again without that")


def throttled_note(*, deps: Deps | None = None) -> str | None:
    """THROTTLED_WORDS when the installed service is a throttled launchd job, else None (and on Linux)."""
    try:
        svc = (deps or Deps()).svc()
        return THROTTLED_WORDS if getattr(svc, "throttled", lambda: False)() else None
    except Exception:  # noqa: BLE001 - a note, never a reason the node does not start
        return None


def stop(home: Path, *, deps: Deps | None = None) -> ServiceState:
    """Stop the service; it does not start again at login until `openajar local start`. Environments
    keep running on their VMs to their time limit, and the next start closes the ones past it."""
    deps = deps or Deps()
    home = Path(home).expanduser()
    deps.svc(home).stop()
    state = status(home, deps=deps)
    head = "Stopped. It stays stopped, also after a restart of this computer, until `openajar local start`."
    live = open_environments(home)
    if live:
        head += (f" {len(live)} environment{'s' if len(live) != 1 else ''} stay open and their VMs keep costing money: "
                 "nothing closes them while openajar is stopped. The next start closes the ones past their time "
                 "limit at once.")
    state.note = head
    return state


def log_tail(home: Path, lines: int = 50) -> list[str]:
    try:
        text = log_path(Path(home).expanduser()).read_text(errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]
