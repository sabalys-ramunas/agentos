"""Supervised services: restart policy with exponential backoff, health checks, rotating logs.

A service runs inside its lane like any other workload, so it is boxed by the lane's
namespaces, cgroup and egress forwarder. Service lanes never share the node network, so a
listening socket is only reachable inside the lane (its own loopback). Health probes run
inside the lane too: a command, or an HTTP GET opened from the lane's network namespace.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from openajar.core.models import utcnow
from openajar.env.lanes import LaneRuntime, Spawned, wait_exit
from openajar.env.models import ServiceConfig, ServiceStatus
from openajar.env.sandbox import Workload

log = logging.getLogger("openajar.env.services")


class RotatingLog:
    def __init__(self, path: Path, max_bytes: int, backups: int):
        self.path = path
        self.max_bytes = max(1024, max_bytes)
        self.backups = max(1, backups)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._size = path.stat().st_size if path.exists() else 0

    def write(self, data: bytes) -> None:
        while data:
            room = self.max_bytes - self._size
            if room <= 0:
                self.rotate()
                room = self.max_bytes
            chunk, data = data[:room], data[room:]
            with open(self.path, "ab") as f:
                f.write(chunk)
            self._size += len(chunk)

    def note(self, text: str) -> None:
        self.write(f"[openajar {utcnow().isoformat(timespec='seconds')}] {text}\n".encode())

    def rotate(self) -> None:
        """service.log -> service.log.1 -> ... -> service.log.N (the oldest is dropped)."""
        name = self.path.name
        oldest = self.path.with_name(f"{name}.{self.backups}")
        if oldest.exists():
            oldest.unlink()
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{name}.{i}")
            if src.exists():
                src.rename(self.path.with_name(f"{name}.{i + 1}"))
        if self.path.exists():
            self.path.rename(self.path.with_name(f"{name}.1"))
        self._size = 0

    def tail(self, lines: int) -> str:
        chunks: list[bytes] = []
        need = lines + 1
        for p in [self.path, *(self.path.with_name(f"{self.path.name}.{i}") for i in range(1, self.backups + 1))]:
            if not p.exists():
                continue
            data = p.read_bytes()
            chunks.insert(0, data)
            if sum(c.count(b"\n") for c in chunks) >= need:
                break
        text = b"".join(chunks).decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])


async def http_probe(rt: LaneRuntime, url: str, timeout_s: float) -> bool:
    u = urlsplit(url)
    host = "127.0.0.1" if u.hostname in ("localhost", None) else u.hostname
    port = u.port or 80
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    loop = asyncio.get_running_loop()
    import socket as _s
    family = _s.AF_INET6 if ":" in host else _s.AF_INET
    sock = await asyncio.to_thread(rt.host.socket_in_netns, rt.netns, family)
    try:
        sock.setblocking(False)
        await asyncio.wait_for(loop.sock_connect(sock, (host, port)), timeout_s)
        await loop.sock_sendall(sock, f"GET {path} HTTP/1.0\r\nHost: {host}:{port}\r\n\r\n".encode())
        data = await asyncio.wait_for(loop.sock_recv(sock, 64), timeout_s)
        parts = data.split(b" ", 2)
        return len(parts) >= 2 and parts[0].startswith(b"HTTP/") and parts[1][:1] in (b"2", b"3")
    except (TimeoutError, OSError):
        return False
    finally:
        sock.close()


class Supervisor:
    """Keeps one service command running in one lane."""

    def __init__(self, lane_id: str, cfg: ServiceConfig, logfile: RotatingLog, *,
                 get_runtime: Callable[[], Awaitable[LaneRuntime]],
                 run_check: Callable[[LaneRuntime, list[str], float], Awaitable[bool]]):
        self.lane_id = lane_id
        self.cfg = cfg
        self.log = logfile
        self.get_runtime = get_runtime
        self.run_check = run_check
        self.status = ServiceStatus(lane=lane_id, desired="stopped", state="stopped", log_path=str(logfile.path))
        self.sp: Spawned | None = None
        self.rt: LaneRuntime | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._restart_now = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, expires_at: datetime | None = None) -> None:
        self.status.desired = "running"
        self.status.expires_at = expires_at
        self.status.detail = None
        if not self.running:
            self.status.restarts = 0
            self._task = asyncio.create_task(self._loop(), name=f"service:{self.lane_id}")
        else:
            self._wake.set()

    async def stop(self, reason: str = "stopped") -> None:
        self.status.desired = "stopped"
        self._wake.set()
        if self.sp and self.sp.proc.returncode is None and self.rt:
            self.status.state = "stopping"
            self.log.note(f"stopping ({reason})")
            await self.rt.terminate(self.sp, grace=self.cfg.stop_timeout_s)
        if self._task:
            try:
                await asyncio.wait_for(self._task, self.cfg.stop_timeout_s + 5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self.status.state = "stopped"
        self.status.pid = None
        self.status.health = "none"
        self.status.next_restart_in_s = None

    def expired(self) -> bool:
        exp = self.status.expires_at
        return exp is not None and utcnow() >= exp

    async def _loop(self) -> None:
        backoff = self.cfg.backoff_initial_s
        loop = asyncio.get_running_loop()
        while self.status.desired == "running":
            if self.expired():
                self.log.note("grant time ran out; stopping")
                self.status.desired = "stopped"
                self.status.detail = "expired"
                break
            self.status.state = "starting"
            try:
                rt = await self.get_runtime()
                self.rt = rt
                env = rt.base_env(self.cfg.env)
                self.sp = await rt.spawn(Workload("svc", self.cfg.command, env), kind="service",
                                         stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                         stderr=asyncio.subprocess.STDOUT)
            except Exception as e:  # noqa: BLE001
                self.log.note(f"could not start: {e}")
                self.status.state = "backoff"
                self.status.detail = str(e)[:300]
                if not await self._backoff(backoff):
                    break
                backoff = min(backoff * 2, self.cfg.backoff_max_s)
                continue
            started = loop.time()
            self.status.state = "running"
            self.status.pid = self.sp.proc.pid
            self.status.started_at = utcnow()
            self.status.next_restart_in_s = None
            self.status.health = "starting" if self.cfg.health else "none"
            self.log.note(f"started: {' '.join(self.cfg.command)}")
            pump = asyncio.create_task(self._pump(self.sp))
            health = asyncio.create_task(self._health(rt, self.sp)) if self.cfg.health else None
            rc = await wait_exit(self.sp.proc)
            if health:
                health.cancel()
            try:
                await asyncio.wait_for(pump, 2)
            except TimeoutError:
                pump.cancel()
            await rt.release(self.sp)
            uptime = loop.time() - started
            self.status.last_exit = rc
            self.status.last_exit_at = utcnow()
            self.status.pid = None
            self.log.note(f"exited with {rc} after {uptime:.1f}s")
            self.sp = None
            if self.status.desired != "running":
                break
            if self.cfg.restart == "never" or (self.cfg.restart == "on-failure" and rc == 0
                                               and not self._restart_now):
                self.status.desired = "stopped"
                break
            self._restart_now = False
            if uptime >= self.cfg.stable_after_s:
                backoff = self.cfg.backoff_initial_s
                self.status.restarts = 0
            self.status.restarts += 1
            if self.cfg.max_restarts is not None and self.status.restarts > self.cfg.max_restarts:
                self.log.note(f"gave up after {self.cfg.max_restarts} restarts")
                self.status.state = "failed"
                self.status.detail = f"crash loop: gave up after {self.cfg.max_restarts} restarts"
                self.status.desired = "stopped"
                return
            self.status.state = "backoff"
            self.log.note(f"restarting in {backoff:.1f}s")
            if not await self._backoff(backoff):
                break
            backoff = min(backoff * 2, self.cfg.backoff_max_s)
        self.status.state = "stopped"
        self.status.health = "none"

    async def _backoff(self, delay: float) -> bool:
        """Sleep `delay` unless stopped. Returns True to continue."""
        self.status.next_restart_in_s = round(delay, 2)
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), delay)
        except TimeoutError:
            pass
        self.status.next_restart_in_s = None
        return self.status.desired == "running"

    async def _pump(self, sp: Spawned) -> None:
        stream = sp.proc.stdout
        if stream is None:
            return
        while True:
            data = await stream.read(65536)
            if not data:
                return
            self.log.write(data)

    async def _health(self, rt: LaneRuntime, sp: Spawned) -> None:
        hc = self.cfg.health
        assert hc is not None
        await asyncio.sleep(hc.start_period_s)
        fails = 0
        while sp.proc.returncode is None:
            if hc.http:
                ok = await http_probe(rt, hc.http, hc.timeout_s)
            else:
                ok = await self.run_check(rt, hc.cmd or ["true"], hc.timeout_s)
            if ok:
                fails = 0
                self.status.health = "healthy"
            else:
                fails += 1
                if fails >= hc.retries:
                    self.status.health = "unhealthy"
                    self.log.note(f"health check failed {fails} times; restarting")
                    self._restart_now = True
                    await rt.terminate(sp, grace=self.cfg.stop_timeout_s)
                    return
            await asyncio.sleep(hc.interval_s)


def expiry(ttl_s: float | None) -> datetime | None:
    return utcnow() + timedelta(seconds=ttl_s) if ttl_s else None
