"""The env eval's tasks: one file each (tNN_<what>.py with `TASK = Task(...)`), loaded in name order.

A task is what a human would type (one prompt per session: a task that spans days has several), the
fixture repository it works on (tests/env_eval/repos/<name>), the node's settings for it
(environments.toml), and a checker that reads the real end state: the bare repository, a clean
checkout's tests, the GitHub stand-in's pull requests, the node's own records of environments,
proofs and machines, Docker, and the tool calls the proxy saw. Never what the agent says it did,
except where the task is to tell the human something (then the words are checked too).

Each task also has two scripted agents: `perfect` does what the agentos-environment skill teaches and
must finish; `careless` takes a plausible wrong turn and must not. Together they prove the checker,
and `perfect` is a free, repeatable benchmark of the canonical loop: its time and the tokens of tool
output it reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Check = tuple[str, bool]


class ToolFailed(Exception):
    def __init__(self, tool: str, text: str):
        super().__init__(f"{tool}: {text[:300]}")
        self.tool = tool
        self.text = text
        self.code = text.split(":", 1)[0].strip()


@dataclass
class Session:
    prompt: str                                                     # {repo} is the repository's slug
    before: Callable[[Ctx], Awaitable[None]] | None = None          # the harness's step before it (a build, the pool)


@dataclass
class Task:
    id: str
    title: str                                                      # what it measures, in one line
    sessions: list[Session]
    check: Callable[[Ctx], Awaitable[list[Check]]]
    perfect: Callable[[Ctx, Agent, int], Awaitable[str]]            # (ctx, agent, session) -> its final words
    careless: Callable[[Ctx, Agent, int], Awaitable[str]]
    repo: str | None = None                                         # a fixture under repos/
    settings: dict[str, Any] = field(default_factory=dict)          # environments.toml for its node
    requires: tuple[str, ...] = ()                                  # tools the node must have, else skipped
    new_environments: int = 1                                       # new machines it needs (more: a wrong turn)
    internet: bool = False                                          # it fetches from a public registry
    timeout_s: float = 480                                          # per session, for a model agent

    def prompt(self, ctx: Ctx, session: int) -> str:
        return self.sessions[session].prompt.format(repo=ctx.repo.slug if ctx.repo else "")


@dataclass
class Ctx:
    """One run of one task: the node, the stand-in, the repository, and what the agent did."""
    task: Task
    node: Any                                                       # node.EvalNode
    github: Any                                                     # ghstub.GitHubStub
    repo: Any = None                                                # node.Repo
    rows: list[dict[str, Any]] = field(default_factory=list)        # every tool call, all sessions
    session_rows: list[list[dict[str, Any]]] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)                # each session's final words
    notes: dict[str, Any] = field(default_factory=dict)             # what a task's steps keep for its checker

    def calls(self, tool: str, session: int | None = None) -> list[dict[str, Any]]:
        from env_eval.metrics import unwrap

        rows = self.rows if session is None else self.session_rows[session]
        return [r for r in (unwrap(x) for x in rows) if r["tool"] == tool]

    def results(self, tool: str, session: int | None = None) -> list[dict[str, Any]]:
        from env_eval.metrics import parsed

        return [parsed(r) for r in self.calls(tool, session) if r.get("ok")]

    def answer(self) -> str:
        return "\n".join(self.answers)

    # ---- the end state ----

    def environments(self) -> list[Any]:
        """This task's environments, as the node keeps them (oldest first)."""
        envs = [e for e in self.node.environments() if not str(e.title).startswith("warm")]
        return sorted(envs, key=lambda e: e.created_at)

    def branches(self) -> dict[str, str]:
        """{branch: commit} in the bare repository."""
        from env_eval.node import git

        out = git("--git-dir", str(self.repo.bare), "for-each-ref", "--format=%(refname:short) %(objectname)",
                  "refs/heads")
        return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)

    def new_branches(self) -> list[str]:
        return [b for b in self.branches() if b != "main"]

    def tests_pass(self, branch: str, cmd: tuple[str, ...] = ("node", "--test")) -> tuple[bool, str]:
        """Clone the bare repository fresh, check out `branch` and run its tests here."""
        from env_eval.node import git

        with tempfile.TemporaryDirectory(prefix="env-eval-check-") as tmp:
            work = Path(tmp) / "w"
            git("clone", "-q", "--branch", branch, str(self.repo.bare), str(work))
            done = subprocess.run(list(cmd), cwd=work, capture_output=True, text=True, timeout=120, check=False,
                                  env={"PATH": os.environ["PATH"], "HOME": tmp})
            return done.returncode == 0, (done.stdout + done.stderr)[-600:]

    async def closed_and_gone(self, wait_s: float = 90) -> list[Check]:
        """Every environment of the task is closed (a save in the background may take a while) and its
        container is gone."""
        deadline = time.monotonic() + wait_s
        while True:
            envs = self.environments()
            open_ = [e.id for e in envs if e.state not in ("closed", "failed")]
            left = await asyncio.to_thread(self.node.machines_left, {e.id for e in envs})
            settling = any(e.state == "closing" for e in envs) or (left and not open_)
            if not settling or time.monotonic() > deadline:
                break
            await asyncio.sleep(2)
        return [("every environment is closed", bool(envs) and not open_),
                ("no container of it is left", bool(envs) and not left)]


