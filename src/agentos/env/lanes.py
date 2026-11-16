"""One warm env lane: its mounts, network namespace, egress forwarder, cgroup and workloads."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import secrets
import shutil
import signal
import socket
import termios
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentos.core.errors import AgentosError
from agentos.env.host import Host
from agentos.env.models import EnvLane
from agentos.env.netproxy import EgressPolicy, Forwarder, Upstream
from agentos.env.paths import EnvPaths, EnvSettings
from agentos.env.sandbox import DEFAULT_PATH, LanePlan, Workload

log = logging.getLogger("agentos.env")
REMOTE_STOP_S = 15.0  # how long the ssh that ends a workload's processes on a VM may take


class SandboxError(AgentosError):
    code = "sandbox_error"


class NoSandbox(SandboxError):
    """This node cannot run a sandboxed lane at all (not root, not Linux, no bubblewrap): not a fault
    that goes away, so the message names the owner's real choice (QA 2026-11-10, finding 31)."""
    code = "no_sandbox"



def _take_controlling_tty() -> None:
    """In the child, after setsid: make the PTY on stdin the session's controlling terminal, so
    Ctrl-C, Ctrl-Z and hang-ups reach the foreground job. A new session does not get one just by
    inheriting an open PTY on Linux (macOS is more lenient), which left Ctrl-C dead in shells."""
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass  # already the controlling terminal, or stdin is not a terminal

@dataclass
class Spawned:
    proc: asyncio.subprocess.Process
    wid: str
    kind: str
    cgroup: str | None
    started: float = field(default_factory=lambda: asyncio.get_running_loop().time())


