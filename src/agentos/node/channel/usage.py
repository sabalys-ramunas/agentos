"""What each warm lane uses, for heartbeat lanes (`cpu_pct`, `mem_mb`, additive).

Read from /proc and the lanes' cgroups, so it costs a directory scan per heartbeat (in a thread,
off the event loop) and nothing on a machine without them (a Mac in development): there a lane
simply has no numbers. A browser lane counts its browser process and every process under it
(Firefox runs its tabs as children). `cpu_pct` is percent of one core over the time since the last
heartbeat (top's convention), so the first heartbeat after a lane warms has `mem_mb` only. An env
lane reports its cgroup's memory. Each lane is measured on its own: one that fails has no numbers,
the others keep theirs. A lane's process is known by its pid and its start time as first seen, so
a pid the system gave to another process after the browser exited is not counted for the lane.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROC = Path("/proc")


def _ticks() -> int:
    try:
        return os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        return 100


def _page() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 4096


class LaneUsage:
    def __init__(self, proc: Path = PROC, clock: Callable[[], float] = time.monotonic):
        self.proc = proc
        self.clock = clock
        self.hz = _ticks()
        self.page = _page()
        self._last: dict[str, tuple[float, float]] = {}  # lane -> (cpu seconds, when)
        self._started: dict[str, dict[int, int]] = {}  # lane -> {its pid: that process's start time}
        self._lock = threading.Lock()  # a heartbeat per connection: two may overlap across a reconnect

    def sample(self, runtime: Any) -> dict[str, dict]:
        """{lane id: {cpu_pct?, mem_mb?}} for the lanes it could measure. Blocking: run it in a thread."""
        with self._lock:
            out: dict[str, dict] = {}
            pids = self._browser_pids(runtime.get("lanes"))
            if pids and self.proc.is_dir():
                table = self._table()
                children: dict[int, list[int]] = {}
                for pid, row in table.items():
                    children.setdefault(row[0], []).append(pid)
                for lane_id, roots in pids.items():
                    try:
                        item = self._lane(lane_id, roots, table, children)
                    except Exception:  # noqa: BLE001, S112  (one lane's numbers are not worth the others')
                        continue
                    if item is not None:
                        out[lane_id] = item
            for gone in (self._last, self._started):
                for lane_id in set(gone) - set(pids):
                    del gone[lane_id]
            out.update(self._env_memory(runtime.get("envs")))
            return out

    def _lane(self, lane_id: str, roots: list[int], table: dict[int, tuple[int, float, int, int]],
              children: dict[int, list[int]]) -> dict | None:
        started = self._started.setdefault(lane_id, {})
        for pid in set(started) - set(roots):
            del started[pid]
        live = []
        for pid in roots:
            if pid not in table:
                continue  # gone (its start time is kept: the pid may come back as another process)
            if started.setdefault(pid, table[pid][3]) != table[pid][3]:
                continue  # the same pid, another process: the lane's browser exited
            live.append(pid)
        tree = _descendants(children, table, live)
        if not tree:
            return None
        cpu = sum(table[pid][1] for pid in tree)
        rss = sum(table[pid][2] for pid in tree)
        item: dict[str, Any] = {"mem_mb": int(rss * self.page // (1024 * 1024))}
        now = self.clock()
        last = self._last.get(lane_id)
        if last is not None and now > last[1] and cpu >= last[0]:
            item["cpu_pct"] = round((cpu - last[0]) / (now - last[1]) * 100, 1)
        self._last[lane_id] = (cpu, now)
        return item

    @staticmethod
    def _browser_pids(lanes: Any) -> dict[str, list[int]]:
        fn = getattr(lanes, "lane_pids", None)
        if not callable(fn):
            return {}
        try:
            return {k: [p for p in v if isinstance(p, int) and p > 0] for k, v in (fn() or {}).items()}
        except Exception:  # noqa: BLE001  (numbers are best effort)
            return {}

    def _table(self) -> dict[int, tuple[int, float, int, int]]:
        """pid -> (parent pid, cpu seconds, resident pages, start time), for every process we can read."""
        table: dict[int, tuple[int, float, int, int]] = {}
        for entry in self.proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
                rss = int((entry / "statm").read_text().split()[1])
            except (OSError, ValueError, IndexError):
                continue
            fields = stat[stat.rfind(")") + 2:].split()  # the name may hold spaces and parentheses
            try:
                table[int(entry.name)] = (int(fields[1]), (int(fields[11]) + int(fields[12])) / self.hz, rss,
                                          int(fields[19]))
            except (ValueError, IndexError):
                continue
        return table

    @staticmethod
    def _env_memory(envs: Any) -> dict[str, dict]:
        host = getattr(envs, "host", None)
        stat = getattr(host, "cgroup_stat", None)
        if envs is None or not callable(stat):
            return {}
        out = {}
        try:
            infos = list(envs.list())
        except Exception:  # noqa: BLE001
            return {}
        for info in infos:
            if str(getattr(info.state, "value", info.state)) not in ("warm", "active"):
                continue
            try:
                memory = (stat(info.lane.key) or {}).get("memory_mb")
            except Exception:  # noqa: BLE001, S112
                continue
            if isinstance(memory, int | float) and memory >= 0:
                out[info.lane.id] = {"mem_mb": int(memory)}
        return out


def _descendants(children: dict[int, list[int]], table: dict[int, Any], roots: list[int]) -> set[int]:
    found: set[int] = set()
    todo = [pid for pid in roots if pid in table]
    while todo:
        pid = todo.pop()
        if pid in found:
            continue
        found.add(pid)
        todo.extend(children.get(pid, ()))
    return found
