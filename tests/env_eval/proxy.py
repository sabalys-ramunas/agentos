"""A recording MCP proxy: every agent reaches the node through it, so every agent is measured the same way.

The agent's MCP config names http://127.0.0.1:<proxy port>/mcp; the proxy forwards each request to
the node as it came (headers, the session id, the bearer token) and streams the answer back
unchanged. On the way it reads the JSON-RPC traffic of POST requests (a JSON body or an SSE stream)
and records:

- each `tools/call`: the tool, its arguments, when it started and ended, ok or the error's code,
  the text the agent got back (what it reads) and how many images;
- each `tools/list`: how many tools and their size in tokens, estimated as the toolset budget test
  does (tests/unit/test_toolsets.py: characters of each tool's JSON / 4);
- `initialize`: the server's instructions, in tokens the same way.

So a scripted agent, Claude Code, Codex or any other command are counted from the MCP side, never
from what they say about themselves.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Self

HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
       "proxy-authorization", "proxy-authenticate", "accept-encoding"}


@dataclass
class Call:
    tool: str
    args: dict[str, Any]
    started: float                      # time.monotonic()
    ended: float | None = None
    ok: bool | None = None
    code: str | None = None             # the error's code ("which_environment"), when it failed
    text: str = ""                      # the result's text blocks, as the agent read them
    images: int = 0
    session: str | None = None          # the MCP session id it came on

    @property
    def seconds(self) -> float:
        return round((self.ended or self.started) - self.started, 3)

    def brief(self, t0: float = 0.0) -> dict[str, Any]:
        return {"tool": self.tool, "args": _short_args(self.args), "ok": self.ok, "code": self.code,
                "chars": len(self.text), "images": self.images, "at_s": round(self.started - t0, 2),
                "seconds": self.seconds}


@dataclass
class Listing:
    at: float
    tools: int
    tokens: int
    names: list[str]


@dataclass
class Recorder:
    calls: list[Call] = field(default_factory=list)
    listings: list[Listing] = field(default_factory=list)
    instructions_tokens: int = 0

    def since(self, mark: tuple[int, int]) -> tuple[list[Call], list[Listing]]:
        return self.calls[mark[0]:], self.listings[mark[1]:]

    def mark(self) -> tuple[int, int]:
        return len(self.calls), len(self.listings)


def tool_tokens(tools: list[dict[str, Any]]) -> int:
    """The toolset budget test's estimate (characters of each tool's JSON / 4)."""
    return sum(len(json.dumps(t)) for t in tools) // 4


def _short_args(args: dict[str, Any]) -> dict[str, Any]:
    """Arguments as kept in results: long strings cut (a file's content), `on` hidden (a handle)."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if k == "on":
            out[k] = "<on>"
        elif isinstance(v, str) and len(v) > 300:
            out[k] = v[:300] + f"... ({len(v)} chars)"
        elif isinstance(v, dict):
            out[k] = _short_args(v)
        else:
            out[k] = v
    return out


def messages(body: bytes, content_type: str) -> list[dict[str, Any]]:
    """The JSON-RPC messages of a body: JSON (one or a batch) or an SSE stream's data lines."""
    text = body.decode("utf-8", "replace")
    found: list[Any] = []
    if "text/event-stream" in content_type:
        data: list[str] = []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line.strip() and data:
                with contextlib.suppress(ValueError):
                    found.append(json.loads("\n".join(data)))
                data = []
    else:
        with contextlib.suppress(ValueError):
            found.append(json.loads(text))
    out: list[dict[str, Any]] = []
    for m in found:
        out += [x for x in (m if isinstance(m, list) else [m]) if isinstance(x, dict)]
    return out


def result_text(result: dict[str, Any]) -> tuple[str, int]:
    texts, images = [], 0
    for block in result.get("content") or []:
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") in ("image", "audio"):
            images += 1
        elif block.get("type") == "resource":
            texts.append(json.dumps(block.get("resource") or {}))
    return "\n".join(texts), images


def error_code(text: str) -> str:
    head = text.strip().split(":", 1)[0].strip()
    return head if head and " " not in head and len(head) <= 40 else "error"


class McpProxy:
    """Serve on 127.0.0.1:`port`, forward to `upstream` (http://127.0.0.1:<node port>)."""

    def __init__(self, upstream: str, port: int, recorder: Recorder | None = None):
        self.upstream = upstream.rstrip("/")
        self.port = port
        self.recorder = recorder or Recorder()
        self._server: Any = None
        self._task: asyncio.Task | None = None
        self._client: Any = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    async def __aenter__(self) -> Self:
        import httpx
        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Route

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10))
        app = Starlette(routes=[Route("/{path:path}", self._handle, methods=["GET", "POST", "DELETE"])])
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                                     lifespan="off", access_log=False))
        self._server.capture_signals = contextlib.nullcontext  # the runner keeps its own Ctrl-C
        self._task = asyncio.create_task(self._server.serve())
        deadline = time.monotonic() + 15
        while not self._server.started:
            if self._task.done():
                self._task.result()
                raise RuntimeError("the MCP proxy stopped while starting")
            if time.monotonic() > deadline:
                raise RuntimeError(f"the MCP proxy did not start on port {self.port}")
            await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, 10)
        if self._client is not None:
            await self._client.aclose()

    async def _handle(self, request: Any) -> Any:
        from starlette.responses import Response, StreamingResponse

        body = await request.body()
        url = self.upstream + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP]
        headers.append(("accept-encoding", "identity"))  # the bytes as they are, to read them
        session = request.headers.get("mcp-session-id")
        pending = self._requests(body, session) if request.method == "POST" else {}
        try:
            upstream = await self._client.send(self._client.build_request(request.method, url, headers=headers,
                                                                          content=body), stream=True)
        except Exception as e:  # noqa: BLE001 - the node is gone: said as an HTTP error, as a client would see it
            self._finish(pending, [], failed=f"node_unreachable: {type(e).__name__}")
            return Response(f"the node did not answer: {type(e).__name__}", status_code=502)
        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP}
        kind = upstream.headers.get("content-type", "")

        async def stream() -> Any:
            buf = bytearray()
            try:
                async for chunk in upstream.aiter_raw():
                    if pending:
                        buf += chunk
                    yield chunk
            finally:
                await upstream.aclose()
                if pending:
                    self._finish(pending, messages(bytes(buf), kind))

        return StreamingResponse(stream(), status_code=upstream.status_code, headers=out_headers)

    def _requests(self, body: bytes, session: str | None) -> dict[Any, tuple[str, Any]]:
        """The JSON-RPC requests this POST carries that are recorded: id -> (method, Call or None)."""
        pending: dict[Any, tuple[str, Any]] = {}
        for m in messages(body, "application/json"):
            method, ident = m.get("method"), m.get("id")
            if ident is None or method not in ("tools/call", "tools/list", "initialize"):
                continue
            call = None
            if method == "tools/call":
                params = m.get("params") or {}
                call = Call(str(params.get("name") or ""), dict(params.get("arguments") or {}), time.monotonic(),
                            session=session)
                self.recorder.calls.append(call)
            pending[ident] = (method, call)
        return pending

    def _finish(self, pending: dict[Any, tuple[str, Any]], answers: list[dict[str, Any]], *,
                failed: str | None = None) -> None:
        now = time.monotonic()
        by_id = {a.get("id"): a for a in answers if "id" in a}
        for ident, (method, call) in pending.items():
            answer = by_id.get(ident)
            result = (answer or {}).get("result") or {}
            if method == "tools/call":
                call.ended = now
                if answer is None or "error" in answer:
                    err = (answer or {}).get("error") or {}
                    call.ok, call.text = False, failed or str(err.get("message") or "no answer")
                    call.code = "node_unreachable" if failed else "jsonrpc_error"
                    continue
                call.text, call.images = result_text(result)
                call.ok = not result.get("isError")
                call.code = None if call.ok else error_code(call.text)
            elif method == "tools/list" and answer is not None:
                tools = result.get("tools") or []
                self.recorder.listings.append(Listing(now, len(tools), tool_tokens(tools),
                                                      [str(t.get("name")) for t in tools]))
            elif method == "initialize" and answer is not None:
                self.recorder.instructions_tokens = len(str(result.get("instructions") or "")) // 4
