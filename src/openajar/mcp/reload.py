"""Live reload of the MCP tool list: tools appear and go while agentosd runs.

Some tools are built from what is stored on the node: one `flow_<id>` tool per saved flow and
one `<site>_<op>` tool per installed connector operation (openajar.runner.tools). Before this
module those were built once, when the MCP server started, so a connector installed with the
CLI (another process) served no tool until agentosd restarted, and a restart closes every lane,
including lanes other sessions hold (the 2026-11-04 canary).

    changes = refresh(server, runtime)          # the diff, applied (no I/O, no await)
    changes = await reload_tools(runtime)       # the same, then clients and the controller told

`refresh` calls every provider again (the ones `build_mcp` used, kept on the server), compares
each tool with what is served by a fingerprint of what a client sees (name, description,
arguments, effect, annotations) and adds the new ones, takes away the gone ones and replaces the
changed ones. Nothing else moves: a call already running finishes on the tool it started with,
and no lane is touched. Then `notifications/tools/list_changed` goes to connected MCP clients
(ToolServer.tools_changed) and the controller channel, when there is one, gets the new list.

`ToolWatch` runs in agentosd. Every POLL_S it reads each feature package's
`tools_signature(runtime)` (openajar.runner.tools: the flow heads, one cheap SQL query) and
reloads when one changed; `poke()` (the ops socket's `reload_tools`, used by `openajar connector
install` and `remove`) reloads at once.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from openajar.core.runtime import Runtime
from openajar.core.tools import ToolSpec
from openajar.mcp.registry import (
    ToolServer,
    collect,
    register,
    tool_annotations,
    tool_description,
    tool_params,
    unregister,
)

log = logging.getLogger(__name__)

POLL_S = 3.0
SERVICE_NAME = "tool_reload"
SIGNATURE_MODULES: tuple[str, ...] = ("openajar.runner.tools",)  # packages whose tools follow stored state


@dataclass
class ToolChanges:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # tool name -> why it could not be served

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"added": self.added, "removed": self.removed, "changed": self.changed}
        if self.failed:
            out["failed"] = self.failed
        return out

    def summary(self) -> str:
        parts = [f"{label} {', '.join(names)}" for label, names in
                 (("added", self.added), ("removed", self.removed), ("changed", self.changed)) if names]
        return "; ".join(parts) or "no change"


def fingerprint(spec: ToolSpec) -> str:
    """What a client sees of a tool, as one string: two specs with the same fingerprint serve the
    same tool, so a reload leaves it alone."""
    params = [(p.name, repr(p.annotation), repr(p.default), p.description) for p in tool_params(spec)]
    return json.dumps({
        "description": tool_description(spec), "params": params, "effect": str(spec.effect),
        "lane_arg": spec.lane_arg, "tags": list(spec.tags),
        "annotations": tool_annotations(spec).model_dump(mode="json", exclude_none=True),
    }, sort_keys=True, default=str)


def refresh(server: ToolServer, runtime: Runtime) -> ToolChanges:
    """Bring the served tools in line with the providers. Synchronous on purpose: with no await
    between the first change and the last, no call or tools/list sees half a reload."""
    changes = ToolChanges()
    if not server.providers:
        return changes
    try:
        wanted, _prompts = collect(runtime, server.providers)
    except Exception:  # a provider's duplicate name: keep serving what was there
        log.exception("could not list the tools again; the served tools stay as they were")
        return changes
    new = {spec.name: spec for spec in wanted}
    for name in sorted(set(server.specs) - set(new)):
        unregister(server, name)
        changes.removed.append(name)
    for name, spec in sorted(new.items()):
        old = server.specs.get(name)
        try:
            if old is not None and fingerprint(old) == fingerprint(spec):
                continue
            if old is not None:
                unregister(server, name)
            register(server, runtime, [spec], server.wrap)
        except Exception as exc:  # noqa: BLE001  (one bad tool must not stop the others)
            changes.failed[name] = f"{type(exc).__name__}: {exc}"[:300]
            log.error("tool %s cannot be served: %s", name, changes.failed[name])
            if old is not None:
                changes.removed.append(name)
            continue
        (changes.changed if old is not None else changes.added).append(name)
    if changes:
        log.info("MCP tools reloaded: %s", changes.summary())
    return changes


async def reload_tools(runtime: Runtime, server: ToolServer | None = None) -> ToolChanges:
    """refresh(), then tell MCP clients (tools/list_changed) and the controller channel."""
    server = server if server is not None else runtime.get("mcp")
    if not isinstance(server, ToolServer):
        return ToolChanges()
    changes = refresh(server, runtime)
    if not changes:
        return changes
    try:
        told = await server.tools_changed()
        log.info("told %d MCP session(s) that the tool list changed", told)
    except Exception:
        log.exception("could not tell MCP clients that the tool list changed")
    channel = runtime.get("channel")
    announce = getattr(channel, "announce_tools", None)
    if callable(announce):
        try:
            result = announce()
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("could not send the new tool list to the controller")
    return changes


def signature_sources(modules: Sequence[str] = SIGNATURE_MODULES) -> list[Callable[[Runtime], Any]]:
    out = []
    for name in modules:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        fn = getattr(module, "tools_signature", None)
        if callable(fn):
            out.append(fn)
    return out


class ToolWatch:
    """Reloads the tool list when what it is built from changes (see the module docstring)."""

    def __init__(self, runtime: Runtime, server: ToolServer | None = None, *, poll_s: float = POLL_S,
                 sources: Sequence[Callable[[Runtime], Any]] | None = None):
        self.runtime = runtime
        self.server = server
        self.poll_s = poll_s
        self.sources = list(signature_sources() if sources is None else sources)
        self._last: Any = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.reloads = 0

    def signature(self) -> Any:
        out = []
        for source in self.sources:
            try:
                out.append(source(self.runtime))
            except Exception:  # a store that cannot be read now: try again next time
                log.debug("tool signature failed", exc_info=True)
                out.append(self._last)
        return tuple(out)

    async def reload(self) -> ToolChanges:
        """Reload now (the signature is read again first, so the poll does not repeat it)."""
        async with self._lock:
            self._last = self.signature()
            changes = await reload_tools(self.runtime, self.server)
            if changes:
                self.reloads += 1
            return changes

    def poke(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        self._last = self.signature()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="openajar-tool-watch")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_s)
            except TimeoutError:
                pass
            poked = self._wake.is_set()
            self._wake.clear()
            try:
                if poked or self.signature() != self._last:
                    await self.reload()
            except Exception:
                log.exception("tool reload failed; the served tools stay as they were")
