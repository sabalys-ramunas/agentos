"""Memory, disk and per-lane limits of the lane manager (agentos.node.lanes).

The probes the warm cap reads (available_memory_mb, disk_free_bytes), the floors below which no lane
warms (a memory floor, and a disk floor because a browser that cannot write its cookies can lose the
login: AUD-23), the systemd scope each lane's browser runs in (ScopeLimiter, default_scopes_fit),
and the manager's side of it (ResourcesMixin: reservations for other work, the memory watchdog's
recycle, memory_status, the disk warning of the reaper loop). The watchdog itself is
agentos.node.memwatch. The manager reads the probes through this module when it is made, so a test
patches them here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agentos.core.models import LaneState
from agentos.core.system import meminfo_mb
from agentos.node.lanes.base import _HOLDS_SLOT, ResourcesLow

log = logging.getLogger("agentos.lanes")

DEFAULT_MEMORY_FLOOR_MB = 800          # a Camoufox lane needs about 400 to 600 MB
DEFAULT_DISK_FLOOR_BYTES = 2 * 1024**3  # AUD-23


# ---- resource probes ------------------------------------------------------------------

def available_memory_mb() -> int | None:
    """MemAvailable in MB, or None where there is no /proc (agentos.core.system.read_meminfo)."""
    return meminfo_mb("MemAvailable")


def disk_free_bytes(path: Path) -> int | None:
    """Free bytes on the filesystem holding `path` (its nearest existing parent)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def _size(n: int) -> str:
    return f"{n / 1024**3:.1f} GB" if n >= 1024**3 else f"{n / 1024**2:.0f} MB"


LANE_TASKS_MAX = 4096  # threads and processes of one lane's browser (a busy lane runs about 400)
# Every lane's scope sits in this slice (under agentos.slice, beside system.slice and user.slice), so
# all lanes together weigh like one unit against agentosd and the rest of the system, however many
# are warm, and equally among themselves inside it. systemd makes the slice on first use.
LANE_SLICE = "agentos-lanes.slice"
MEMORY_HIGH_SHARE = 0.9  # reclaim pressure starts here, before MemoryMax would kill a process


