"""The outbox: events, tool definitions, traces and lane states queued for the controller in
`seq` order, so nothing is lost while it is away.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from agentos.node.channel import bridge, wire

log = logging.getLogger("agentos.channel")


TOOLS_PART_BYTES = 768 * 1024  # tool definitions per `node` event, well under the 1 MiB message limit
TOOL_DESCRIPTION_CHARS = 4000  # what the controller keeps of a description


class OutboxMixin:
    """The outbox: events, tools, traces and lane states queued for the controller, kept while it is away."""

    # ---- the outbox ----
    def _enqueue(self, kind: str, body: dict) -> None:
        try:
            self.store.enqueue(kind, body)
        except wire.Oversize:
            log.error("channel: dropped a %s over the 1 MiB limit", kind)
            return
        self._outbox_wake.set()

    def _enqueue_event(self, kind: str, payload: dict) -> None:
        self._enqueue("event", {"kind": kind, "payload": payload})

    async def announce_tools(self) -> None:
        """The tool list changed while agentosd runs (agentos.mcp.reload: a connector installed or
        removed): send the controller the whole list again. Offline, the next welcome sends it."""
        if not self.connected:
            return
        self._enqueue_tools(await bridge.tool_defs(self.runtime.get("mcp")))
        self._outbox_wake.set()

    def _enqueue_tools(self, tools: list[dict]) -> None:
        """The tool list as one `node` event, or as parts `tools_part { index, count }` when it would
        not fit one message. A single definition too large to send is logged, not sent."""
        parts: list[list[dict]] = [[]]
        size = 0
        for tool in tools:
            if isinstance(tool.get("description"), str):
                tool = {**tool, "description": tool["description"][:TOOL_DESCRIPTION_CHARS]}
            length = len(json.dumps(tool, separators=(",", ":"), ensure_ascii=False).encode())
            if length > TOOLS_PART_BYTES:
                log.error("channel: tool %s is too large to announce (%d bytes); the controller will not list it",
                          str(tool.get("name"))[:64], length)
                continue
            if parts[-1] and size + length > TOOLS_PART_BYTES:
                parts.append([])
                size = 0
            parts[-1].append(tool)
            size += length
        if len(parts) == 1:
            self._enqueue_event("node", {"tools": parts[0]})
            return
        for index, part in enumerate(parts):
            self._enqueue_event("node", {"tools": part, "tools_part": {"index": index, "count": len(parts)}})

    def _enqueue_traces(self, events: list[dict]) -> None:
        try:
            self.store.enqueue("traces", {"events": events})
        except wire.Oversize:
            if len(events) == 1:
                log.error("channel: dropped a trace over the size limit")
                return
            middle = len(events) // 2
            self._enqueue_traces(events[:middle])
            self._enqueue_traces(events[middle:])
            return
        self._outbox_wake.set()

    def _flush_trace_buffer(self) -> None:
        pending, self._traces = self._traces, []
        events = [bridge.trace_event(p) for p in pending]
        for start in range(0, len(events), wire.MAX_TRACES_PER_BATCH):
            self._enqueue_traces(events[start:start + wire.MAX_TRACES_PER_BATCH])

    async def _flush_traces(self) -> None:
        while True:
            await asyncio.sleep(self.timings.trace_flush)
            if self._traces:
                self._flush_trace_buffer()

    def _trace(self, kind: str, **fields: Any) -> None:
        traces = self.runtime.get("traces")
        if traces is None:
            return
        from agentos.policy.traces import TraceEvent

        try:
            traces.write(TraceEvent(kind=kind, principal="controller", principal_kind="controller", **fields))
        except Exception:
            log.exception("channel: trace failed")

    # ---- lanes ----
    def _all_lanes(self) -> list[dict]:
        """Every lane as the controller sees it; a terminal lane a person at the controller holds
        shows that in `control` (node_streams.TerminalHolds)."""
        lanes = bridge.all_lanes(self.runtime, self.config.name)
        for lane in lanes:
            if lane.get("control") is None and (control := self.terminal_holds.control(lane["id"])) is not None:
                lane["control"] = control
        return lanes

    async def _watch_lanes(self) -> None:
        while True:
            self._lane_wake.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.timings.lane_poll):
                    await self._lane_wake.wait()
            self.check_lanes()

    def check_lanes(self) -> None:
        current = self._all_lanes()
        changed = False
        seen: dict[str, tuple] = {}
        for lane in current:
            digest = bridge.lane_digest(lane)
            seen[lane["id"]] = digest
            if self._lanes_seen.get(lane["id"]) != digest:
                self._enqueue_event("lane", lane)
                changed = True
        if set(seen) != set(self._lanes_seen):
            changed = True
        self._lanes_seen = seen
        if changed:
            self._beat_wake.set()

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(self.timings.housekeeping)
            self.store.purge_calls(self.timings.result_ttl)
            if self.terminal_holds.reap():
                self._lane_wake.set()
            now = time.monotonic()
            for call_id, call in list(self.resumable.items()):
                if now - call.created > self.timings.result_ttl:
                    self.resumable.pop(call_id, None)
            self._confirm_standing()
            self.check_standing()
