"""One connection to the controller: the channel's timings, its reconnect backoff, and what is
kept per live WebSocket.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

from websockets.asyncio.client import ClientConnection

from agentos.node.channel import wire
from agentos.node.channel.streams import StreamTable


@dataclass
class Timings:
    ping_interval: float = wire.PING_INTERVAL
    dead_after: float = wire.DEAD_AFTER
    heartbeat: float = wire.HEARTBEAT_INTERVAL
    backoff_min: float = wire.BACKOFF_MIN
    backoff_max: float = wire.BACKOFF_MAX
    handshake: float = wire.HANDSHAKE_TIMEOUT
    result_ttl: float = wire.RESULT_TTL
    lane_poll: float = 2.0
    trace_flush: float = 0.5
    housekeeping: float = 30.0
    stream_stall: float = 30.0
    reports: float = 30.0          # how often node reports (inventory, services, ...) are rebuilt
    report_min: float = 60.0       # never one report kind more often than this
    doctor_after: float = 20.0     # the first doctor run, after start
    doctor_every: float = 900.0


class Backoff:
    """Exponential from `low` to `high`, plus up to 25% jitter on top."""

    def __init__(self, low: float, high: float, rng: random.Random | None = None):
        self.low, self.high = low, high
        self.rng = rng or random.Random()
        self.attempt = 0

    def next(self) -> float:
        base = min(self.high, self.low * float(2 ** min(self.attempt, 30)))
        self.attempt += 1
        return base + self.rng.uniform(0, base * 0.25)

    def reset(self) -> None:
        self.attempt = 0


class Connection:
    """One authenticated socket."""

    def __init__(self, ws: ClientConnection):
        self.ws = ws
        self.last_rx = time.monotonic()
        self.sent_seq = 0
        self._lock = asyncio.Lock()
        self.streams = StreamTable(self.send, self.send_binary)
        self.tasks: set[asyncio.Task] = set()            # handlers running beside the reader (_off_loop)
        self.chains: dict[str, asyncio.Task] = {}        # the last of them per ordering key

    async def send(self, envelope: wire.Envelope) -> None:
        text = envelope.encode()
        async with self._lock:
            await self.ws.send(text)

    async def send_raw(self, frame: str) -> None:
        async with self._lock:
            await self.ws.send(frame)

    async def send_binary(self, frame: bytes) -> None:
        async with self._lock:
            await self.ws.send(frame)
