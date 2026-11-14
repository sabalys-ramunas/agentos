"""The stream messages of the channel: stream_open, credit, resize and close for a lane's live
view and terminal PTYs (the workers are in node_streams.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from websockets.exceptions import ConnectionClosed

from agentos.core import live as live_sockets
from agentos.core.errors import AgentosError, Frozen, InvalidRequest, ServiceUnavailable
from agentos.node.channel import files, logstream, node_streams, wire
from agentos.node.channel.connection import Connection
from agentos.node.channel.streams import StreamClosed, StreamPipe, StreamStalled

log = logging.getLogger("agentos.channel")


class StreamMessagesMixin:
    """Stream messages: stream_open, credit, resize and close for live views and terminals."""

    async def _push(self, conn: Connection, pipe: StreamPipe, data: bytes) -> None:
        reason = "eof"
        try:
            await pipe.write(data)
        except StreamStalled:
            reason = "stalled"
        except (StreamClosed, ConnectionClosed):
            return
        finally:
            conn.streams.drop(pipe.id)
        await pipe.close(reason)

    # ---- streams ----
    async def _on_stream_open(self, conn: Connection, message: wire.Envelope) -> None:
        body = message.body
        stream_id, kind, lane = wire.normal_ulid(body.get("stream_id")), body.get("kind"), body.get("lane")
        params = body.get("params") if body.get("params") is not None else {}
        try:
            if not wire.is_ulid(stream_id):
                raise InvalidRequest("stream_id must be a ULID")
            if conn.streams.get(stream_id) is not None:
                raise InvalidRequest("this stream_id is already open")
            if not isinstance(params, dict):
                raise InvalidRequest("stream params must be an object")
            if len(conn.streams.pipes) >= conn.streams.limit:
                raise ServiceUnavailable("too many open streams on this connection")
            extra = None
            mode = str(params.get("mode", "view"))
            self.ceiling.check_stream(str(kind), mode)
            if mode == "control":
                self._refuse_frozen(lane)
            if kind == "vnc":
                worker = node_streams.prepare_vnc(self.runtime.get("lanes"), self._control, lane, params)
            elif kind == "terminal":
                worker, extra = await node_streams.prepare_terminal(self.runtime.get("envs"), lane, params,
                                                                    self.terminal_holds)
            elif kind == "file":
                worker = await files.prepare_file(self.runtime, self.config.home, lane, params)
            elif kind == "logs":
                worker = logstream.prepare_logs(self.runtime, self.config, params)
            else:
                raise InvalidRequest(f"unknown stream kind {str(kind)[:20]!r}")
        except AgentosError as exc:
            await conn.send(wire.error_envelope(exc.code, exc.message, message.id or None,
                                                stream_id=stream_id if isinstance(stream_id, str) else None))
            return
        stall = self.timings.stream_stall if kind == "file" else None  # a file has an end; a viewer may idle
        pipe = conn.streams.new(stream_id, kind, lane=lane if isinstance(lane, str) else None,
                                **({"stall_after": stall} if stall is not None else {}))
        pipe.extra = extra
        if mode == "control":
            self._track_live(conn, pipe)
        ready = {"stream_id": stream_id}
        if kind == "terminal" and isinstance(getattr(extra, "id", None), str):
            ready["session"] = extra.id  # a reconnect passes it as params.session to attach to the same shell
        try:
            await conn.send(wire.Envelope("stream_ready", ready, re=message.id or None))
        except BaseException:
            conn.streams.drop(pipe.id)
            if pipe.live is not None:
                live_sockets.of(self.runtime).remove(pipe.live)
            raise
        pipe.task = asyncio.create_task(self._supervise(conn, pipe, worker), name=f"stream-{stream_id}")

    async def _supervise(self, conn: Connection, pipe: StreamPipe, worker: Any) -> None:
        reason = "error"
        try:
            reason = await worker(pipe)
        except asyncio.CancelledError:
            raise
        except StreamClosed:
            reason = pipe.close_reason or "closed"
        except StreamStalled:
            reason = "stalled"
        except ConnectionClosed:
            return
        except Exception:
            log.exception("channel: stream %s failed", pipe.id)
        finally:
            conn.streams.drop(pipe.id)
            if pipe.live is not None:
                live_sockets.of(self.runtime).remove(pipe.live)
        await pipe.close(reason)

    def _refuse_frozen(self, lane: Any) -> None:
        """During a freeze a person at the controller may still look, but not type (docs/PROTOCOL.md)."""
        freeze = self.runtime.get("freeze")
        frozen_for = getattr(freeze, "frozen_for", None)
        rec = frozen_for(lane if isinstance(lane, str) else None) if callable(frozen_for) else None
        if rec is not None:
            raise Frozen(freeze.message(lane if isinstance(lane, str) else None, rec))

    def _track_live(self, conn: Connection, pipe: StreamPipe) -> None:
        """A control stream closes (reason `frozen`) once a freeze covers its lane (agentos.core.live)."""
        async def recheck() -> tuple[int, str] | None:
            try:
                self._refuse_frozen(pipe.lane)
            except Frozen:
                return live_sockets.CLOSE_DENIED, "frozen"
            return None

        def end(entry: live_sockets.LiveEntry) -> None:
            live_sockets.of(self.runtime).remove(entry)  # also when the worker never got to start
            conn.streams.drop(pipe.id)
            if pipe.task is not None:
                pipe.task.cancel()
            task = asyncio.get_running_loop().create_task(pipe.close(entry.reason or "frozen"))
            self._stream_closers.add(task)
            task.add_done_callback(self._stream_closers.discard)

        pipe.live = live_sockets.of(self.runtime).add(
            live_sockets.LiveEntry(f"channel:{pipe.kind}", recheck, lane=pipe.lane, on_close=end))

    async def _on_stream_close(self, conn: Connection, message: wire.Envelope) -> None:
        conn.streams.on_close(message.body)

    async def _on_stream_credit(self, conn: Connection, message: wire.Envelope) -> None:
        conn.streams.on_credit(message.body)

    async def _on_resize(self, conn: Connection, message: wire.Envelope) -> None:
        pipe = conn.streams.get(str(message.body.get("stream_id")))
        session = getattr(pipe, "extra", None) if pipe is not None else None
        if session is None:
            raise InvalidRequest("resize needs an open terminal stream")
        node_streams.resize(session, message.body)
