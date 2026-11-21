"""Process helpers shared by the node, ops and dev tools (Linux /proc; harmless no-ops elsewhere)."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Iterator
from pathlib import Path


def iter_processes() -> Iterator[tuple[int, list[str]]]:
    """(pid, argv) for every process we can read."""
    proc = Path("/proc")
    if not proc.is_dir():
        return
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if raw:
            yield int(entry.name), [a.decode(errors="replace") for a in raw.rstrip(b"\0").split(b"\0")]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie still answers kill(0); treat it as gone
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return True


def process_group(pgid: int) -> list[int]:
    """Pids of every live process in process group `pgid` (from /proc; empty elsewhere)."""
    members: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return members
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 2 and fields[2] == str(pgid) and fields[0] != "Z":
            members.append(int(entry.name))
    return sorted(members)


def _signal_tree(pid: int, sig: int) -> None:
    """Signal pid's process group when pid leads it (we start everything in its own
    session), otherwise only pid, so we never hit a group we do not own."""
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def kill_processes(pids: list[int], *, grace: float = 3.0) -> None:
    """SIGTERM each process (group), wait up to `grace`, then SIGKILL what is left."""
    pids = [p for p in pids if p != os.getpid()]
    for pid in pids:
        _signal_tree(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(pid_alive(p) for p in pids):
        await asyncio.sleep(0.1)
    for pid in pids:
        if pid_alive(pid):
            _signal_tree(pid, signal.SIGKILL)
