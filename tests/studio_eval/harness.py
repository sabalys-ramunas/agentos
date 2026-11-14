"""The studio agent experience eval (docs/design/studio-ax.md, rule 15): the parts a run is made of.

- `Studio`: `agentos studio up` in a scratch HOME (never the real ~/.agentos), on a free port,
  with the agent grant's token read from its banner. One per agent kind, so runs never share
  state.
- `Site`: the tasks' pages, copied fresh into a folder before each task and served on
  127.0.0.1 by a plain http server (a dev server stand-in).
- `Mcp`: an MCP client to the studio server. `call` is the runner's own (setup and checking,
  not recorded); `Trace.call` is the scripted agent's (recorded).
- `Trace`: one row per tool call an agent made: tool, arguments, ok, error code, characters
  of text read, images.
- `wrong_turns`: what a careful agent would not have done, read from the trace.
- `claude_agent`: Claude Code headless (`claude -p`) with only the studio MCP server, file
  Read/Edit/Write/Glob in the task's folder, and the agentos-studio skill appended to its
  system prompt. Its stream-json transcript becomes the same Trace rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "src/agentos/client/skills/agentos-studio/SKILL.md"
AGENTOS = str(Path(sys.executable).parent / "agentos")   # the console script of this environment
LANE_TOOLS = {"lanes_list", "lane_open", "lane_close", "lane_view", "lane_claim", "lane_release", "browser_navigate",
              "browser_snapshot", "browser_act", "browser_screenshot", "browser_wait", "browser_tabs"}
# A page that did not load or render is news about the page, not a wrong call.
PAGE_ERRORS = ("studio_page_not_loaded", "studio_http_error", "studio_blank_page", "studio_dev_error")
FOOTER = ("\n\nThe page files are in your working folder, {folder}; a dev server serves that folder at {base}, so "
          "a saved file is live at once. Use the studio MCP tools to check pages. When you are done, end with one "
          'line of JSON: {{"status": "done" or "blocked", "summary": "<one or two sentences: what you found and '
          'did>"}}.')


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- the studio server ----

class Studio:
    """`agentos studio up` on a scratch HOME. Use as a context manager."""

    def __init__(self, home: Path):
        self.home = home
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.token = ""
        self.log = home / "studio-up.log"

    @property
    def url(self) -> str:
        """The runner's own connection: studio and profiles listed."""
        return f"http://127.0.0.1:{self.port}/mcp?toolsets=studio,profiles"

    @property
    def agent_url(self) -> str:
        """An agent's connection, as the line `agentos studio up` prints: the studio toolset listed,
        the rest through tools_more and tools_call."""
        return f"http://127.0.0.1:{self.port}/mcp?toolsets=studio"

    @property
    def node_home(self) -> Path:
        return self.home / ".agentos" / "studio" / "node"

    def __enter__(self) -> Self:
        self.home.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "HOME": str(self.home)}
        out = self.log.open("w")
        self.proc = subprocess.Popen([AGENTOS, "studio", "up", "--port", str(self.port)],
                                     env=env, stdout=out, stderr=subprocess.STDOUT, cwd=REPO)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            m = re.search(r'Bearer ([^"\s]+)', self.log.read_text(errors="replace"))
            if m:
                self.token = m.group(1)
                return self
            if self.proc.poll() is not None:
                break
            time.sleep(0.3)
        self.__exit__(None, None, None)
        raise RuntimeError(f"agentos studio up did not start; see {self.log}")

    def __exit__(self, *exc: object) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def owner(self, *args: str) -> str:
        """An owner command on this studio's node (agentos profile approve ...)."""
        env = {**os.environ, "HOME": str(self.home), "AGENTOS_HOME": str(self.node_home)}
        done = subprocess.run([AGENTOS, *args], env=env, capture_output=True, text=True,
                              timeout=60, cwd=REPO, check=False)
        if done.returncode != 0:
            raise RuntimeError(f"agentos {' '.join(args)} failed: {done.stderr.strip()[-300:]}")
        return done.stdout


# ---- the site ----

