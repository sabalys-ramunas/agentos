"""Interactive shells: a PTY per session, owned by the node, so a session outlives its caller.

Output goes into a ring buffer with absolute offsets. The MCP reader keeps one cursor; web
terminal clients subscribe to a live stream. When nobody is watching and the agent has not
read a full buffer's worth, the node stops reading the PTY: the program blocks on its own
output (no CPU burn, no memory growth) until someone reads. Nothing is lost that way.
"""

from __future__ import annotations

import asyncio
import codecs
import fcntl
import os
import secrets
import struct
import termios
from datetime import datetime

from openajar.core.models import utcnow
from openajar.env.lanes import LaneRuntime, Spawned, wait_exit
from openajar.env.models import ShellInfo
from openajar.env.sandbox import Workload


class Ring:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.buf = bytearray()
        self.total = 0          # absolute offset of the end of the buffer

    @property
    def start(self) -> int:
        return self.total - len(self.buf)

    def append(self, data: bytes) -> None:
        self.buf += data
        self.total += len(data)
        if len(self.buf) > self.capacity:
            del self.buf[: len(self.buf) - self.capacity]

    def read(self, cursor: int, max_bytes: int) -> tuple[bytes, int, int]:
        """(data, new_cursor, dropped) from `cursor`, at most max_bytes."""
        dropped = 0
        if cursor < self.start:
            dropped = self.start - cursor
            cursor = self.start
        off = cursor - self.start
        data = bytes(self.buf[off: off + max_bytes])
        return data, cursor + len(data), dropped


