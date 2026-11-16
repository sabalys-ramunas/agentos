"""One-shot commands inside a lane: capped, binary-safe output, exit code, duration, timeouts."""

from __future__ import annotations

import asyncio
import base64
import signal as _signal
from typing import Any, BinaryIO

from agentos.env import oom
from agentos.env.lanes import LaneRuntime, new_wid, wait_exit
from agentos.env.models import ExecResult
from agentos.env.sandbox import SharedSession, Workload

LEFTOVER_GRACE_S = 0.5


class Capture:
    """Keeps the first quarter and the last three quarters of `cap` bytes; counts the rest."""

    def __init__(self, cap: int):
        self.head_cap = max(cap // 4, 1)
        self.tail_cap = max(cap - self.head_cap, 1)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, data: bytes) -> None:
        self.total += len(data)
        room = self.head_cap - len(self.head)
        if room > 0:
            self.head += data[:room]
            data = data[room:]
        if data:
            self.tail += data
            if len(self.tail) > self.tail_cap:
                del self.tail[: len(self.tail) - self.tail_cap]

    @property
    def dropped(self) -> int:
        return self.total - len(self.head) - len(self.tail)

    def shrunk(self, cap: int) -> Capture:
        """The same output kept in `cap` bytes: the first quarter and the last three quarters of it."""
        if len(self.head) + len(self.tail) <= cap:
            return self
        out = Capture(cap)
        if not self.dropped:  # head and tail are one piece: keep its ends
            out.feed(bytes(self.head + self.tail))
            return out
        out.head = self.head[:out.head_cap]
        out.tail = self.tail[len(self.tail) - out.tail_cap:]
        out.total = self.total
        return out


def _cleaned(cap: Capture, session: SharedSession) -> Capture:
    """The captured stream through session.clean (agentos.vms.sshmux.Lease.clean), its sizes kept true."""
    out = Capture(cap.head_cap + cap.tail_cap)
    if not cap.dropped:
        out.feed(session.clean(bytes(cap.head + cap.tail)))
        return out
    out.head, out.tail = bytearray(session.clean(bytes(cap.head))), bytearray(session.clean(bytes(cap.tail)))
    out.total = cap.total - (len(cap.head) - len(out.head)) - (len(cap.tail) - len(out.tail))
    return out


