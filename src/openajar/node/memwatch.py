"""A memory ceiling for every lane by default, and a watchdog that acts before the kernel does.

During the 2026-11-04 canary one LinkedIn lane's content process reached about 2.3 GB on a 3.8 GB
node, leaving nothing for the other lanes. Two pieces stop that:

The ceiling. `lane_memory_max_mb` in node.toml sets it; 0 turns it off; unset (the default) means
LANE_MEMORY_DEFAULT_MB (openajar.core.config: ten lanes at it fit on 16 GB, and a warm lane on a
logged-in feed takes 0.8 to 1 GB).

Where systemd runs lane scopes (openajar.node.lanes.ScopeLimiter), the ceiling is the scope's
MemoryMax, MemoryHigh at 90% of it. The default ceiling rides only on default scopes (root, cgroup
v2, no memory cap above agentosd); it never asks systemd for a scope by itself, which is what an
explicit cap does. Everywhere else (the hardened unit, a dev box) the watchdog is the only
per-lane limit.

The watchdog (MemoryWatch) is one more step of the lane manager's reaper loop
(openajar.node.lanes.LaneManagerImpl._reap_loop, next to the disk check), at most every WATCH_S
seconds. It reads each warm lane's memory: its scope's memory.current when it has a scope, else
the proportional set size of its browser's process tree (/proc). It acts in the least
destructive way first:

  1. at WARN_SHARE of the ceiling: a lane event `memory_high`, a log line and one card in Needs
     you (a question with no options, from the node; agentosd wires it with `install`), once
     per episode;
  2. at RECYCLE_SHARE (where MemoryHigh starts reclaiming) for RECYCLE_AFTER looks in a row: the
     lane's browser is closed gracefully (cookies and the profile are kept, the page is gone, a
     claim's holder hears claim_lost), and the next call opens it fresh. Never while a call or a
     flow or connector run is on the lane, while an agent drives it through the commit gate (a
     publish may be in flight), or while a person has it or has a live view open: the watchdog
     tries again on its next look, and the scope's MemoryMax stays the hard stop.

It watches the node as well: below half of memory_floor_mb available for RECYCLE_AFTER looks it
closes the biggest lane it safely can. Warning about a node short of memory is the health
service's job (openajar.ops.health, which names the biggest lane from the watchdog's last look).
Without /proc (a Mac) it sees nothing and does nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openajar.core.config import LANE_MEMORY_DEFAULT_MB
from openajar.core.models import Principal, parse_lane_id

log = logging.getLogger("openajar.lanes.memory")

WATCH_S = 30.0           # at most this often (the reaper runs every 30 s or sooner)
WARN_SHARE = 0.8
RECYCLE_SHARE = 0.9      # openajar.node.lanes.MEMORY_HIGH_SHARE: reclaim pressure starts here
CLEAR_SHARE = 0.7        # an episode ends (a later climb warns again) below this
RECYCLE_AFTER = 3        # looks in a row over RECYCLE_SHARE before the browser is closed
WATCHDOG = Principal(kind="node", id="memory-watchdog", owner="owner")


# ---- the ceiling ---------------------------------------------------------------------------

def resolve_ceiling(value: int | None) -> tuple[int | None, str]:
    """(ceiling MB or None, where it comes from: "set", "default" or "off") for a
    `lane_memory_max_mb` value: 0 is off, a number is set, None is LANE_MEMORY_DEFAULT_MB."""
    if value == 0:
        return None, "off"
    if value:
        return int(value), "set"
    return LANE_MEMORY_DEFAULT_MB, "default"


def lane_memory_ceiling(config: Any) -> tuple[int | None, str]:
    """resolve_ceiling for a node.toml."""
    return resolve_ceiling(getattr(config, "lane_memory_max_mb", None))


# ---- what a lane uses ----------------------------------------------------------------------

def _children(proc: Path) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        out.setdefault(ppid, []).append(int(entry.name))
    return out


def _pss_kb(proc: Path, pid: int) -> int:
    for name, key in (("smaps_rollup", "Pss:"), ("status", "VmRSS:")):
        try:
            for line in (proc / str(pid) / name).read_text().splitlines():
                if line.startswith(key):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            continue
    return 0


class LaneMemory:
    """Reads what each lane's browser uses, in MB: its scope's memory.current, else the summed
    PSS of its process tree. None where /proc is not there."""

    def __init__(self, proc: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup")):
        self.proc = proc
        self.cgroup_root = cgroup_root

    def available(self) -> bool:
        return (self.proc / "self").exists()

    def _scope_bytes(self, pid: int) -> int | None:
        try:
            lines = (self.proc / str(pid) / "cgroup").read_text().splitlines()
        except OSError:
            return None
        path = next((line[3:] for line in lines if line.startswith("0::")), "")
        if "/openajar-lane-" not in path:
            return None
        try:
            return int((self.cgroup_root / path.lstrip("/") / "memory.current").read_text().strip())
        except (OSError, ValueError):
            return None

    def usage(self, lane_pids: dict[str, list[int]]) -> dict[str, int]:
        if not lane_pids or not self.available():
            return {}
        out: dict[str, int] = {}
        children: dict[int, list[int]] | None = None
        for lane_id, pids in lane_pids.items():
            scoped = self._scope_bytes(pids[0]) if pids else None
            if scoped is not None:
                out[lane_id] = scoped // (1024 * 1024)
                continue
            if children is None:
                try:
                    children = _children(self.proc)
                except OSError:
                    return out
            seen: set[int] = set()
            todo = list(pids)
            while todo:
                pid = todo.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                todo += children.get(pid, [])
            out[lane_id] = sum(_pss_kb(self.proc, pid) for pid in seen) // 1024
        return out


# ---- the watchdog --------------------------------------------------------------------------

@dataclass
class _Episode:
    warned: bool = False
    over: int = 0


@dataclass
class MemoryWatch:
    """See the module docstring. `lanes` is the lane manager that owns the watch
    (openajar.node.lanes.LaneManagerImpl): it calls step() from its reaper loop, and the watch
    calls back its lane_pids(), recycle_for_memory(), _event(), memory_probe() and
    memory_floor_mb. `ask(lane_id, question)` puts a card in Needs you; without it (a CLI
    manager, a test) a warning is a lane event and a log line."""
    lanes: Any
    ceiling_mb: int | None = None
    probe: Callable[[dict[str, list[int]]], dict[str, int]] | None = None
    ask: Callable[[str, str], None] | None = None
    episodes: dict[str, _Episode] = field(default_factory=dict)
    last: dict[str, int] = field(default_factory=dict)   # MB per lane at the last look
    short: int = 0   # looks in a row with the node under half its memory floor
    _at: float | None = None

    def __post_init__(self) -> None:
        if self.probe is None:
            self.probe = LaneMemory().usage

    async def step(self, now: float) -> list[tuple[str, str]]:
        """tick(), unless the last one was less than WATCH_S ago (`now` is the manager's
        monotonic clock): the reaper may run more often than the watch needs to."""
        if self._at is not None and now - self._at < WATCH_S:
            return []
        self._at = now
        return await self.tick()

    async def tick(self) -> list[tuple[str, str]]:
        """One look at every warm lane and at the node. Returns what it did: (lane, "warn" |
        "recycle" | "wait"), "" for the lane when the node had none it could close."""
        pids = self.lanes.lane_pids()
        usage = self.probe(pids) if pids else {}
        self.last = usage
        for gone in set(self.episodes) - set(usage):
            self.episodes.pop(gone, None)
        done: list[tuple[str, str]] = []
        if self.ceiling_mb:
            for lane_id, mb in sorted(usage.items()):
                done += await self._lane(lane_id, mb)
        done += await self._node(usage)
        return done

    async def _lane(self, lane_id: str, mb: int) -> list[tuple[str, str]]:
        assert self.ceiling_mb
        share = mb / self.ceiling_mb
        ep = self.episodes.setdefault(lane_id, _Episode())
        if share < CLEAR_SHARE:
            self.episodes.pop(lane_id, None)
            return []
        out = []
        if share >= WARN_SHARE and not ep.warned:
            ep.warned = True
            self._warn(lane_id, mb, f"{lane_id}'s browser uses {mb} MB of its {self.ceiling_mb} MB memory ceiling")
            out.append((lane_id, "warn"))
        ep.over = ep.over + 1 if share >= RECYCLE_SHARE else 0
        if ep.over >= RECYCLE_AFTER:
            why = f"its browser used {mb} MB of its {self.ceiling_mb} MB ceiling for {ep.over} looks in a row"
            if await self.lanes.recycle_for_memory(lane_id, why):
                self.episodes.pop(lane_id, None)
                out.append((lane_id, "recycle"))
            else:
                out.append((lane_id, "wait"))
        return out

    async def _node(self, usage: dict[str, int]) -> list[tuple[str, str]]:
        floor = int(self.lanes.memory_floor_mb or 0)
        available = self.lanes.memory_probe() if floor else None
        if available is None or available >= floor // 2:
            self.short = 0
            return []
        self.short += 1
        if self.short < RECYCLE_AFTER:
            return []
        why = f"the node had only {available} MB free for {self.short} looks in a row"
        for lane_id, _mb in sorted(usage.items(), key=lambda kv: -kv[1]):
            if await self.lanes.recycle_for_memory(lane_id, why):
                self.short = 0
                return [(lane_id, "recycle")]
        return [("", "wait")]

    def _warn(self, lane_id: str, mb: int, what: str) -> None:
        next_step = ("If it stays this high, openajar closes the lane's browser gracefully (cookies kept, the page "
                     "is gone) once no call, run or person is on it; the next call opens it fresh.")
        log.warning("%s. %s", what, next_step)
        self.lanes._event(lane_id, "memory_high", {"mb": mb, "ceiling_mb": self.ceiling_mb, "detail": what})
        if self.ask is not None:
            try:
                self.ask(lane_id, f"{what}. {next_step}")
            except Exception:  # a card that cannot be made never stops the watchdog
                log.warning("could not put the memory warning in Needs you", exc_info=True)


def install(runtime: Any) -> None:
    """agentosd: the watchdog's warnings become cards in Needs you (the policy's approvals, looked
    up when a warning comes, so the order services are built in does not matter)."""
    watch = getattr(runtime.get("lanes"), "memory_watch", None)
    if watch is None:  # the dev fakes have no browsers to watch
        return

    def ask(lane_id: str, question: str) -> None:
        policy = runtime.get("policy")
        approvals = getattr(policy, "approvals", None)
        if approvals is None:
            return
        try:
            workspace = parse_lane_id(lane_id)[0]
        except ValueError:
            workspace = None
        ttl = int(getattr(getattr(policy, "cfg", None), "question_ttl_s", 24 * 3600))
        approvals.create_question(question=question, options=None, requester=WATCHDOG, grant_id=None,
                                  grant_path=None, ttl_s=ttl, workspace=workspace)

    watch.ask = ask
