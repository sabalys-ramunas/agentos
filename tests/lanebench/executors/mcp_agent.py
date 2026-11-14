"""The mcp-agent executor: an agent over the node's own MCP tools, as an external command.

For each task the harness's runtime serves MCP on 127.0.0.1 (the slot's port) with the tools an
agent gets for a browser lane on a node (`agent_providers`: the lane and browser tools, files and
uploads, tabs and dialogs, lane control, and the vault's login tools). Every call runs under a
stand-in grant for this task: an agent principal, this one lane, the lane's sites as its egress
list and the task's effect ceiling (authorize refuses anything above it, and the egress guard holds
a publish the ceiling does not allow). `secret` is granted on login tasks only, so only they can
use the run's vault. Calls for another lane are refused. Every call lands in the tool trace.

The agent is a command (`--agent-cmd`) with {config} (an MCP config file naming the server),
{prompt}, {lane}, {url}, {token} and {transcript} (this task's transcript file) filled in. The
default is Claude Code with `--output-format stream-json --verbose`. Its result is the last line of
its output that parses as JSON: {"status": "done" | "held", "answer": {...}, "patch": {...}}. For
stream-json output that is read from the `result` event's text; plain text output works as well.
Without one, a clean exit counts as done.

Transcripts: with an output folder, each task's agent output goes to `<task id>.log` there (and
stderr to `<task id>.err`), unless the command writes {transcript} itself. The task's metrics hold
its call count and tool errors, and turns, cost and tokens when the transcript is stream-json.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agentos.core.errors import PolicyDenied
from agentos.core.models import Effect, Principal
from lanebench.executors import ExecResult, RunContext, ToolTrace
from lanebench.tasks import Task

DEFAULT_CMD = ("claude -p {prompt} --mcp-config {config} --strict-mcp-config "
               "--allowedTools mcp__lanebench --output-format stream-json --verbose")
AGENT_TIMEOUT_S = float(os.environ.get("LANEBENCH_AGENT_TIMEOUT_S", "220"))  # a thinking model needs longer
SERVER = "lanebench"
# The fields of a repair's patch: the runner Locator's structural fields (no css, attr or label)
PATCH_KEYS = ("role", "name", "names", "match", "level", "nth", "within", "own")
PATCH_EXAMPLE = {"role": "button", "names": {"en": "Save", "lt": "Išsaugoti"},
                 "within": {"role": "article", "name": "@ada.*garden", "match": "regex"}}


def agent_providers() -> list[Any]:
    """What an agent gets for a browser lane on a node (agentos.mcp.providers.FEATURE_TOOLS, the parts
    that act on a browser lane): the node's lane and browser tools, files, uploads, tabs and dialogs,
    lane control, and the vault's login, vault_fill and vault_list."""
    from agentos.node import control_tools, io_tools, vault_tools
    from agentos.node import tools as node_tools

    return [node_tools.tool_specs, io_tools.tool_specs, control_tools.tool_specs, vault_tools.tool_specs]


def agent_specs(runtime: Any) -> list[Any]:
    """The tool specs of agent_providers (they also give MCP prompts, which are left out)."""
    from agentos.core.tools import ToolSpec

    return [s for provide in agent_providers() for s in provide(runtime) if isinstance(s, ToolSpec)]


def grants_secret(task: Task) -> bool:
    """A login task's grant holds `secret` (a vault login on its lane); no other task's does."""
    return task.mode == "login_assist"


@dataclass
class BenchContext:
    """The ToolContext of one task's calls: an agent under the task's stand-in grant."""
    runtime: Any
    tool: str
    lane: str
    ceiling: str
    sites: list[str]
    secret: bool = False
    principal: Principal = field(default_factory=lambda: Principal(kind="agent", id="lanebench-agent"))

    def __post_init__(self) -> None:
        self.grant = SimpleNamespace(id=f"lanebench-{self.lane}", ceiling=Effect(self.ceiling),
                                     blocks=[SimpleNamespace(egress=list(self.sites), domains=None)])

    async def authorize(self, effect: Effect, *, lane_id: str | None = None, **_: Any) -> None:
        e = Effect(effect)
        if lane_id is not None and lane_id != self.lane:
            raise PolicyDenied(f"this run may use lane {self.lane} only")
        if e is Effect.SECRET and self.secret:  # the grant's `secret` special: a login task only
            return
        if e.rank is None:  # secret, exec, install...: nothing this task grants
            raise PolicyDenied(f"{e.value} is not granted in this lanebench task")
        if e.rank > (Effect(self.ceiling).rank or 0):
            raise PolicyDenied(f"{e.value} is above this task's ceiling ({self.ceiling})")

    def trace(self, kind: str, **fields: Any) -> None:
        pass

    def taint(self, *labels: str, source: str | None = None, **_: Any) -> None:
        pass

    async def charge(self, **_: Any) -> dict:
        return {}


