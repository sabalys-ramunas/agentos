"""Executors: whatever drives the lane for a task. Each is an object with a `name` and an async
`run(task, ctx) -> ExecResult`. The harness gives it a RunContext for one throwaway lane, already
on the task's start page, and scores what happened, not what the executor says happened (except
for answers and holds, which the checks compare with the site's own state).

- replay: hand-written structural steps per task through the node's flow driver (replay.py).
- mcp-agent: an agent over the node's MCP tools, as an external command (mcp_agent.py).
- jev: TypeSafe's Jev picks each step, the node's tools act (jev.py).
- pilot: agentos.pilot's own loop, the product version of jev (pilot.py).
- planted: deliberately bad runs that the gates must fail (planted.py; tests only).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lanebench.tasks import Task


@dataclass
class ExecResult:
    status: str = "done"              # done | held | gave_up | failed
    answer: dict[str, Any] = field(default_factory=dict)
    patch: dict[str, Any] | None = None
    reason: str | None = None
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)   # what the executor measured about itself

    def public(self) -> dict[str, Any]:
        return {"status": self.status, "answer": self.answer, "patch": self.patch, "reason": self.reason,
                "error": self.error, "metrics": self.metrics}


class ToolTrace:
    """Every call an executor made on the lane: the flow driver's steps for replay, MCP tool calls
    for an agent. Arguments are kept short, and typed text only as its length."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, tool: str, args: dict[str, Any], *, ok: bool, error: str | None, ms: float,
            code: str | None = None) -> None:
        safe = {}
        for k, v in args.items():
            if k in ("text", "value", "password"):
                safe[k] = f"<{len(str(v))} chars>"
            else:
                s = v if isinstance(v, int | float | bool) or v is None else str(v)
                safe[k] = s[:200] if isinstance(s, str) else s
        row = {"tool": tool, "args": safe, "ok": ok, "error": error, "ms": round(ms, 1)}
        if code:
            row["code"] = code  # an AgentosError's code: what an MCP agent reads first
        self.rows.append(row)

    async def call(self, tool: str, args: dict[str, Any], fn: Callable[[], Awaitable[Any]]) -> Any:
        t0 = time.monotonic()
        try:
            out = await fn()
        except Exception as e:
            self.add(tool, args, ok=False, error=f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}",
                     ms=(time.monotonic() - t0) * 1000, code=_code(e))
            raise
        self.add(tool, args, ok=True, error=None, ms=(time.monotonic() - t0) * 1000)
        return out


def _code(e: Exception) -> str | None:
    code = getattr(e, "code", None)
    return code if isinstance(code, str) else None


class RecordingDriver:
    """A flow driver whose every call lands in the tool trace."""

    TRACED = ("navigate", "find", "click", "type", "fill", "press", "select", "upload", "scroll_to", "read_state",
              "extract_items")

    def __init__(self, inner: Any, trace: ToolTrace):
        self.inner = inner
        self.trace = trace

    @property
    def page(self) -> Any:
        return self.inner.page

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if name not in self.TRACED:
            return attr

        async def traced(*args: Any, **kwargs: Any) -> Any:
            shown = {f"a{i}": _describe(a) for i, a in enumerate(args)} | {k: _describe(v) for k, v in kwargs.items()}
            if name in ("type", "fill") and len(args) > 1:
                shown["text"] = args[1]
                shown.pop("a1", None)
            if name == "navigate" and args:
                shown = {"url": args[0]}
            return await self.trace.call(f"flow.{name}", shown, lambda: attr(*args, **kwargs))

        return traced


def _describe(value: Any) -> Any:
    describe = getattr(value, "describe", None)
    if callable(describe):
        try:
            return describe()
        except Exception:  # noqa: BLE001 - a description is only for the trace
            return type(value).__name__
    return value


@dataclass
class RunContext:
    task: Task
    lane_id: str
    base_url: str
    runtime: Any
    trace: ToolTrace
    lane_sites: list[str]
    planted: dict[str, Any] = field(default_factory=dict)  # only for planted bad runs (the checker's URL)
    work_dir: Path | None = None      # the run's throwaway home, on disk: scratch files go here
    transcripts: Path | None = None   # the run's transcript folder, when the report has one

    @property
    def locale(self) -> str:
        return self.task.locale

    def url(self, path: str) -> str:
        return self.base_url + path

    @asynccontextmanager
    async def driver(self) -> AsyncIterator[RecordingDriver]:
        """The node's own flow driver on this lane (human input through human.py)."""
        factory = self.runtime.require("flow_driver")
        async with factory(self.lane_id) as inner:
            yield RecordingDriver(inner, self.trace)

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Any]:
        """The lane's raw Playwright page: planted bad runs, and the jev executor's human.py calls."""
        async with self.runtime.lanes.using(self.lane_id, principal_kind="flow") as page:
            yield page


class Executor(Protocol):
    name: str

    async def run(self, task: Task, ctx: RunContext) -> ExecResult: ...


def get(name: str, **options: Any) -> Executor:
    if name == "replay":
        from lanebench.executors.replay import ReplayExecutor

        return ReplayExecutor()
    if name == "mcp-agent":
        from lanebench.executors.mcp_agent import McpAgentExecutor

        return McpAgentExecutor(**options)
    if name == "jev":
        from lanebench.executors.jev import JevExecutor

        return JevExecutor()
    if name == "pilot":
        from lanebench.executors.pilot import PilotExecutor

        return PilotExecutor()
    if name.startswith("planted:"):
        from lanebench.executors.planted import planted

        return planted(name.split(":", 1)[1])
    raise ValueError(f"unknown executor {name!r}: replay, mcp-agent, jev, pilot")
