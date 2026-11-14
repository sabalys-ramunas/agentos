"""Streams over the channel: binary frames with a 16-byte id header and credit flow control.

Both ends use `StreamPipe`. Each direction has its own window, starting at 256 KiB: a sender
never has more than the granted credit in flight, and the receiver hands credit back with
`stream_credit { stream_id, bytes }` (additive, payload bytes only) as it consumes. A peer
that sends past its credit has its stream closed with `credit_exceeded`. Every pipe is
driven by its own task, so a writer waiting for credit never blocks the socket reader that
would deliver that credit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agentos.node.channel import wire

log = logging.getLogger("agentos.channel.streams")

CHUNK_BYTES = 32 * 1024
CREDIT_RETURN = wire.INITIAL_CREDIT // 4
MAX_CREDIT = 1 << 31
MAX_STREAMS = 32
STALL_AFTER = 60.0  # a writer the peer grants no credit for this long gives up (`stalled`)

SendText = Callable[[wire.Envelope], Awaitable[None]]
SendBinary = Callable[[bytes], Awaitable[None]]


class StreamClosed(Exception):
    pass


class StreamStalled(Exception):
    """The receiver stopped granting credit."""


class StreamPipe:
    def __init__(self, stream_id: str, kind: str, send_text: SendText, send_binary: SendBinary, *,
                 lane: str | None = None, stall_after: float | None = None,
                 credit: int = wire.INITIAL_CREDIT):
        self.id = stream_id
        self.kind = kind
        self.lane = lane
        self._send_text = send_text
        self._send_binary = send_binary
        self.send_credit = credit     # what we may still send
        self.recv_credit = credit     # what the peer may still send us
        self.stall_after = stall_after
        self.inbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False
        self.close_reason: str | None = None
        self.sent_bytes = 0
        self.received_bytes = 0
        self.task: asyncio.Task | None = None
        self.extra: Any = None  # what the worker needs besides bytes (a terminal's PTY session)
        self.live: Any = None   # its agentos.core.live entry: a control stream a freeze closes
        self._consumed = 0
        self._credit = asyncio.Event()

    # ---- sending ----
    def add_credit(self, count: int) -> None:
        self.send_credit = min(self.send_credit + count, MAX_CREDIT)
        self._credit.set()

    async def write(self, data: bytes) -> None:
        view = memoryview(data)
        while len(view):
            while self.send_credit <= 0:
                if self.closed:
                    raise StreamClosed()
                self._credit.clear()
                try:
                    async with asyncio.timeout(self.stall_after):
                        await self._credit.wait()
                except TimeoutError:
                    raise StreamStalled() from None
            if self.closed:
                raise StreamClosed()
            count = min(len(view), self.send_credit, CHUNK_BYTES)
            self.send_credit -= count
            self.sent_bytes += count
            await self._send_binary(wire.stream_frame(self.id, bytes(view[:count])))
            view = view[count:]

    # ---- receiving ----
    def deliver(self, payload: bytes) -> bool:
        """A frame from the peer. False when it is past the peer's credit (a protocol violation)."""
        if self.closed:
            return True  # frames can still be in flight after a close
        if len(payload) > self.recv_credit:
            return False
        self.recv_credit -= len(payload)
        self.received_bytes += len(payload)
        self.inbox.put_nowait(payload)
        return True

    async def read(self) -> bytes | None:
        """The next payload, or None once the stream is closed."""
        if self.closed and self.inbox.empty():
            return None
        return await self.inbox.get()

    async def consumed(self, count: int) -> None:
        """Hand credit back once enough input has been processed."""
        self._consumed += count
        if self._consumed >= CREDIT_RETURN and not self.closed:
            grant, self._consumed = self._consumed, 0
            self.recv_credit += grant
            await self._send_text(wire.Envelope("stream_credit", {"stream_id": self.id, "bytes": grant}))

    async def grant(self, count: int) -> None:
        """Give the peer more credit up front (a reader that wants a bigger window)."""
        if count > 0 and not self.closed:
            self.recv_credit += count
            await self._send_text(wire.Envelope("stream_credit", {"stream_id": self.id, "bytes": count}))

    # ---- closing ----
    def mark_closed(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        self._credit.set()
        self.inbox.put_nowait(None)

    async def close(self, reason: str, *, notify: bool = True) -> None:
        was_open = not self.closed
        self.mark_closed(reason)
        if notify and was_open:
            with contextlib.suppress(Exception):
                await self._send_text(wire.Envelope("stream_close", {"stream_id": self.id, "reason": reason}))


class StreamTable:
    """The streams of one connection. They end with the connection."""

    def __init__(self, send_text: SendText, send_binary: SendBinary, *, limit: int = MAX_STREAMS,
                 stall_after: float | None = STALL_AFTER):
        self.send_text = send_text
        self.send_binary = send_binary
        self.limit = limit
        self.stall_after = stall_after
        self.pipes: dict[str, StreamPipe] = {}

    def new(self, stream_id: str, kind: str, **kw) -> StreamPipe:
        stream_id = wire.normal_ulid(stream_id)
        if stream_id in self.pipes:
            raise ValueError("this stream_id is already open")
        if len(self.pipes) >= self.limit:
            raise OverflowError("too many open streams on this connection")
        kw.setdefault("stall_after", self.stall_after)
        pipe = StreamPipe(stream_id, kind, self.send_text, self.send_binary, **kw)
        self.pipes[stream_id] = pipe
        return pipe

    def get(self, stream_id: str) -> StreamPipe | None:
        return self.pipes.get(wire.normal_ulid(stream_id))

    def drop(self, stream_id: str) -> StreamPipe | None:
        return self.pipes.pop(wire.normal_ulid(stream_id), None)

    async def on_binary(self, frame: bytes) -> None:
        try:
            stream_id, payload = wire.parse_stream_frame(frame)
        except wire.WireError:
            log.warning("dropped a binary frame with a bad header")
            return
        pipe = self.pipes.get(stream_id)
        if pipe is None:
            return
        if not pipe.deliver(payload):
            log.warning("stream %s sent past its credit; closing it", stream_id)
            self.pipes.pop(stream_id, None)
            if pipe.task is not None:
                pipe.task.cancel()
            await pipe.close("credit_exceeded")

    def on_credit(self, body: wire.Json) -> None:
        count = body.get("bytes")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise ValueError("stream_credit needs a positive byte count")
        pipe = self.get(str(body.get("stream_id")))
        if pipe is not None:
            pipe.add_credit(count)

    def on_close(self, body: wire.Json) -> None:
        pipe = self.drop(str(body.get("stream_id")))
        if pipe is not None:
            reason = body.get("reason")
            pipe.mark_closed(reason if isinstance(reason, str) else "closed")

    def close_all(self, reason: str = "disconnected") -> None:
        for pipe in list(self.pipes.values()):
            pipe.mark_closed(reason)
            if pipe.task is not None:
                pipe.task.cancel()
        self.pipes.clear()