class ScopeLimiter:
    """Per-lane limits: moves a lane's browser processes into their own transient systemd scope, so
    one runaway page cannot take the node or the other lanes down. Every scope has the same CPU
    weight inside LANE_SLICE, so busy lanes share the CPU per lane and not per process (a lane with
    eight tabs' worth of content processes gets no more than a lane with one) and twenty busy lanes
    take no more from agentosd than two, and its own cap on tasks (without
    scopes, agentosd and every browser share the service's TasksMax). A memory cap (MemoryMax,
    MemoryHigh at 90% of it, no swap; LANE_MEMORY_DEFAULT_MB unless node.toml says otherwise) and a
    CPU cap (percent of one core, when set) are added.
    OOMPolicy=continue: the kernel killing one content process for memory leaves the rest of the
    lane's browser running (a scope's default, stop, takes the whole browser down; measured on
    systemd 255), and the lane manager notices a dead main process as it does any crash.
    Content processes the browser starts later inherit the scope. Only where systemd runs and
    busctl is installed; elsewhere it does nothing. The scope goes away with the browser; a browser
    in a scope outlives an agentosd that is killed outright, until the next start sweeps it."""

    def __init__(self, max_mb: int | None = None, *, cpu_max_pct: int | None = None,
                 tasks_max: int = LANE_TASKS_MAX, run: Callable[[list[str]], Awaitable[tuple[int, str]]] | None = None):
        self.max_bytes = int(max_mb) * 1024 * 1024 if max_mb else None
        self.cpu_max_pct = int(cpu_max_pct) if cpu_max_pct else None
        self.tasks_max = int(tasks_max)
        self._run = run or _run_command
        self._oom_policy = True  # dropped after a systemd that cannot set OOMPolicy on a scope refuses it
        # What systemd actually did: a cap set in node.toml that systemd refuses (busctl denied, as
        # for the hardened unit's unprivileged user) is not in force, and status() says so.
        self.applied = 0
        self.refused = 0
        self.working: bool | None = None  # the last attempt; None before the first
        self.error: str | None = None

    @staticmethod
    def supported() -> bool:
        return Path("/run/systemd/system").is_dir() and shutil.which("busctl") is not None

    @staticmethod
    def unit_name(lane_id: str, pid: int) -> str:
        return f"agentos-lane-{lane_id.replace('/', '--')}-{pid}.scope"

    def properties(self) -> list[tuple[str, str, str]]:
        """The scope's limits as (name, D-Bus type, value)."""
        props = [("Slice", "s", LANE_SLICE), ("CPUWeight", "t", "100"), ("TasksMax", "t", str(self.tasks_max))]
        if self._oom_policy:
            props.append(("OOMPolicy", "s", "continue"))
        if self.max_bytes:
            props += [("MemoryMax", "t", str(self.max_bytes)),
                      ("MemoryHigh", "t", str(int(self.max_bytes * MEMORY_HIGH_SHARE))), ("MemorySwapMax", "t", "0")]
        if self.cpu_max_pct:
            props.append(("CPUQuotaPerSecUSec", "t", str(self.cpu_max_pct * 10_000)))
        return props

    def describe(self) -> str:
        parts = [f"memory {_size(self.max_bytes)}" if self.max_bytes else None,
                 f"CPU {self.cpu_max_pct}% of a core" if self.cpu_max_pct else None, f"{self.tasks_max} tasks"]
        return ", ".join(p for p in parts if p)

    def _argv(self, lane_id: str, unit: str, pids: list[int]) -> list[str]:
        props = self.properties()
        return ["busctl", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))", unit, "fail",
                str(len(props) + 3), "PIDs", "au", str(len(pids)), *map(str, pids),
                *(item for prop in props for item in prop),
                "CollectMode", "s", "inactive-or-failed", "Description", "s", f"agentos lane {lane_id}", "0"]

    async def apply(self, lane_id: str, pids: list[int]) -> str | None:
        """The scope unit's name, or None when it could not be made."""
        pids = sorted({p for p in pids if p and p > 1})
        if not pids:
            return None
        unit = self.unit_name(lane_id, pids[0])
        try:
            code, out = await self._run(self._argv(lane_id, unit, pids))
            if code != 0 and self._oom_policy and "OOMPolicy" in out:
                log.info("systemd cannot set OOMPolicy on a scope here; lane scopes go without it")
                self._oom_policy = False
                code, out = await self._run(self._argv(lane_id, unit, pids))
        except Exception as e:  # noqa: BLE001 - a limit is optional
            self._refused(lane_id, str(e))
            return None
        if code != 0:
            self._refused(lane_id, out.strip()[:200] or f"busctl exit {code}")
            return None
        if self.working is False:
            log.info("lane scopes work again")
        self.applied += 1
        self.working, self.error = True, None
        log.info("%s: browser limited to %s in %s", lane_id, self.describe(), unit)
        return unit

    def _refused(self, lane_id: str, error: str) -> None:
        """Warn when scopes stop working (once, not for every lane), then only at debug level."""
        level = logging.DEBUG if self.working is False else logging.WARNING
        log.log(level, "%s: could not limit its browser (%s), so it runs without %s; later lanes are "
                "logged at debug level", lane_id, error, self.describe())
        self.refused += 1
        self.working, self.error = False, error

    def status(self) -> dict[str, Any]:
        return {"configured": self.describe(), "applied": self.applied, "refused": self.refused,
                "working": self.working, "error": self.error}