# ---- the scripted agents' side ----

class Agent:
    """A scripted agent's MCP session (through the recording proxy, like any agent)."""

    def __init__(self, client: Any):
        self.client = client
        self.on: str | None = None

    async def raw(self, tool: str, args: dict[str, Any] | None = None) -> tuple[bool, str]:
        result = await self.client.call_tool(tool, args or {})
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        return not result.is_error, text

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        """The result parsed (JSON, else the text); ToolFailed on an error."""
        ok, text = await self.raw(tool, args)
        if not ok:
            raise ToolFailed(tool, text)
        with contextlib.suppress(ValueError):
            return json.loads(text)
        return text

    async def on_call(self, tool: str, **args: Any) -> Any:
        return await self.call(tool, {"on": self.on, **args})

    async def via(self, tool: str, **args: Any) -> Any:
        """A tool outside the default list, the way the instructions say: tools_call(name, arguments)."""
        return await self.call("tools_call", {"name": tool, "arguments": {"on": self.on, **args}})

    async def open(self, **args: Any) -> dict[str, Any]:
        """environment_open, then environment(on) until it is ready (the skill's step 1)."""
        out = await self.call("environment_open", {"wait_s": 200, **args})
        self.on = out["on"]
        for _ in range(6):
            if out.get("state") == "ready":
                break
            if out.get("state") in ("failed", "closed"):
                raise ToolFailed("environment_open", json.dumps(out)[:400])
            out = {**out, **await self.on_call("environment")}
        return out

    async def sh(self, cmd: str, *, cwd: str | None = None, timeout_s: float = 120) -> dict[str, Any]:
        args: dict[str, Any] = {"cmd": cmd, "timeout_s": timeout_s}
        if cwd:
            args["cwd"] = cwd
        return await self.on_call("terminal_exec", **args)


def done(summary: str, status: str = "done") -> str:
    return json.dumps({"status": status, "summary": summary})


# ---- the harness's waits between sessions ----

async def until(what: str, test: Callable[[], bool], timeout_s: float, every_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await asyncio.to_thread(test):
            return
        await asyncio.sleep(every_s)
    raise TimeoutError(f"{what} did not happen in {timeout_s:.0f} s")


# ---- loading ----

def all_tasks() -> list[Task]:
    here = Path(__file__).parent
    out = []
    for f in sorted(here.glob("t[0-9][0-9]_*.py")):
        mod = importlib.import_module(f"env_eval.tasks.{f.stem}")
        out.append(mod.TASK)
    return out


def has_node() -> bool:
    return shutil.which("node") is not None