class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class Site:
    """A folder served on 127.0.0.1. Each task gets <root>/<task id>/ with its pages."""

    def __init__(self, root: Path):
        self.root = root
        self.port = free_port()
        handler = functools.partial(_Quiet, directory=str(root))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self.root.mkdir(parents=True, exist_ok=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    def base(self, task_id: str) -> str:
        return f"http://127.0.0.1:{self.port}/{task_id}/"

    def lay_out(self, task_id: str, pages: dict[str, str]) -> Path:
        folder = self.root / task_id
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        for name, text in pages.items():
            (folder / name).write_text(text)
        return folder


# ---- MCP ----

class ToolFailed(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self.text = text
        self.code = text.split(":", 1)[0].strip()


def _texts(result: Any) -> tuple[str, int]:
    texts, images = [], 0
    for c in result.content:
        if c.type == "text":
            texts.append(c.text)
        else:
            images += 1
    return "\n".join(texts), images


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


class Mcp:
    def __init__(self, studio: Studio):
        self.studio = studio
        self._stack: contextlib.AsyncExitStack | None = None
        self.client: Any = None

    async def __aenter__(self) -> Self:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        self._stack = contextlib.AsyncExitStack()
        http = await self._stack.enter_async_context(
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {self.studio.token}"}, timeout=400))
        self.client = await self._stack.enter_async_context(
            Client(streamable_http_client(self.studio.url, http_client=http), mode="legacy"))
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    async def raw(self, tool: str, args: dict[str, Any]) -> tuple[bool, str, int]:
        result = await self.client.call_tool(tool, args)
        text, images = _texts(result)
        return not result.is_error, text, images

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        """The runner's own call (not an agent's): the result, parsed; ToolFailed on an error."""
        ok, text, _ = await self.raw(tool, args or {})
        if not ok:
            raise ToolFailed(text)
        return _parse(text)


@dataclass
class Row:
    tool: str
    args: dict[str, Any]
    ok: bool
    code: str | None
    chars: int
    images: int
    text: str = ""

    def brief(self) -> dict[str, Any]:
        return {"tool": self.tool, "args": self.args, "ok": self.ok, "code": self.code, "chars": self.chars,
                "images": self.images}


@dataclass
class Trace:
    mcp: Mcp | None = None
    rows: list[Row] = field(default_factory=list)

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        """A scripted agent's call: recorded, then returned parsed (ToolFailed on an error)."""
        args = args or {}
        ok, text, images = await self.mcp.raw(tool, args)
        self.rows.append(Row(tool, args, ok, None if ok else text.split(":", 1)[0].strip(), len(text), images, text))
        if not ok:
            raise ToolFailed(text)
        return _parse(text)

    def calls(self, tool: str) -> list[Row]:
        return [r for r in self.rows if r.tool == tool]


def tokens_read(rows: list[Row]) -> int:
    """Characters of tool result text / 4 (images are counted apart)."""
    return sum(r.chars for r in rows) // 4


def unwrap(rows: list[Row]) -> list[Row]:
    """A tools_call(name, arguments) row as the tool it ran, so the rules below see it."""
    out = []
    for r in rows:
        if r.tool == "tools_call" and isinstance(r.args.get("name"), str):
            args = r.args.get("arguments") or {}
            r = Row(r.args["name"], args if isinstance(args, dict) else {}, r.ok, r.code, r.chars, r.images, r.text)
        out.append(r)
    return out


def wrong_turns(rows: list[Row], *, images: str = "any", urls: tuple[str, ...] = (),
                listed: tuple[str, ...] = ("studio",)) -> list[str]:
    """What a careful agent following the skill would not have done. `images`: "never" (the task
    needs no image), "crop" (a crop is fine, a whole screen is not) or "any". `urls`: the url
    prefixes the task is about; a page tool on another url is guessing. `listed`: the toolsets
    the agent's connection lists from the start."""
    out: list[str] = []
    checked: set[tuple[str, str]] = set()
    looked: set[str] = set()              # urls checked or compared in this run
    last: tuple[str, str] | None = None
    for r in rows:
        url = str(r.args.get("url", ""))
        if urls and url and r.tool in ("ui_check", "ui_diff", "system_check", "ui_baseline") \
                and not url.startswith(urls):
            out.append(f"{r.tool} on a url the task did not give ({url[:60]})")
        if r.tool == "ui_baseline" and url not in looked:
            out.append("ui_baseline before checking or comparing: it replaced the baseline")
        if r.tool in ("ui_check", "ui_diff", "system_check"):
            looked.add(url)
        if r.tool == "tools_more" and r.args.get("toolset") in listed:
            out.append(f"tools_more({r.args.get('toolset')}) though its tools were listed")
        key = (r.tool, json.dumps(r.args, sort_keys=True))
        if key == last:
            out.append(f"repeated {r.tool} with the same arguments")
        last = key if r.ok else None          # calling again after a failure (and a fix) is the loop
        if not r.ok and not (r.code or "").startswith(PAGE_ERRORS):
            out.append(f"{r.tool} failed: {r.code}")
        if r.tool in LANE_TOOLS:
            out.append(f"called the lane tool {r.tool} (studio takes a url)")
        if r.tool in ("ui_check", "system_check") and r.ok:
            same = json.dumps({k: v for k, v in r.args.items() if k not in ("since", "detail", "only")}, sort_keys=True)
            if (r.tool, same) in checked and not r.args.get("since"):
                out.append(f"{r.tool} again on {url.rsplit('/', 1)[-1]} without since")
            checked.add((r.tool, same))
        if r.args.get("detail") == "full":
            out.append(f"{r.tool} with detail=full")
        if r.tool == "ui_image":
            if images == "never":
                out.append("fetched an image the task did not need")
            elif images == "crop" and not r.args.get("crop"):
                out.append("fetched a whole screen where a crop would do")
    return out


# ---- the agent's answer ----

def final_json(text: str) -> dict[str, Any]:
    for line in reversed(text.strip().splitlines()):
        line = line.strip().strip("`")
        if line.startswith("{"):
            with contextlib.suppress(ValueError):
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
    return {}


# ---- Claude Code as the agent ----

def _result_text(content: Any) -> tuple[str, int]:
    if isinstance(content, str):
        return content, 0
    texts, images = [], 0
    for block in content or []:
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
        elif block.get("type") == "image":
            images += 1
    return "\n".join(texts), images


def transcript_rows(lines: list[str], server: str = "studio") -> tuple[list[Row], dict[str, Any], str]:
    """Trace rows of the studio tools, the result event's metrics, and the final text."""
    prefix = f"mcp__{server}__"
    uses: dict[str, tuple[str, dict]] = {}
    rows: list[Row] = []
    metrics: dict[str, Any] = {}
    final = ""
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "assistant":
            for block in ev.get("message", {}).get("content", []) or []:
                if block.get("type") == "tool_use":
                    uses[block.get("id")] = (block.get("name", ""), block.get("input") or {})
        elif kind == "user":
            for block in ev.get("message", {}).get("content", []) or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                name, args = uses.get(block.get("tool_use_id"), ("", {}))
                text, images = _result_text(block.get("content"))
                ok = not block.get("is_error")
                tool = name.removeprefix(prefix)
                rows.append(Row(tool, args, ok, None if ok else text.split(":", 1)[0].strip()[:40], len(text), images,
                                text))
        elif kind == "result":
            final = str(ev.get("result") or "")
            usage = ev.get("usage") or {}
            metrics = {"turns": ev.get("num_turns"), "cost_usd": ev.get("total_cost_usd"),
                       "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                       "cache_read_tokens": usage.get("cache_read_input_tokens")}
    return rows, {k: v for k, v in metrics.items() if v is not None}, final


async def claude_agent(prompt: str, *, studio: Studio, folder: Path, model: str, transcript: Path,
                       timeout_s: float = 420) -> tuple[list[Row], dict[str, Any], dict[str, Any], str | None]:
    """Run Claude Code on one task. Returns (rows, answer, metrics, error)."""
    if shutil.which("claude") is None:
        return [], {}, {}, "the claude command is not installed"
    with tempfile.TemporaryDirectory(prefix="studio-eval-") as tmp:
        config = Path(tmp) / "mcp.json"
        config.write_text(json.dumps({"mcpServers": {"studio": {
            "type": "http", "url": studio.agent_url, "headers": {"Authorization": f"Bearer {studio.token}"}}}}))
        config.chmod(0o600)
        builtin = "Read,Edit,Write,Glob"
        argv = ["claude", "-p", prompt, "--mcp-config", str(config), "--strict-mcp-config",
                "--tools", builtin, "--allowedTools", f"mcp__studio,{builtin}",
                "--append-system-prompt-file", str(SKILL), "--model", model,
                "--permission-mode", "acceptEdits", "--output-format", "stream-json", "--verbose"]
        proc = await asyncio.create_subprocess_exec(*argv, cwd=folder, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        error = None
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            error = f"ran past {timeout_s:.0f} s"
        text = out.decode(errors="replace")
        transcript.write_text(text)
        if err:
            transcript.with_suffix(".err").write_bytes(err)
        rows, metrics, final = transcript_rows(text.splitlines())
        if proc.returncode not in (0, None) and error is None:
            error = f"claude exited with {proc.returncode}: {err.decode(errors='replace').strip()[-200:]}"
        return rows, final_json(final), metrics, error