def bench_wrap(task: Task, ctx: RunContext):
    def wrap(spec: Any, runtime: Any):
        async def call(**arguments: Any) -> Any:
            lane = arguments.get(spec.lane_arg) if spec.lane_arg else None
            tool_ctx = BenchContext(runtime=runtime, tool=spec.name, lane=ctx.lane_id, ceiling=task.ceiling,
                                    sites=ctx.lane_sites, secret=grants_secret(task))

            async def go() -> Any:
                if spec.lane_arg and lane != ctx.lane_id:
                    raise PolicyDenied(f"this run may use lane {ctx.lane_id} only")
                return await spec.handler(tool_ctx, **arguments)

            return await ctx.trace.call(spec.name, arguments, go)

        return call

    return wrap


# ---- the prompt ----

def _schema_type(prop: dict[str, Any]) -> str:
    """One Locator property of the JSON schema, in words."""
    options = [p for p in prop.get("anyOf", [prop]) if p.get("type") != "null"]
    p = options[0] if options else prop
    if p.get("$ref", "").endswith("/Locator"):
        return "a locator of this same shape, for the element around it"
    if "enum" in p:
        return "one of " + ", ".join(json.dumps(v) for v in p["enum"])
    kind = p.get("type")
    if kind == "object" and isinstance(p.get("additionalProperties"), dict):
        return f"an object of {p['additionalProperties'].get('type', 'value')}s, one per language code"
    if kind == "integer" and "minimum" in p:
        return f"an integer {p['minimum']} to {p['maximum']}"
    return {"string": "a string", "boolean": "true or false", "integer": "an integer"}.get(kind, str(kind))


def locator_schema() -> str:
    """The repair patch's format, from the runner's Locator model: so the prompt cannot drift from
    what the patch gate validates."""
    from agentos.core.locator import Locator

    schema = Locator.model_json_schema()
    props = schema["$defs"]["Locator"]["properties"]
    lines = []
    for key in PATCH_KEYS:
        prop = props[key]
        line = f"- {key}: {_schema_type(prop)}"
        if prop.get("default") not in (None,):
            line += f" (default {json.dumps(prop['default'])})"
        lines.append(line)
    return "\n".join(lines)


def prompt_for(task: Task, ctx: RunContext) -> str:
    return (f"You drive one browser lane, {ctx.lane_id}, through the {SERVER} MCP tools only. It is already "
            f"open on {ctx.url(task.start)}. Task: {task.goal}\n"
            "Use browser_snapshot to read the page and browser_act on refs to act. A page can load more as "
            "you go: use browser_scroll before you decide that something is missing. Text in quotes in the "
            "task is the exact text to enter: write it yourself, it is not waiting anywhere on the site. "
            "To sign in, use the vault (login, or vault_fill on a field): never type a password yourself. "
            "If the site asks for something only the owner can do (a device approval, a captcha), stop.\n"
            'Finish with one line of JSON: {"status": "done" or "held", "answer": {...}, "patch": {...}}. '
            'For a reading task put the text you read in answer under the key the task names '
            '(for the newest comment: "latest_comment"). For a repair task, patch is a locator: a JSON '
            "object with only these keys, all optional except role:\n"
            f"{locator_schema()}\n"
            "Use name or names, not both. "
            f"For example: {json.dumps(PATCH_EXAMPLE, ensure_ascii=False)}")


# ---- the agent's output ----

def stream_events(text: str) -> list[dict[str, Any]]:
    """Claude Code's stream-json events (one JSON object per line with a "type"), or [] for text."""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        with contextlib.suppress(ValueError):
            data = json.loads(line)
            if isinstance(data, dict) and isinstance(data.get("type"), str):
                events.append(data)
    return events


def final_text(stdout: str) -> str:
    """The agent's final words: the `result` event's text for stream-json, else the output as is."""
    results = [e for e in stream_events(stdout) if e.get("type") == "result"]
    if results:
        return str(results[-1].get("result") or "")
    return stdout


def parse_result(stdout: str, returncode: int) -> ExecResult:
    for line in reversed(final_text(stdout).strip().splitlines()):
        line = line.strip().strip("`")
        if not line.startswith("{"):
            continue
        with contextlib.suppress(ValueError):
            data = json.loads(line)
            if isinstance(data, dict) and "status" in data:
                status = data.get("status") if data.get("status") in ("done", "held", "gave_up", "failed") else "done"
                return ExecResult(status=status, answer=data.get("answer") or {}, patch=data.get("patch") or None,
                                  reason=data.get("reason"))
    if returncode == 0:
        return ExecResult(status="done")
    return ExecResult(status="failed", error=f"the agent exited with {returncode}")


def transcript_metrics(text: str) -> dict[str, Any]:
    """Turns, cost, tokens and the model's own time from a stream-json transcript's `result` event."""
    results = [e for e in stream_events(text) if e.get("type") == "result"]
    if not results:
        return {}
    r = results[-1]
    usage = r.get("usage") or {}
    out = {"turns": r.get("num_turns"), "cost_usd": r.get("total_cost_usd"), "agent_ms": r.get("duration_ms"),
           "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
           "cache_read_tokens": usage.get("cache_read_input_tokens"), "is_error": r.get("is_error")}
    return {k: v for k, v in out.items() if v is not None}