class ShellSession:
    def __init__(self, sid: str, rt: LaneRuntime, sp: Spawned, master: int, cols: int, rows: int,
                 buffer_bytes: int):
        self.id = sid
        self.rt = rt
        self.sp = sp
        self.master = master
        self.cols, self.rows = cols, rows
        self.ring = Ring(buffer_bytes)
        self.cursor = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.subscribers: set[asyncio.Queue] = set()
        self.created_at: datetime = utcnow()
        self.last_activity: datetime = utcnow()
        self.exited_at: datetime | None = None
        self.exit_code: int | None = None
        self.paused = False
        self.closed = False
        self.eof = False
        self._data = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(master, self._on_readable)
        self._waiter = asyncio.create_task(self._wait_exit())

    @property
    def lane_id(self) -> str:
        return self.rt.lane.id

    @property
    def running(self) -> bool:
        return self.exited_at is None

    def info(self) -> ShellInfo:
        return ShellInfo(id=self.id, lane=self.lane_id, state="running" if self.running else "exited",
                         exit_code=self.exit_code, cols=self.cols, rows=self.rows, created_at=self.created_at,
                         last_activity=self.last_activity, attached=len(self.subscribers),
                         unread_bytes=max(0, self.ring.total - self.cursor))

    # ------------------------------------------------------------------ PTY io
    def _on_readable(self) -> None:
        want = 65536
        if not self.subscribers:
            # never let unread output scroll out: stop reading and let the program wait
            room = self.ring.capacity - (self.ring.total - self.cursor)
            if room <= 0:
                self._pause()
                return
            want = min(want, room)
        try:
            data = os.read(self.master, want)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self.eof = True
            self._stop_reading()
            self._data.set()
            return
        self.ring.append(data)
        for q in list(self.subscribers):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass
        self._data.set()
        if not self.subscribers and self.ring.total - self.cursor >= self.ring.capacity:
            self._pause()

    def _pause(self) -> None:
        if not self.paused and not self.closed:
            self._loop.remove_reader(self.master)
            self.paused = True

    def _resume(self) -> None:
        if self.paused and not self.closed and not self.eof:
            self.paused = False
            self._loop.add_reader(self.master, self._on_readable)

    def _stop_reading(self) -> None:
        if not self.closed:
            try:
                self._loop.remove_reader(self.master)
            except (ValueError, OSError):
                pass

    async def _wait_exit(self) -> None:
        rc = await wait_exit(self.sp.proc)
        # drain what is left in the PTY before declaring exit
        await asyncio.sleep(0.05)
        if not self.closed:
            while True:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.ring.append(data)
                for q in list(self.subscribers):
                    try:
                        q.put_nowait(data)
                    except asyncio.QueueFull:
                        pass
        self.exit_code = rc if rc is None or rc >= 0 else 128 - rc
        self.exited_at = utcnow()
        self._data.set()
        for q in list(self.subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def write(self, data: bytes, timeout_s: float = 5.0) -> int:
        if not self.running or self.closed:
            raise RuntimeError("the shell has exited")
        self.last_activity = utcnow()
        sent = 0
        deadline = self._loop.time() + timeout_s
        while sent < len(data):
            try:
                sent += os.write(self.master, data[sent: sent + 4096])
            except BlockingIOError:
                if self._loop.time() > deadline:
                    break
                await asyncio.sleep(0.01)
        return sent

    async def read(self, *, wait_s: float = 1.0, max_bytes: int = 65536) -> tuple[bytes, int]:
        """New output since the last read: (bytes, dropped). Waits up to wait_s for something."""
        self.last_activity = utcnow()
        if self.cursor >= self.ring.total and self.running and wait_s > 0:
            self._data.clear()
            self._resume()
            try:
                await asyncio.wait_for(self._data.wait(), wait_s)
            except TimeoutError:
                pass
            # small settle so a command's output arrives in one read
            await asyncio.sleep(0.05)
        data, self.cursor, dropped = self.ring.read(self.cursor, max_bytes)
        if self.ring.total - self.cursor < self.ring.capacity:
            self._resume()
        return data, dropped

    def read_text(self, data: bytes) -> str:
        return self.decoder.decode(data)

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(1, min(int(cols), 1000)), max(1, min(int(rows), 1000))
        self.cols, self.rows = cols, rows
        if not self.closed:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.last_activity = utcnow()

    def subscribe(self, *, replay: bool = True, maxsize: int = 1024) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        if replay and self.ring.buf:
            q.put_nowait(bytes(self.ring.buf))
        if not self.running:
            q.put_nowait(None)
        self.subscribers.add(q)
        self._resume()
        self.last_activity = utcnow()
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
        self.last_activity = utcnow()

    async def close(self) -> None:
        if self.closed:
            return
        if self.sp.proc.returncode is None:
            await self.rt.terminate(self.sp, grace=1.0)
        try:
            await asyncio.wait_for(self._waiter, 3)
        except (TimeoutError, asyncio.CancelledError):
            self._waiter.cancel()
        self._stop_reading()
        self.closed = True
        try:
            os.close(self.master)
        except OSError:
            pass
        await self.rt.release(self.sp)
        for q in list(self.subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass


def default_shell(rt: LaneRuntime) -> list[str]:
    if rt.lane.isolated:
        return ["/bin/bash", "--login"]
    for sh in ("/bin/bash", "/bin/sh"):
        if os.path.exists(sh):
            return [sh, "-i"]
    return ["/bin/sh", "-i"]


async def open_session(rt: LaneRuntime, *, cols: int, rows: int, term: str, buffer_bytes: int,
                       argv: list[str] | None = None, env: dict[str, str] | None = None) -> ShellSession:
    master, slave = os.openpty()
    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.set_blocking(master, False)
        sid = "sh_" + secrets.token_hex(6)
        wl = Workload(f"s-{sid[3:]}", argv or default_shell(rt), rt.base_env(env, term=term), pty=True)
        sp = await rt.spawn(wl, kind="shell", stdin=slave, stdout=slave, stderr=slave)
    except BaseException:
        os.close(master)
        os.close(slave)
        raise
    os.close(slave)
    return ShellSession(sid, rt, sp, master, cols, rows, buffer_bytes)