def default_scopes_fit(*, proc_cgroup: Path = Path("/proc/self/cgroup"), cgroup_root: Path = Path("/sys/fs/cgroup"),
                       euid: int | None = None) -> bool:
    """Whether lanes get scopes without an explicit cap: only as root (a scope on the system manager
    needs it), and only when no cgroup from this daemon's own up to the root caps memory (memory.max
    or memory.high: the hardened unit caps the whole service, and an operator may cap a slice above
    it), which browsers in scopes of their own would leave. On cgroup v1 or hybrid, or when a file
    cannot be read, no default scopes: the browsers stay in the daemon's cgroup, as before scopes."""
    if euid is None:
        euid = os.geteuid() if hasattr(os, "geteuid") else -1
    if euid != 0:
        return False
    try:
        lines = [line for line in proc_cgroup.read_text().splitlines() if line]
        if len(lines) != 1 or not lines[0].startswith("0::"):
            return False  # v1 or hybrid: memory is a v1 controller, so the v2 files say nothing
        level = cgroup_root / lines[0][3:].strip().lstrip("/")
        while level != cgroup_root and cgroup_root in level.parents:
            for name in ("memory.max", "memory.high"):
                if (level / name).read_text().strip() != "max":
                    return False
            level = level.parent
    except OSError:
        return False
    return True