def fit(out: Capture, err: Capture, budget: int) -> tuple[Capture, Capture]:
    """stdout and stderr within `budget` bytes in all, for a result an agent's client takes inline.
    stderr (where errors are) keeps what it has up to a quarter of the budget, or more when stdout
    leaves room; stdout gets the rest."""
    kept_out, kept_err = len(out.head) + len(out.tail), len(err.head) + len(err.tail)
    if kept_out + kept_err <= budget:
        return out, err
    err_room = min(kept_err, max(budget // 4, budget - kept_out))
    return out.shrunk(max(budget - err_room, 1)), err.shrunk(max(err_room, 1))


def _trim_utf8_end(b: bytes) -> tuple[bytes, bytes]:
    """Split off an incomplete multi-byte sequence at the end."""
    for i in range(1, min(4, len(b)) + 1):
        c = b[-i]
        if c & 0xC0 == 0x80:
            continue
        need = 2 if c & 0xE0 == 0xC0 else 3 if c & 0xF0 == 0xE0 else 4 if c & 0xF8 == 0xF0 else 1
        return (b[:-i], b[-i:]) if need > i else (b, b"")
    return b, b""


def _trim_utf8_start(b: bytes) -> bytes:
    i = 0
    while i < min(3, len(b)) and b[i] & 0xC0 == 0x80:
        i += 1
    return b[i:]


def encode_output(cap: Capture) -> tuple[str, str]:
    """(text, encoding). Text with a marker when the middle was dropped; base64 for binary."""
    head, tail = bytes(cap.head), bytes(cap.tail)
    dropped = cap.dropped
    h, h_rest = _trim_utf8_end(head) if (tail or dropped) else (head, b"")
    t = _trim_utf8_start(tail) if dropped else tail
    try:
        if b"\x00" in head or b"\x00" in tail:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "nul")
        if not dropped:
            text = (head + tail).decode("utf-8")
        else:
            lost = dropped + len(h_rest) + (len(tail) - len(t))
            text = (h.decode("utf-8") + f"\n[agentos: {lost} bytes truncated here, of {cap.total} in all; the "
                    "start and the end are kept]\n" + t.decode("utf-8"))
        return text, "utf-8"
    except UnicodeDecodeError:
        return base64.b64encode(head + tail).decode(), "base64"


async def _pump(stream: asyncio.StreamReader | None, cap: Capture, signs: oom.Signs | None = None) -> None:
    if stream is None:
        return
    while True:
        data = await stream.read(65536)
        if not data:
            return
        cap.feed(data)
        if signs is not None:
            signs.feed(data)


def exit_status(rt: LaneRuntime, rc: int | None) -> tuple[int | None, str | None]:
    """(exit_code, signal name) of a finished workload. A signal death has no exit code; bubblewrap
    reports a payload killed by a signal as 128+N, which keeps its exit code and names the signal."""
    if rc is not None and rc < 0:
        try:
            return None, _signal.Signals(-rc).name
        except ValueError:
            return None, f"signal {-rc}"
    if rc is not None and rc > 128 and rt.lane.isolated:
        try:
            return rc, _signal.Signals(rc - 128).name
        except ValueError:
            pass
    return rc, None


async def _exit_or(proc: asyncio.subprocess.Process, timeout_s: float, stop: asyncio.Event) -> bool:
    """Wait for the process to exit (TimeoutError past timeout_s) or for `stop`: True when stopped first."""
    waiter = asyncio.ensure_future(wait_exit(proc, timeout_s))
    stopper = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (waiter, stopper):
            if not task.done():
                task.cancel()
    if waiter.done() and not waiter.cancelled():
        waiter.result()  # TimeoutError
        return False
    return True


async def run(rt: LaneRuntime, argv: list[str], *, timeout_s: float, stdin: bytes | None = None,
              max_bytes: int = 65536, env: dict[str, str] | None = None, install: bool = False,
              cwd: str | None = None, kind: str = "exec", stdin_file: BinaryIO | None = None,
              budget: int | None = None, stdout_file: BinaryIO | None = None,
              stdout_limit: int | None = None, oom_check: bool = False) -> ExecResult:
    """`stdin_file` (an open file) is the workload's stdin as it is, for input too large to hold in
    memory (a sync's tar archive); `stdin` is fed from memory. `max_bytes` is kept of each stream;
    `budget`, when given, bounds both together (fit). `stdout_file` (an open file) takes all of
    stdout instead, for output too large to hold (an archive out of the lane): past `stdout_limit`
    bytes the workload is ended, nothing more is written, and the result has stdout_bytes over the
    limit and truncated set. `oom_check`: on a VM, a command that failed, or printed a sign of a kill
    (a "Killed" line, an exit code 137: agentos.env.oom.Signs), says what the kernel killed for memory
    while it ran (`oom`), at the cost of one more ssh call in those cases only."""
    loop = asyncio.get_running_loop()
    oom_before = rt.stat().get("oom_kills", 0)
    wl = Workload(new_wid("i" if install else "x"), argv, rt.base_env(env), cwd=cwd, install=install,
                  oom_record=oom_check, mux=True)
    start = loop.time()
    stdin_arg: Any = stdin_file if stdin_file is not None else (
        asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL)
    sp = await rt.spawn(wl, kind=kind, stdin=stdin_arg, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE)
    out, err = Capture(max_bytes), Capture(max_bytes)
    proc = sp.proc

    async def feed() -> None:
        if stdin is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

    over = asyncio.Event()

    async def to_file() -> None:
        if proc.stdout is None or stdout_file is None:
            return
        while True:
            data = await proc.stdout.read(65536)
            if not data:
                return
            out.total += len(data)
            if stdout_limit is not None and out.total > stdout_limit:
                over.set()
                return
            stdout_file.write(data)

    signs = oom.Signs() if oom_check else None  # a "Killed" line or an exit code 137 in the output
    readers = asyncio.gather(to_file() if stdout_file is not None else _pump(proc.stdout, out, signs),
                             _pump(proc.stderr, err, signs), feed())
    timed_out = abandoned = False
    try:
        try:
            if stdout_file is None:
                await wait_exit(proc, timeout_s)
            elif await _exit_or(proc, timeout_s, over):
                await rt.terminate(sp, grace=1.0)  # past stdout_limit: nothing reads its output any more
        except TimeoutError:
            timed_out = True
            await rt.terminate(sp, grace=2.0)
        # background children may hold the pipes open: give them a moment, then end the workload
        try:
            await asyncio.wait_for(asyncio.shield(readers), LEFTOVER_GRACE_S)
        except TimeoutError:
            await rt.kill(sp)
            try:
                await asyncio.wait_for(readers, 2)
            except TimeoutError:
                readers.cancel()
    except asyncio.CancelledError:
        if proc.returncode is None:  # the caller went away: what it runs ends, on a VM too, before it is released
            abandoned = True
            readers.cancel()
            rt.abandon(sp)
        raise
    finally:
        if not abandoned:
            if sp.session is not None:  # ssh's own lines about a shared connection are never the command's
                err = _cleaned(err, sp.session)
            await rt.release(sp)
    duration = loop.time() - start
    exit_code, sig_name = exit_status(rt, proc.returncode)
    oom_after = rt.stat().get("oom_kills", 0)
    kills = None
    if oom_check and (exit_code != 0 or sig_name or (signs is not None and signs.seen)):
        kills = await rt.oom_kills(wl.wid, elapsed_s=duration, final=True)
    if budget is not None:
        out, err = fit(out, err, budget)
    so, so_enc = encode_output(out)
    se, se_enc = encode_output(err)
    return ExecResult(
        exit_code=exit_code, signal=sig_name, stdout=so, stderr=se, stdout_encoding=so_enc,
        stderr_encoding=se_enc, stdout_bytes=out.total, stderr_bytes=err.total,
        truncated=bool(out.dropped or err.dropped or over.is_set()), timed_out=timed_out,
        oom_killed=oom_after > oom_before or any(k.get("in_job") for k in kills or []), oom=kills or None,
        duration_s=round(duration, 3))
