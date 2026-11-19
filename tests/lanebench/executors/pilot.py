"""The pilot executor: agentos.pilot's loop on the bench, through the node's own tools.

A thin shell over the package: it builds a Request from the task and maps the pilot's honest status
onto the bench's four. The loop is the product's own, so a score here is a score of what ships.

What stands in for the product here:

- Tools: the node's tool handlers under the task's stand-in grant (mcp_agent.bench_wrap), as the jev
  executor gets them, and browser_scroll instead of its direct wheel_down.
- The decider: jev.JevClient on TYPESAFE_API_KEY. The harness has no brains service, and a decider
  is one small interface, so this one posts to the same endpoint BrainService.decide does.
- Values: what the planner would hand over, from the task's replay steps (jev.planner_values), so
  this scores the pilot's picks and not any writing.

`LANEBENCH_RESCUE_CMD` turns rescue on: a command that takes BrainService.pick's prompt on stdin and
prints its reply, for example `claude -p --model haiku --tools ""`. Without it, an unsure pick ends needs_help, which counts as
gave_up here. Repair tasks need a locator written, so they end gave_up without acting, as with jev.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from collections.abc import Sequence
from typing import Any

from agentos.brains.decisions import DecisionRejected, pick_prompt, read_pick
from agentos.core.errors import AgentosError
from agentos.pilot import Limits, Pilot, Request, Result, Status, Value
from agentos.pilot.deciders import BLOCKED, UNAVAILABLE, DeciderFailed, Pick, Reply, holds_secret
from agentos.pilot.hands import ToolFailed
from lanebench.executors import ExecResult, RunContext
from lanebench.executors.jev import API_URL, MODEL, Hands, JevClient, JevRefused, Metrics, planner_values
from lanebench.executors.mcp_agent import agent_specs, bench_wrap
from lanebench.tasks import Task

WANTED = ("browser_snapshot", "browser_act", "browser_scroll", "browser_wait", "browser_upload", "browser_files",
          "vault_fill")
RESCUE_TIMEOUT_S = 120.0
STATUSES = {Status.DONE_VERIFIED: "done", Status.HELD: "held", Status.WAITING_APPROVAL: "held",
            Status.REFUSED: "gave_up", Status.GAVE_UP: "gave_up", Status.NEEDS_HELP: "gave_up",
            Status.DONE_UNVERIFIED: "gave_up"}  # not a claim of done: the checks still read the site's own state


class BenchTools:
    """The node's tools under the task's stand-in grant, with the jev executor's look."""

    def __init__(self, task: Task, ctx: RunContext):
        wrap = bench_wrap(task, ctx)
        self.calls = {s.name: wrap(s, ctx.runtime) for s in agent_specs(ctx.runtime) if s.name in WANTED}
        self.hands = Hands(ctx)

    async def call(self, name: str, **arguments: Any) -> Any:
        try:
            return await self.calls[name](**arguments)
        except AgentosError as e:
            raise ToolFailed(e.code, e.message) from None

    async def look(self) -> None:
        await self.hands.look()


class BenchDecider:
    """JevClient as a pilot decider. Its metrics count calls and tokens the way a jev run does."""

    def __init__(self, client: JevClient, metrics: Metrics):
        self.client, self.metrics = client, metrics
        self.name = client.model

    async def ask(self, state: Any, questions: dict[str, Any], *, must_not_send: Sequence[str]) -> Reply:
        before = self.metrics.input_tokens
        t0 = time.monotonic()
        try:
            answers = await self.client.ask(state, questions, secrets=list(must_not_send), metrics=self.metrics)
        except JevRefused as e:
            raise DeciderFailed(BLOCKED if "secret" in str(e) else UNAVAILABLE, str(e)) from None
        return Reply(answers, input_tokens=self.metrics.input_tokens - before, model=self.metrics.model,
                     latency_ms=round((time.monotonic() - t0) * 1000))


class CommandRescue:
    """A rescue as a command: BrainService.pick's own prompt on stdin, its reply on stdout, read by
    the same strict reader. The harness has no brains service, so this is how a bench run gets one."""

    def __init__(self, command: str):
        self.argv = shlex.split(command)
        self.name = self.argv[0]

    async def pick(self, question: str, options: dict[str, str], state: Any, *, must_not_send: Sequence[str],
                   max_usd: float | None = None) -> Pick:
        prompt = pick_prompt(question, options, state)
        if holds_secret(prompt, must_not_send):
            raise DeciderFailed(BLOCKED, "the prompt held a secret, so it was not sent")
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(*self.argv, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), RESCUE_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            raise DeciderFailed(UNAVAILABLE, f"{self.name} took too long") from None
        if proc.returncode != 0:
            raise DeciderFailed(UNAVAILABLE, f"{self.name} exited with {proc.returncode}")
        try:
            choice, confidence = read_pick(out.decode(errors="replace"), options)
        except DecisionRejected as e:
            raise DeciderFailed(UNAVAILABLE, e.message) from None
        return Pick(choice, confidence, latency_ms=round((time.monotonic() - t0) * 1000), model=self.name)


def request_for(task: Task, ctx: RunContext, files: Sequence[str]) -> Request:
    """A password is handed over as the run's vault entry, as a node run hands it (the node fills it
    by name); its text stays only so must_not_send can check nothing carries it."""
    values = tuple(Value(key=v.key, label=v.label, text=v.text, secret=v.secret, vault=v.vault)
                   for v in planner_values(task))
    answer = next((str(c["key"]) for c in task.check if c.get("kind") == "answer"), None)
    return Request(lane=ctx.lane_id, goal=task.goal, sites=tuple(ctx.lane_sites), values=values, files=tuple(files),
                   read=answer, seed=task.id, limits=Limits())


def exec_result(result: Result, metrics: Metrics) -> ExecResult:
    metrics.stopped_by = result.reason
    public = {**metrics.public(), "steps": result.rows, "pilot_status": result.status.value, "needs": result.needs,
              "escalations": [e.public() for e in result.escalations],
              "spend": {job: s.public() for job, s in result.spend.items()}}
    error = result.message if result.status is Status.REFUSED else None
    return ExecResult(status=STATUSES[result.status], answer=result.answer, reason=result.reason, error=error,
                      metrics=public)


class PilotExecutor:
    name = "pilot"

    def __init__(self, client: JevClient | None = None) -> None:
        key = os.environ.pop("TYPESAFE_API_KEY", None)
        self.client = client or JevClient(key or "", url=os.environ.get("JEV_API_URL", API_URL),
                                          model=os.environ.get("JEV_MODEL", MODEL))
        command = os.environ.get("LANEBENCH_RESCUE_CMD")
        self.rescue = CommandRescue(command) if command else None

    def tools(self, task: Task, ctx: RunContext) -> Any:
        return BenchTools(task, ctx)

    async def run(self, task: Task, ctx: RunContext) -> ExecResult:
        metrics = Metrics()
        if task.mode == "repair":
            metrics.stopped_by = "repair_needs_writing"
            return ExecResult(status="gave_up", reason="a repair writes a locator; the pilot only picks",
                              metrics=metrics.public())
        tools = self.tools(task, ctx)
        listed = await tools.call("browser_files", on=ctx.lane_id)
        files = [f["name"] for f in listed.get("inbox", []) if f.get("name") and f["name"] in task.goal]
        pilot = Pilot(tools, BenchDecider(self.client, metrics), rescue=self.rescue)
        return exec_result(await pilot.run(request_for(task, ctx, files)), metrics)


__all__ = ["BenchDecider", "BenchTools", "CommandRescue", "PilotExecutor", "exec_result", "request_for"]