async def _run_command(argv: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 10.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "timed out"
    return proc.returncode or 0, out.decode(errors="replace")


class ResourcesMixin:
    """Memory and disk of the lane manager (agentos.node.lanes.LaneManagerImpl)."""

    def _memory_short(self) -> tuple[int, int] | None:
        """(available MB after reservations, floor MB) when below the floor, else None."""
        floor = self.memory_floor_mb
        if not floor:
            return None
        available = self.memory_probe()
        if available is None:
            return None
        effective = available - sum(self._reservations.values())
        return (effective, floor) if effective < floor else None

    def _memory_error(self, lane_id: str, short: tuple[int, int]) -> ResourcesLow:
        effective, floor = short
        reserved = sum(self._reservations.values())
        note = f", {reserved} MB of it reserved for other work" if reserved else ""
        return ResourcesLow(
            f"only {max(effective, 0)} MB of memory is free on this node (agentos keeps {floor} MB free{note}) and "
            f"no idle lane can be closed to make room, so {lane_id} was not opened. Close a lane you do not "
            "need (browser_close), or try again once running work finishes.")

    def _check_disk(self, lane_id: str) -> None:
        floor = self.disk_floor_bytes
        if not floor:
            return
        free = self.disk_probe(self.config.profile_dir(lane_id))
        if free is not None and free < floor:
            raise ResourcesLow(
                f"only {_size(free)} is free on the disk that holds {lane_id}'s profile (agentos keeps "
                f"{_size(floor)} free), so the lane was not opened: a browser that cannot write its cookies "
                "can lose the login. Free space on the node (old screenshots, traces, backups), then open it again.")

    def reserve_memory(self, key: str, mb: int) -> None:
        """Count `mb` MB against the memory floor until release_memory(key), for work that is
        about to start outside the lane manager (a brain). Raises ResourcesLow when that would
        take the node below the floor. Reserving the same key again replaces it."""
        if mb <= 0:
            self.release_memory(key)
            return
        floor = self.memory_floor_mb or 0
        available = self.memory_probe()
        if available is not None and floor:
            others = sum(v for k, v in self._reservations.items() if k != key)
            left = available - others - mb
            if left < floor:
                raise ResourcesLow(f"{key} needs {mb} MB, but only {max(available - others, 0)} MB is free on this "
                                   f"node (agentos keeps {floor} MB free). Close a lane or try again later.")
        self._reservations[key] = int(mb)

    def release_memory(self, key: str) -> None:
        self._reservations.pop(key, None)

    async def recycle_for_memory(self, lane_id: str, why: str) -> bool:
        """Close a lane's browser for memory (the watchdog, agentos.node.memwatch), gracefully: its cookies and
        profile are kept, the page is gone, and the next call opens it fresh. Only when nothing is
        mid-way on it: no call in flight or waiting, no flow or connector run, no agent driving it
        through the commit gate (a publish may be under way), no person in control or watching a
        live view. False when it did not close (the watchdog looks again later). A claim's holder
        hears claim_lost on its next call."""
        lane = self._lanes.get(lane_id)

        def safe() -> bool:
            if lane is None or lane.state != LaneState.WARM or lane.session is None or lane.evicting:
                return False
            if lane.users or lane.viewers or lane.access.busy() or lane.access.queue or self._held_by_human(lane_id):
                return False
            claim = lane.access.claim
            if claim is not None and claim.implicit:
                return False
            guard = getattr(lane.session, "guard", None)
            return not (callable(getattr(guard, "driving", None)) and guard.driving())

        if lane is not None and not safe() and not lane.op.locked() and self._only_overdue(lane_id, lane):
            # the only thing "on" the lane is a call past its deadline that has not let go: what the
            # watchdog promised ("once no call, run or person is on it") has come, so close it now
            await self.close_stuck(lane_id, [c for c in lane.calls if not c.task.done()], why)
            if lane.state == LaneState.COLD:
                self._event(lane_id, "memory_recycled", {"detail": why, "stuck_call": True})
                return True
            return False
        if lane is None or not safe() or lane.op.locked():
            return False
        async with lane.op:
            if not safe():
                return False
            log.warning("%s: closing its browser because %s (cookies kept; the next call opens it again)",
                        lane_id, why)
            await self._teardown(lane_id, lane)
            if lane.claimed():
                self._end_claim(lane_id, lane, "closed", f"{why}, so the lane's browser was closed and its page "
                                "is gone")
        self._event(lane_id, "memory_recycled", {"detail": why})
        await self._notify()
        return True

    def _only_overdue(self, lane_id: str, lane: Any) -> bool:
        """Whether every caller inside the lane is a call past its deadline (and no person is on it)."""
        if lane.session is None or lane.viewers or self._held_by_human(lane_id):
            return False
        inside = lane.access.inside()
        calls = [c for c in lane.calls if not c.task.done()]
        if not inside or not calls or not all(c.overdue for c in calls):
            return False
        mine = {id(c.waiter) for c in calls}
        return all(id(w) in mine for w in inside)

    def lane_pids(self) -> dict[str, list[int]]:
        """The browser processes of each lane that has one (for what a lane uses; read only)."""
        out: dict[str, list[int]] = {}
        for lane_id, lane in list(self._lanes.items()):
            session = lane.session
            if session is None:
                continue
            pids = [p for p in [getattr(session, "pid", None), *(getattr(session, "pids", None) or [])] if p]
            if pids:
                out[lane_id] = list(dict.fromkeys(pids))
        return out

    def memory_status(self) -> dict[str, Any]:
        """What the warm cap sees: available and reserved memory, the floors, free disk. The per-lane
        caps only while systemd applies them (the last scope was made); `lane_limits` has the rest."""
        limiter = self.limiter
        applied = limiter is not None and limiter.working is True
        return {
            "available_mb": self.memory_probe(),
            "reserved_mb": sum(self._reservations.values()),
            "reservations": dict(self._reservations),
            "floor_mb": self.memory_floor_mb,
            "disk_free_bytes": self.disk_probe(self.config.home),
            "disk_floor_bytes": self.disk_floor_bytes,
            "lane_memory_max_mb": limiter.max_bytes // (1024 * 1024) if applied and limiter.max_bytes else None,
            # the ceiling the memory watchdog holds lanes to (set, the default, or off),
            # and what each warm lane used at its last look (MB)
            "lane_memory_ceiling_mb": self.lane_memory_ceiling[0],
            "lane_memory_ceiling_source": self.lane_memory_ceiling[1],
            "lanes_mb": dict(self.memory_watch.last),
            "lane_cpu_max_pct": limiter.cpu_max_pct if applied else None,
            "lane_limits": limiter.status() if limiter is not None else None,
        }

    def _watch_disk(self) -> None:
        if not self.disk_floor_bytes:
            return
        free = self.disk_probe(self.config.home)
        low = free is not None and free < self.disk_floor_bytes
        if low and not self._disk_warned:
            warm = [lid for lid, lane in self._lanes.items() if lane.state in _HOLDS_SLOT]
            log.error("only %s is free on the disk of %s; no lane warms until there is %s (warm now: %s)",
                      _size(free), self.config.home, _size(self.disk_floor_bytes), ", ".join(warm) or "none")
        self._disk_warned = low