def new_wid(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


async def wait_exit(proc: asyncio.subprocess.Process, timeout: float | None = None) -> int:
    """Wait for the process itself to exit. (asyncio's proc.wait() also waits for its pipes,
    which a background child can hold open for as long as it likes.)"""
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    delay = 0.005
    while proc.returncode is None:
        if deadline is not None and loop.time() >= deadline:
            raise TimeoutError
        await asyncio.sleep(delay)
        delay = min(delay * 2, 0.05)
    return proc.returncode


class LaneRuntime:
    def __init__(self, lane: EnvLane, plan: LanePlan | None, *, paths: EnvPaths, settings: EnvSettings,
                 host: Host, launcher, upstream: Upstream | None = None, on_egress=None):
        self.lane = lane
        self.plan = plan
        self.paths = paths
        self.settings = settings
        self.host = host
        self.launcher = launcher
        self.upstream = upstream
        self.on_egress = on_egress
        self.cgroup: str | None = None
        self.forwarder: Forwarder | None = None
        self.workloads: dict[str, Spawned] = {}
        self.exec_sem = asyncio.Semaphore(settings.max_concurrent_exec)
        self.up_done = False
        self.proxy_port: int | None = None
        self.git_port: int | None = None    # the git gateway's listener in this lane (agentos.git.service)
        self.side_servers: list = []        # listeners besides the forwarder, closed when the lane goes down
        self.on_down: list = []             # callbacks when the lane goes down (the git tokens end)
        self._mounted: list[Path] = []
        self._netns_up = False

    # ------------------------------------------------------------------ lifecycle
    @property
    def netns(self) -> Path | None:
        return self.plan.netns if self.plan else None

    async def up(self) -> None:
        lane, plan = self.lane, self.plan
        try:
            self.paths.lane_dir(lane.id).mkdir(parents=True, exist_ok=True)
            self.paths.logs(lane.id).mkdir(parents=True, exist_ok=True)
            for v in lane.profile.volumes:
                self.paths.volume(v).mkdir(parents=True, exist_ok=True)
            if not lane.isolated:
                self.paths.workdir(lane.id).mkdir(parents=True, exist_ok=True)
            else:
                if not self.host.can_isolate:
                    raise SandboxError(getattr(self.host, "reason", "this node cannot run isolated lanes"))
                assert plan is not None
                await asyncio.to_thread(self._mount_all)
                if plan.netns is not None:
                    await asyncio.to_thread(self.host.netns_create, plan.netns)
                    self._netns_up = True
                    if lane.profile.network == "isolated":
                        policy = EgressPolicy(lane.profile.egress_allowlist, lane.policy.egress_default)
                        self.forwarder = Forwarder(lane.id, policy, upstream=self.upstream,
                                                   on_event=self.on_egress)
                        self.proxy_port = await self.listen(self.forwarder, self.settings.proxy_port)
            self.cgroup = await asyncio.to_thread(self.host.cgroup_lane, lane.key, lane.limits)
            self.up_done = True
        except BaseException:
            await self.down()
            raise

    def _mount_all(self) -> None:
        plan = self.plan
        assert plan is not None
        plan.run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.paths.run, 0o700)
        if plan.disk is not None:
            d = plan.disk
            if d.kind == "tmpfs":
                self.host.mount_tmpfs(d.target, d.size_mb, noexec=d.noexec)
            else:
                assert d.image is not None
                if not d.image.exists():
                    self.host.make_disk_image(d.image, d.size_mb)
                self.host.mount_disk_image(d.image, d.target, noexec=d.noexec)
            self._mounted.append(d.target)
            scratch = plan.scratch
            if scratch is not None:
                for sub, mode in (("tmp", 0o1777), ("run", 0o755), ("shm", 0o1777)):
                    p = scratch / sub
                    shutil.rmtree(p, ignore_errors=True)
                    p.mkdir(parents=True, exist_ok=True)
                    os.chmod(p, mode)
        if self.lane.profile.filesystem == "shared":
            return
        for ov in plan.overlays:
            self.host.mount_overlay(ov.lowers, ov.upper, ov.work, ov.target, noexec=ov.noexec)
            self._mounted.append(ov.target)

    async def listen(self, forwarder: Any, port: int) -> int:
        """Open 127.0.0.1:<port> inside the lane's network namespace and serve it from the host.
        Returns the bound port."""
        sock = await asyncio.to_thread(self.host.socket_in_netns, self.netns)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            sock.listen(128)
        except OSError:
            sock.close()
            raise
        bound = sock.getsockname()[1]
        await forwarder.serve(sock)
        return bound

    async def down(self) -> None:
        for sp in list(self.workloads.values()):
            await self.kill(sp)
        if self.forwarder:
            await self.forwarder.close()
            self.forwarder = None
        for server in self.side_servers:
            try:
                await server.close()
            except Exception:
                log.exception("a listener of %s did not close", self.lane.id)
        self.side_servers.clear()
        self.git_port = None
        for callback in self.on_down:
            try:
                callback()
            except Exception:
                log.exception("a lane-down callback of %s failed", self.lane.id)
        self.on_down.clear()
        await asyncio.to_thread(self._unmount_all)
        if self._netns_up and self.plan and self.plan.netns:
            await asyncio.to_thread(self.host.netns_delete, self.plan.netns)
            self._netns_up = False
        if self.cgroup:
            await asyncio.to_thread(self.host.cgroup_remove, self.cgroup)
            self.cgroup = None
        if self.plan and self.plan.run_dir.is_dir() and not self.host.mounts_under(self.plan.run_dir):
            shutil.rmtree(self.plan.run_dir, ignore_errors=True)
        self.up_done = False

    def _unmount_all(self) -> None:
        # everything under this lane's run dir, deepest first (covers mounts a crash left too)
        if self.plan:
            for m in self.host.mounts_under(self.plan.run_dir):
                if self.plan.netns and m == str(self.plan.netns):
                    continue
                self.host.unmount(Path(m))
        for target in reversed(self._mounted):
            self.host.unmount(target)
        self._mounted.clear()

    # ------------------------------------------------------------------ workloads
    def base_env(self, extra: dict[str, str] | None = None, *, term: str | None = None) -> dict[str, str]:
        lane = self.lane
        if not lane.isolated:
            env = {"AGENTOS_LANE": lane.id}
        else:
            env = {"PATH": DEFAULT_PATH, "HOME": "/root", "USER": "root", "LOGNAME": "root",
                   "LANG": "C.UTF-8", "SHELL": "/bin/bash", "AGENTOS_LANE": lane.id}
            if self.plan:
                env.update(self.plan.env)
            if lane.profile.network == "isolated" and self.proxy_port:
                proxy = f"http://127.0.0.1:{self.proxy_port}"
                env.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy,
                            "https_proxy": proxy, "NO_PROXY": "localhost,127.0.0.1,::1",
                            "no_proxy": "localhost,127.0.0.1,::1"})
        if term:
            env["TERM"] = term
        env.update(extra or {})
        return env

    async def spawn(self, wl: Workload, *, kind: str, stdin=None, stdout=None, stderr=None) -> Spawned:
        cg = None
        if self.cgroup:
            cg = await asyncio.to_thread(self.host.cgroup_workload, self.lane.key, wl.wid)
        cmd = self.launcher.command(self.plan, self.lane, wl, cgroup=cg, workdir=self.paths.workdir(self.lane.id))
        # A PTY has one session. Taken here, a sandboxed `setsid -c` could not take it from us:
        # that needs CAP_SYS_ADMIN, which the sandbox drops, so the shell died on open.
        take_tty = getattr(wl, "pty", False) and not cmd.takes_tty
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd.argv, env=cmd.env, cwd=cmd.cwd, stdin=stdin, stdout=stdout, stderr=stderr,
                pass_fds=cmd.pass_fds, start_new_session=True,
                preexec_fn=_take_controlling_tty if take_tty else None)
        finally:
            for fd in cmd.close_after_spawn:
                os.close(fd)
        sp = Spawned(proc, wl.wid, kind, cg)
        self.workloads[wl.wid] = sp
        return sp

    async def _remote_signal(self, sp: Spawned, sig: str) -> None:
        """On a VM lane, the workload's processes there (its own process group, agentos.vms.ssh): killing
        the local ssh client does not end them. Best effort, bounded: the VM may be gone."""
        stop = getattr(self.launcher, "stop_argv", None)
        argv = stop(self.lane, sp.wid, sig) if callable(stop) and self.lane.remote is not None else None
        if not argv:
            return
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        except OSError:
            return
        try:
            await asyncio.wait_for(proc.wait(), REMOTE_STOP_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    async def terminate(self, sp: Spawned, grace: float = 2.0) -> None:
        """SIGTERM every process of the workload, then SIGKILL after `grace` (on a VM too)."""
        await self._remote_signal(sp, "TERM")
        if sp.proc.returncode is None:
            self._signal(sp, signal.SIGTERM)
            try:
                await wait_exit(sp.proc, grace)
            except TimeoutError:
                pass
        await self.kill(sp)

    async def kill(self, sp: Spawned) -> None:
        await self._remote_signal(sp, "KILL")
        if sp.cgroup:
            await asyncio.to_thread(self.host.cgroup_kill, sp.cgroup)
        self._signal(sp, signal.SIGKILL)
        if sp.proc.returncode is None:
            try:
                await wait_exit(sp.proc, 5)
            except TimeoutError:
                log.warning("workload %s of %s did not exit after SIGKILL", sp.wid, self.lane.id)

    def _signal(self, sp: Spawned, sig: int) -> None:
        if sp.cgroup:
            self.host.cgroup_signal(sp.cgroup, sig)
        if sp.proc.returncode is None:
            try:
                os.killpg(sp.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    async def release(self, sp: Spawned) -> None:
        """After a workload ends: kill leftovers (background children) and drop its cgroup. On a VM the
        workload's own shell ends its leftovers (agentos.vms.ssh.remote_command)."""
        if sp.cgroup:
            await asyncio.to_thread(self.host.cgroup_remove, sp.cgroup)
        else:
            try:
                os.killpg(sp.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.workloads.pop(sp.wid, None)

    def busy(self) -> bool:
        return bool(self.workloads)

    def stat(self) -> dict:
        return self.host.cgroup_stat(self.lane.key) if self.cgroup else {}
