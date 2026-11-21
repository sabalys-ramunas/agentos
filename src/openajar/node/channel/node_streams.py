"""What a node streams to its controller: a lane's live view (`vnc`) and terminal PTYs (`terminal`).

`vnc` goes through `openajar.node.vnc_relay.VncRelay`, the same relay the node's own viewer uses,
so the same input rules apply: the picture always flows, but keys, pointer and clipboard reach
x11vnc only while the stream was opened with `params.mode = "control"` AND a human holds control
of the lane. Input stops (with held keys released) the moment control goes back to agents.

`terminal` attaches to an env lane's PTY (openajar.env): output bytes stream out, and
`resize { stream_id, cols, rows }` resizes it. Input bytes reach the PTY only while the stream was
opened with `params.mode = "control"` AND the person named in `params.by` holds the lane: a
`lane_control` `take` from the controller with the same `by` (TerminalHolds; env lanes have no
lane control of their own). Typing counts as activity, and a hold nobody types into for
`control_idle_seconds` goes back to agents. `stream_ready` names the shell (`session`), so a
reconnect can pass `params.session` and attach to it again. Detaching leaves the shell running,
as the panel's web terminal does.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from openajar.core.errors import (
    InvalidRequest,
    LaneNotFound,
    LaneQuarantined,
    OpenajarError,
    ServiceUnavailable,
)
from openajar.core.models import ControlState, LaneKind, LaneState
from openajar.node.channel import wire
from openajar.node.channel.streams import StreamPipe
from openajar.node.vnc_relay import CLOSE_PROTOCOL, CLOSE_VNC_UNREACHABLE, VncRelay, upstream

log = logging.getLogger("openajar.channel.streams")

Worker = Callable[[StreamPipe], Awaitable[str]]


class PipeSocket:
    """A StreamPipe shaped like the Starlette WebSocket that VncRelay drives. Credit for one
    payload is handed back when the relay asks for the next, that is once the previous one has
    been written and drained to x11vnc."""

    def __init__(self, pipe: StreamPipe):
        self.pipe = pipe
        self.close_code: int | None = None
        self._held = 0

    async def receive(self) -> dict:
        if self._held:
            await self.pipe.consumed(self._held)
            self._held = 0
        data = await self.pipe.read()
        if data is None:
            return {"type": "websocket.disconnect", "code": 1000}
        self._held = len(data)
        return {"type": "websocket.receive", "bytes": data}

    async def send_bytes(self, data: bytes) -> None:
        await self.pipe.write(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code


class ControlView:
    """Who holds a lane: the viewer's ControlHub when the lane manager has control listeners,
    else a direct read. Fails closed (an unreadable lane counts as paused)."""

    def __init__(self, lanes: Any):
        self.lanes = lanes
        self.hub = None
        if callable(getattr(lanes, "add_control_listener", None)):
            from openajar.node.viewer_state import ControlHub

            self.hub = ControlHub(lanes)

    def state(self, lane_id: str) -> ControlState:
        if self.hub is not None:
            return self.hub.state(lane_id)
        try:
            return self.lanes.get(lane_id).control.state
        except Exception:  # noqa: BLE001
            return ControlState.PAUSED

    def watch(self, lane_id: str, callback: Callable[[], None]) -> Callable[[], None]:
        if self.hub is None:
            return lambda: None
        return self.hub.watch(lane_id, lambda _control: callback())


def _lane(lanes: Any, lane_id: Any) -> Any:
    if not isinstance(lane_id, str):
        raise InvalidRequest("a stream needs a lane")
    if lanes is None:
        raise ServiceUnavailable("this node has no browser lanes")
    try:
        return lanes.get(lane_id)
    except LaneNotFound:
        raise
    except OpenajarError:
        raise
    except Exception:  # noqa: BLE001
        raise LaneNotFound(f"no lane {lane_id}") from None


def prepare_vnc(lanes: Any, control: ControlView | None, lane_id: Any, params: dict) -> Worker:
    """Check everything that can fail before `stream_ready` goes out."""
    info = _lane(lanes, lane_id)
    if info.spec.kind not in (LaneKind.BROWSER, LaneKind.ANDROID):
        raise InvalidRequest(f"lane {lane_id} has no live view")
    if info.state is LaneState.QUARANTINED:
        raise LaneQuarantined(f"lane {lane_id} is quarantined")
    viewer = lanes.view(lane_id)
    if viewer is None:
        raise ServiceUnavailable(f"lane {lane_id} is {info.state.value}; open it first")
    mode = params.get("mode", "view")
    if mode not in ("view", "control"):
        raise InvalidRequest("params.mode must be view or control")
    target = upstream(viewer)

    async def run(pipe: StreamPipe) -> str:
        def allow_input() -> bool:
            return mode == "control" and control is not None and control.state(lane_id) == ControlState.HUMAN

        def on_input() -> None:
            note = getattr(lanes, "note_human_input", None)
            if callable(note):
                note(lane_id)

        socket = PipeSocket(pipe)
        relay = VncRelay(socket, **target, allow_input=allow_input, on_input=on_input)
        unwatch = control.watch(lane_id, relay.control_changed) if control is not None else (lambda: None)
        try:
            await relay.run()
        finally:
            unwatch()
        if socket.close_code == CLOSE_VNC_UNREACHABLE:
            return "vnc_unreachable"
        if socket.close_code == CLOSE_PROTOCOL:
            return "protocol_error"
        return "eof" if socket.close_code is not None else "closed"

    return run


def holder_name(by: Any) -> str:
    """How a person at the controller is named as a lane's holder (lane_control `by`)."""
    return f"controller:{by}" if isinstance(by, str) and 0 < len(by) <= 80 else "controller"


