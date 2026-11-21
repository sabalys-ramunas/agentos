"""The persistent SSH tunnel: a launchd LaunchAgent on macOS, a systemd user unit on Linux.

Both run the same command and restart it when it exits (the node rebooted, the laptop
woke on another network). `BatchMode=yes` because nobody can answer a prompt there; the
key must be in ssh-agent or unencrypted. `ControlPath=none` so the tunnel never rides on
(or dies with) a multiplexed session from the owner's terminal.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from openajar.client.errors import TunnelError
from openajar.client.state import Paths, shown

THROTTLE_SECONDS = 30  # launchd waits at least this long between restarts
CMD_TIMEOUT = 30


@dataclass(frozen=True)
class TunnelSpec:
    name: str
    ssh: str
    host: str
    local_port: int
    remote_port: int

    def ssh_args(self) -> list[str]:
        return [
            self.ssh, "-N",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=15",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-L", f"{self.local_port}:localhost:{self.remote_port}",
            self.host,
        ]


@dataclass(frozen=True)
class ServiceState:
    installed: bool
    loaded: bool
    running: bool
    pid: int | None = None
    detail: str | None = None

    @property
    def label(self) -> str:
        if self.running:
            return "running"
        if self.loaded:
            return "restarting" if not self.detail else f"stopped ({self.detail})"
        return "not loaded" if self.installed else "not installed"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise TunnelError(f"{Path(cmd[0]).name} did not answer in {CMD_TIMEOUT} seconds") from None
    except OSError as exc:
        raise TunnelError(f"could not run {cmd[0]}: {exc.strerror}") from None


def _last_line(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or proc.stdout).strip()
    return text.splitlines()[-1] if text else f"exit {proc.returncode}"


def _tail(path: Path, n: int) -> list[str]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [line for line in lines if line.strip()][-n:]


class LaunchdService:
    kind = "launchd"

    def __init__(self, paths: Paths, launchctl: str, uid: int | None = None):
        self.paths = paths
        self.launchctl = launchctl
        self.domain = f"gui/{os.getuid() if uid is None else uid}"

    def label(self, name: str) -> str:
        return f"dev.agentos.tunnel.{name}"

    def unit_path(self, name: str) -> Path:
        return self.paths.launch_agents_dir / f"{self.label(name)}.plist"

    def log_path(self, name: str) -> Path:
        return self.paths.logs_dir / f"tunnel-{name}.log"

    def render(self, spec: TunnelSpec) -> bytes:
        log = str(self.log_path(spec.name))
        return plistlib.dumps({
            "Label": self.label(spec.name),
            "ProgramArguments": spec.ssh_args(),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": THROTTLE_SECONDS,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        })

    def matches(self, spec: TunnelSpec) -> bool:
        path = self.unit_path(spec.name)
        return path.exists() and path.read_bytes() == self.render(spec)

    def install(self, spec: TunnelSpec) -> None:
        path = self.unit_path(spec.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.render(spec))
        path.chmod(0o644)
        if self.state(spec.name).loaded:
            self.stop(spec.name)
        proc = _run([self.launchctl, "bootstrap", self.domain, str(path)])
        if proc.returncode != 0:
            # "5: Input/output error" usually means a stale copy is still loaded: unload it once and retry.
            self.stop(spec.name)
            proc = _run([self.launchctl, "bootstrap", self.domain, str(path)])
        if proc.returncode != 0:
            raise TunnelError(f"launchctl could not load {path.name}: {_last_line(proc)}",
                              f"look at `launchctl print {self.domain}/{self.label(spec.name)}`")

    def restart(self, name: str) -> None:
        proc = _run([self.launchctl, "kickstart", "-k", f"{self.domain}/{self.label(name)}"])
        if proc.returncode != 0:
            raise TunnelError(f"launchctl could not restart the tunnel: {_last_line(proc)}")

    def stop(self, name: str) -> bool:
        proc = _run([self.launchctl, "bootout", f"{self.domain}/{self.label(name)}"])
        return proc.returncode == 0

    def uninstall(self, name: str) -> bool:
        stopped = self.stop(name)
        path = self.unit_path(name)
        existed = path.exists()
        if existed:
            path.unlink()
        return stopped or existed

    def state(self, name: str) -> ServiceState:
        installed = self.unit_path(name).exists()
        proc = _run([self.launchctl, "print", f"{self.domain}/{self.label(name)}"])
        if proc.returncode != 0:
            return ServiceState(installed=installed, loaded=False, running=False)
        out = proc.stdout
        state = re.search(r"^\s*state = (.+)$", out, re.MULTILINE)
        pid = re.search(r"^\s*pid = (\d+)$", out, re.MULTILINE)
        last = re.search(r"^\s*last exit code = (.+)$", out, re.MULTILINE)
        running = bool(state and state.group(1).strip() == "running")
        detail = None
        if not running and last and "never" not in last.group(1):
            detail = f"last exit {last.group(1).strip()}"
        return ServiceState(installed=installed, loaded=True, running=running,
                            pid=int(pid.group(1)) if pid else None, detail=detail)

    def log_tail(self, name: str, n: int = 5) -> list[str]:
        return _tail(self.log_path(name), n)

    def describe(self, name: str) -> str:
        return f"LaunchAgent {self.label(name)} ({shown(self.unit_path(name))})"

    def undo_lines(self, name: str) -> list[str]:
        return [f"launchctl bootout {self.domain}/{self.label(name)}", f"rm {self.unit_path(name)}"]


def systemd_quote(arg: str) -> str:
    """One ExecStart word: `%` and `$` are specifiers and variables in unit files."""
    escaped = arg.replace("%", "%%").replace("$", "$$")
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", escaped.replace("$$", "")):
        return escaped
    return '"' + escaped.replace("\\", "\\\\").replace('"', '\\"') + '"'


class SystemdService:
    kind = "systemd"

    def __init__(self, paths: Paths, systemctl: str, journalctl: str | None = None):
        self.paths = paths
        self.systemctl = systemctl
        self.journalctl = journalctl

    def label(self, name: str) -> str:
        return f"agentos-tunnel-{name}.service"

    def unit_path(self, name: str) -> Path:
        return self.paths.systemd_user_dir / self.label(name)

    def render(self, spec: TunnelSpec) -> bytes:
        exec_start = " ".join(systemd_quote(arg) for arg in spec.ssh_args())
        return (
            "# Written by `openajar connect`; remove with `openajar connect --remove "
            f"{spec.name}`.\n"
            "[Unit]\n"
            f"Description=openajar tunnel to {spec.host} ({spec.name})\n"
            "StartLimitIntervalSec=0\n"
            "\n"
            "[Service]\n"
            f"ExecStart={exec_start}\n"
            "Restart=always\n"
            "RestartSec=10\n"
            "RestartSteps=6\n"
            "RestartMaxDelaySec=300\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        ).encode()

    def matches(self, spec: TunnelSpec) -> bool:
        path = self.unit_path(spec.name)
        return path.exists() and path.read_bytes() == self.render(spec)

    def _ctl(self, *args: str) -> subprocess.CompletedProcess:
        return _run([self.systemctl, "--user", *args])

    def install(self, spec: TunnelSpec) -> None:
        path = self.unit_path(spec.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.render(spec))
        path.chmod(0o644)
        unit = self.label(spec.name)
        for args in (("daemon-reload",), ("enable", unit), ("restart", unit)):
            proc = self._ctl(*args)
            if proc.returncode != 0:
                raise TunnelError(f"systemctl --user {' '.join(args)} failed: {_last_line(proc)}",
                                  "is a systemd user session running? (`systemctl --user status`)")

    def restart(self, name: str) -> None:
        proc = self._ctl("restart", self.label(name))
        if proc.returncode != 0:
            raise TunnelError(f"systemctl could not restart the tunnel: {_last_line(proc)}")

    def stop(self, name: str) -> bool:
        return self._ctl("stop", self.label(name)).returncode == 0

    def uninstall(self, name: str) -> bool:
        path = self.unit_path(name)
        existed = path.exists()
        disabled = self._ctl("disable", "--now", self.label(name)).returncode == 0
        if existed:
            path.unlink()
            self._ctl("daemon-reload")
        return existed or disabled

    def state(self, name: str) -> ServiceState:
        installed = self.unit_path(name).exists()
        if not installed:
            return ServiceState(installed=False, loaded=False, running=False)
        proc = self._ctl("is-active", self.label(name))
        active = proc.stdout.strip()
        return ServiceState(installed=True, loaded=active in ("active", "activating"), running=active == "active",
                            detail=None if active == "active" else active or None)

    def log_tail(self, name: str, n: int = 5) -> list[str]:
        if not self.journalctl:
            return []
        try:
            proc = subprocess.run([self.journalctl, "--user", "-u", self.label(name), "-n", str(n), "--no-pager",
                                   "-o", "cat"], check=False, capture_output=True, text=True, timeout=CMD_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [line for line in proc.stdout.splitlines() if line.strip()][-n:]

    def describe(self, name: str) -> str:
        return f"systemd user unit {self.label(name)} ({shown(self.unit_path(name))})"

    def undo_lines(self, name: str) -> list[str]:
        return [f"systemctl --user disable --now {self.label(name)}", f"rm {self.unit_path(name)}"]


class NoTunnel:
    """A connection to the node on this computer (`openajar up --here`): there is nothing to keep up."""

    kind = "none"

    def matches(self, spec: TunnelSpec) -> bool:
        return True

    def install(self, spec: TunnelSpec) -> None:
        return None

    def restart(self, name: str) -> None:
        return None

    def stop(self, name: str) -> bool:
        return False

    def uninstall(self, name: str) -> bool:
        return False

    def state(self, name: str) -> ServiceState:
        return ServiceState(installed=True, loaded=True, running=True, detail="no tunnel")

    def log_tail(self, name: str, n: int = 5) -> list[str]:
        return []

    def describe(self, name: str) -> str:
        return "none (the node is this computer)"

    def undo_lines(self, name: str) -> list[str]:
        return []
