"""Reading what a server sends, under a deadline a trickle cannot push back.

A socket timeout starts again on every byte, so a server that sends one byte a second keeps a read
open for ever, and a thread cannot be killed from outside. So every read here sets the socket's
timeout to what is left of one `Deadline`, and everything a server can send has a cap. Going past
either fails that one connection, and `MailFailed` says which in a word.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from typing import Any

CHUNK = 65_536


class MailFailed(Exception):
    """A connection or a login did not work. `kind` says why, in a word: no server text comes with it."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class Deadline:
    """One end time for a whole job. It never moves."""

    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic):
        self.clock, self.seconds, self.end = clock, seconds, clock() + seconds

    def left(self) -> float:
        """What is left, or MailFailed when nothing is."""
        left = self.end - self.clock()
        if left <= 0:
            raise MailFailed("too_slow")
        return left

    def past_half(self) -> bool:
        return self.end - self.clock() < self.seconds / 2


class Wire:
    """A socket's bytes as a file for reading, with a line cap and a cap on what one answer may hold."""

    def __init__(self, sock: Any, deadline: Deadline, *, max_line: int, max_total: int):
        self.sock, self.deadline, self.max_line, self.max_total = sock, deadline, max_line, max_total
        self.buffer = bytearray()
        self.total = 0

    def begin(self) -> None:
        """A new command: the cap on an answer counts from here."""
        self.total = 0

    def _fill(self) -> bool:
        self.sock.settimeout(self.deadline.left())
        try:
            chunk = self.sock.recv(CHUNK)
        except TimeoutError:
            raise MailFailed("too_slow") from None
        self.total += len(chunk)
        if self.total > self.max_total:
            raise MailFailed("too_much")
        self.buffer += chunk
        return bool(chunk)

    def _take(self, size: int) -> bytes:
        out = bytes(self.buffer[:size])
        del self.buffer[:size]
        return out

    def readline(self, limit: int = -1) -> bytes:
        """One line. Past the cap is `too_much`, unless a caller asked for less, as http.client does."""
        cap = self.max_line if limit < 0 else min(limit, self.max_line)
        scanned = 0
        while True:
            at = self.buffer.find(b"\n", scanned)
            if at >= 0:
                return self._take(min(at + 1, cap))
            if len(self.buffer) >= cap:
                if limit < 0 or limit > self.max_line:
                    raise MailFailed("too_much")
                return self._take(cap)
            scanned = len(self.buffer)
            if not self._fill():
                return self._take(len(self.buffer))

    def read(self, size: int = -1) -> bytes:
        while (size < 0 or len(self.buffer) < size) and self._fill():
            pass
        return self._take(len(self.buffer) if size < 0 else size)

    def read1(self, size: int = -1) -> bytes:
        if not self.buffer:
            self._fill()
        return self._take(len(self.buffer) if size < 0 else min(size, len(self.buffer)))

    def readinto(self, target: Any) -> int:
        data = self.read(len(target))
        target[:len(data)] = data
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def closed(self) -> bool:
        return False


class WireSocket:
    """A connected socket for http.client: its reads and writes come under the deadline."""

    def __init__(self, sock: socket.socket, deadline: Deadline, *, max_line: int, max_total: int):
        self.sock, self.deadline = sock, deadline
        self.wire = Wire(sock, deadline, max_line=max_line, max_total=max_total)

    def makefile(self, *_: Any, **__: Any) -> Wire:
        return self.wire

    def sendall(self, data: bytes) -> None:
        self.sock.settimeout(self.deadline.left())
        self.sock.sendall(data)

    def close(self) -> None:
        """http.client calls this while its response still reads, as a file from `makefile` would
        keep a socket open. Whoever made the socket closes it."""