DEFAULT_IDLE_SECONDS = 600  # NodeConfig.control_idle_seconds' default


class TerminalHolds:
    """Who holds each terminal lane for the controller: taken by `lane_control`, kept while the
    holder types, and given back after `idle_seconds` without input (or by `hand_back`). In memory:
    a node restart hands every terminal back to agents, so input stops (fails closed).

    A hold always ends: `idle_seconds` of 0 (a browser lane's "never", openajar.node.lanes) is the
    default here, since the controller would otherwise keep a shell for good. Idle time is measured
    on `clock` (monotonic); `since` in lane events is wall time."""

    def __init__(self, idle_seconds: float, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.idle_seconds = idle_seconds if idle_seconds > 0 else DEFAULT_IDLE_SECONDS
        self.clock = clock
        self.wall = wall
        self._held: dict[str, dict] = {}

    def take(self, lane_id: str, by: str) -> None:
        self._held[lane_id] = {"by": by, "since": self.wall(), "active": self.clock()}

    def hand_back(self, lane_id: str) -> bool:
        return self._held.pop(lane_id, None) is not None

    def holder(self, lane_id: str) -> str | None:
        held = self._held.get(lane_id)
        if held is None:
            return None
        if self.clock() - held["active"] > self.idle_seconds:
            self._held.pop(lane_id, None)
            log.info("terminal lane %s went back to agents after %.0f s without input", lane_id, self.idle_seconds)
            return None
        return held["by"]

    def note_input(self, lane_id: str) -> None:
        held = self._held.get(lane_id)
        if held is not None:
            held["active"] = self.clock()

    def reap(self) -> list[str]:
        """Hand back the holds that went idle; returns their lanes."""
        return [lane for lane in list(self._held) if self.holder(lane) is None]

    def control(self, lane_id: str) -> dict | None:
        """The lane's `control` for lane events, or None while agents have it."""
        by = self.holder(lane_id)
        if by is None:
            return None
        return {"state": ControlState.HUMAN.value, "by": by, "since": wire.iso(self._held[lane_id]["since"]),
                "takeback_requested": False}


def _size(body: dict) -> tuple[int, int]:
    cols, rows = body.get("cols", 120), body.get("rows", 32)
    if not (isinstance(cols, int) and isinstance(rows, int) and not isinstance(cols, bool)
            and not isinstance(rows, bool) and 0 < cols <= 1000 and 0 < rows <= 1000):
        raise InvalidRequest("cols and rows must be whole numbers from 1 to 1000")
    return cols, rows


async def prepare_terminal(envs: Any, lane_id: Any, params: dict,
                           holds: TerminalHolds | None = None) -> tuple[Worker, Any]:
    """Attach to `params.session`, or open a new shell. Returns the worker and the PTY session.
    Input goes through only in `control` mode, while `params.by` holds the lane in `holds`."""
    if envs is None:
        raise ServiceUnavailable("this node has no terminal lanes")
    if not isinstance(lane_id, str):
        raise InvalidRequest("a terminal stream needs a lane")
    mode = params.get("mode", "view")
    if mode not in ("view", "control"):
        raise InvalidRequest("params.mode must be view or control")
    by = params.get("by")
    if by is not None and not (isinstance(by, str) and 0 < len(by) <= 80):
        raise InvalidRequest("params.by names the person at the controller (at most 80 characters)")
    me = holder_name(by)
    cols, rows = _size(params)
    session_id = params.get("session")
    if session_id is not None:
        if not isinstance(session_id, str):
            raise InvalidRequest("params.session must be a shell session id")
        session = envs.shell(session_id)
        if session.lane_id != lane_id:
            raise LaneNotFound(f"shell {session_id} is not on lane {lane_id}")
    else:
        session = await envs.shell_open(lane_id, cols=cols, rows=rows, term="xterm-256color")

    def allow_input() -> bool:
        return mode == "control" and holds is not None and holds.holder(lane_id) == me

    async def run(pipe: StreamPipe) -> str:
        feed = session.subscribe(replay=True)

        async def out() -> str:
            while True:
                data = await feed.get()
                if data is None:
                    return "exit"
                await pipe.write(data)

        async def inp() -> str:
            while True:
                data = await pipe.read()
                if data is None:
                    return "closed"
                if session.running and allow_input():
                    await session.write(data)
                    holds.note_input(lane_id)  # type: ignore[union-attr]  (allow_input needs holds)
                await pipe.consumed(len(data))

        tasks = [asyncio.create_task(out()), asyncio.create_task(inp())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            return next(iter(done)).result()
        finally:
            for task in tasks:
                task.cancel()
            session.unsubscribe(feed)

    return run, session


def resize(session: Any, body: dict) -> None:
    cols, rows = _size(body)
    session.resize(cols, rows)