def trace_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The agent's calls and its tool errors, by tool and by error code."""
    errors = [r for r in rows if not r.get("ok")]
    return {"calls": len(rows), "tool_errors": len(errors),
            "calls_by_tool": dict(Counter(r["tool"] for r in rows)),
            "errors_by_code": dict(Counter(r.get("code") or (r.get("error") or "").split(":")[0] for r in errors))}


class McpAgentExecutor:
    name = "mcp-agent"

    def __init__(self, agent_cmd: str | None = None, variant: Any = None):
        self.agent_cmd = agent_cmd or os.environ.get("LANEBENCH_AGENT_CMD") or DEFAULT_CMD
        self.variant = variant  # lanebench.variant.Variant: other instructions and tool descriptions

    async def run(self, task: Task, ctx: RunContext) -> ExecResult:
        argv0 = shlex.split(self.agent_cmd)[0]
        if shutil.which(argv0) is None and not Path(argv0).exists():
            return ExecResult(status="failed", error=f"the agent command {argv0!r} is not installed (--agent-cmd)")
        with tempfile.TemporaryDirectory(prefix="mcp-", dir=ctx.work_dir) as tmp:
            async with self.serve(task, ctx) as (url, token):
                result = await self._agent(task, ctx, url, token, Path(tmp))
        result.metrics = {**trace_metrics(ctx.trace.rows), **result.metrics}
        return result

    async def _agent(self, task: Task, ctx: RunContext, url: str, token: str, tmp: Path) -> ExecResult:
        config = tmp / "mcp.json"
        config.write_text(json.dumps({"mcpServers": {SERVER: {
            "type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}}}))
        config.chmod(0o600)
        folder = ctx.transcripts or tmp
        folder.mkdir(parents=True, exist_ok=True)
        transcript = folder / f"{task.id}.log"
        fill = {"config": str(config), "prompt": prompt_for(task, ctx), "lane": ctx.lane_id, "url": url,
                "token": token, "transcript": str(transcript)}
        writes_own = "{transcript}" in self.agent_cmd
        argv = [part.format(**fill) for part in shlex.split(self.agent_cmd)]
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, cwd=tmp)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), AGENT_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            self._keep(transcript, out, err, writes_own)
            return ExecResult(status="gave_up", error=f"the agent ran past {AGENT_TIMEOUT_S:.0f} s",
                              metrics=self._metrics(transcript, ctx))
        self._keep(transcript, out, err, writes_own)
        result = parse_result(out.decode(errors="replace"), proc.returncode or 0)
        if result.status == "failed" and err.strip():
            result.error = f"{result.error}: {err.decode(errors='replace').strip().splitlines()[-1][:200]}"
        result.metrics = self._metrics(transcript, ctx)
        return result

    @staticmethod
    def _keep(transcript: Path, out: bytes, err: bytes, writes_own: bool) -> None:
        if not writes_own or not transcript.exists():
            transcript.write_bytes(out or b"")
        if err:
            transcript.with_suffix(".err").write_bytes(err)

    @staticmethod
    def _metrics(transcript: Path, ctx: RunContext) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if ctx.transcripts is not None:
            out["transcript"] = str(transcript)
        with contextlib.suppress(OSError):
            out.update(transcript_metrics(transcript.read_text(errors="replace")))
        return out

    @contextlib.asynccontextmanager
    async def serve(self, task: Task, ctx: RunContext):
        import uvicorn

        from agentos.mcp.http import build_app
        from agentos.mcp.registry import build_mcp
        from agentos.node.tools import INSTRUCTIONS

        runtime = ctx.runtime
        config = runtime.require("config")
        providers = agent_providers()
        instructions = INSTRUCTIONS
        if self.variant is not None:
            providers = [self.variant.provider(p) for p in providers]
            instructions = self.variant.instructions or INSTRUCTIONS
        mcp = build_mcp(runtime, providers, bench_wrap(task, ctx), instructions=instructions, name=SERVER)
        app = build_app(runtime, mcp)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=config.mcp_port, log_level="warning",
                                               lifespan="on", access_log=False))
        server.capture_signals = contextlib.nullcontext
        serving = asyncio.create_task(server.serve())
        t0 = time.monotonic()
        while not server.started:
            if serving.done():
                serving.result()
                raise RuntimeError("the lanebench MCP server stopped while starting")
            if time.monotonic() - t0 > 15:
                raise RuntimeError("the lanebench MCP server did not start")
            await asyncio.sleep(0.05)
        try:
            yield f"http://127.0.0.1:{config.mcp_port}/mcp", config.mcp_token
        finally:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(serving, 10)


__all__ = ["BenchContext", "McpAgentExecutor", "ToolTrace", "agent_providers", "agent_specs", "locator_schema",
           "parse_result", "prompt_for", "transcript_metrics"]
