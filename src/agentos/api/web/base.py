"""What every contract module shares: the one org, its routes, and running a node route in place.

`call`, `read` and `write` run a real node route inside this request, with the same cookie, CSRF
header and origin, so a contract route never re-implements who may do what. It reshapes what the
node answers, and that is all it does.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from agentos.api.app import Api, endpoint
from agentos.api.errors import ApiError, error_body

log = logging.getLogger("agentos.api")

ORG = "local"
"""A node is one org to the app: `/local/needs-you`. Not on the reserved list in web/DESIGN.md."""

Handler = Callable[[Request], Awaitable[Any]]
_DROPPED = {b"content-length", b"content-type", b"accept-encoding"}


@dataclass
class Reply:
    status: int
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: bytes = b""

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None

    def header(self, name: str) -> str | None:
        wanted = name.lower().encode()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value.decode("latin-1")
        return None


class Refused(Exception):
    """A node route said no. The app gets its answer as it was, status and envelope alike."""

    def __init__(self, reply: Reply):
        super().__init__(f"node route answered {reply.status}")
        self.reply = reply

    def response(self) -> Response:
        return Response(self.reply.body, status_code=self.reply.status, media_type="application/json")


def _scope(request: Request, method: str, path: str, *, query: Mapping[str, Any] | None, body: Any,
           cookie: str | None, raw: bytes | None = None, content_type: str | None = None) -> tuple[Scope, bytes]:
    """The scope of `method path` on this node, carrying the caller's headers (cookie, CSRF, origin).
    `raw` is sent as it is (a file's bytes); otherwise `body` is sent as JSON."""
    if raw is not None:
        payload = raw
    else:
        payload = b"" if body is None else json.dumps(body).encode()
    headers = [(k, v) for k, v in request.scope["headers"] if k.lower() not in _DROPPED]
    if raw is not None:
        headers += [(b"content-type", (content_type or "application/octet-stream").encode()),
                    (b"content-length", str(len(payload)).encode())]
    elif body is not None:
        headers += [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    if cookie is not None:
        headers = [(k, v) for k, v in headers if k.lower() != b"cookie"] + [(b"cookie", cookie.encode("latin-1"))]
    pairs = {k: v for k, v in (query or {}).items() if v is not None}
    scope: Scope = {
        **request.scope,
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": urlencode(pairs).encode(),
        "headers": headers,
        "path_params": {},
        "state": {},
    }
    return scope, payload


async def call(request: Request, method: str, path: str, *, query: Mapping[str, Any] | None = None,
               body: Any = None, cookie: str | None = None, raw: bytes | None = None,
               content_type: str | None = None) -> Reply:
    """Run `method path` on this node, inside this request. `cookie` replaces the caller's, for the
    moment right after a sign-in, when the new session is not on the request yet."""
    scope, payload = _scope(request, method, path, query=query, body=body, cookie=cookie, raw=raw,
                            content_type=content_type)
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    reply = Reply(status=500)
    chunks: list[bytes] = []

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            reply.status = message["status"]
            reply.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await request.app.router(scope, receive, send)
    reply.body = b"".join(chunks)
    return reply


async def holder_names(request: Request) -> dict[str, str]:
    """Who can hold an account, in words: the owner, or a member by name. A holder this list does not name
    reads as a person, never as an id."""
    reply = await call(request, "GET", "/api/members")  # the members list: an admin's; anyone else gets the owner only
    members = reply.json() if reply.status == 200 else []
    names = {"owner": "The owner"}
    names.update({m["id"]: m.get("display_name") or m.get("handle") or "A person" for m in members if m.get("id")})
    return names


def control_of(control: dict[str, Any] | None, holders: Mapping[str, str]) -> dict[str, Any]:
    """An account's control as the app's `LaneControl`: `by` is who holds it, in words, and `by_id` is the id.
    The app decides from `by_id` alone whether the holder is the person looking, so it is always sent."""
    out = dict(control or {})
    # Only a person in control holds it: the node keeps the last holder's id after a hand back, and that is not one now.
    holder = out.get("by") if out.get("state") == "human" else None
    out["by"] = holders.get(holder, "A person") if holder else None
    out["by_id"] = holder or None
    return out


class Opened:
    """A node route that answered with a stream (server-sent events). `status` and `headers` are its
    first answer; `chunks` yields its body as it is written, and stops when the node does."""

    def __init__(self, status: int, headers: list[tuple[bytes, bytes]], chunks: AsyncIterator[bytes]):
        self.status = status
        self.headers = headers
        self.chunks = chunks


async def open_stream(request: Request, path: str, *, query: Mapping[str, Any] | None = None) -> Opened:
    """GET a node route that streams, inside this request, and return once it has answered. Its
    body is then read as it arrives, so an event reaches the caller as the node writes it. When the
    caller goes away, the node's route is cancelled with it."""
    scope, payload = _scope(request, "GET", path, query=query, body=None, cookie=None)
    started: asyncio.Future[tuple[int, list[tuple[bytes, bytes]]]] = asyncio.get_running_loop().create_future()
    chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
    gone = asyncio.Event()
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            if not started.done():
                started.set_result((message["status"], list(message.get("headers", []))))
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                chunks.put_nowait(body)
            if not message.get("more_body", False):
                chunks.put_nowait(None)

    async def run() -> None:
        try:
            await request.app.router(scope, receive, send)
        except Exception as exc:
            if started.done():
                log.exception("a node route that streams failed after it answered")
            else:
                started.set_exception(exc)  # the caller hears it as the node's refusal would be
        finally:
            if not started.done():
                started.set_exception(RuntimeError("the node route ended without an answer"))
            chunks.put_nowait(None)

    task = asyncio.ensure_future(run())
    try:
        status, headers = await started
    except BaseException:
        task.cancel()
        raise

    async def body() -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await chunks.get()
                if chunk is None:
                    return
                yield chunk
        finally:
            gone.set()
            task.cancel()

    return Opened(status, headers, body())


def unwrap(reply: Reply) -> Any:
    if 200 <= reply.status < 300:
        return reply.json()
    raise Refused(reply)


async def read(request: Request, path: str, *, query: Mapping[str, Any] | None = None) -> Any:
    """GET a node route, or pass its refusal on."""
    return unwrap(await call(request, "GET", path, query=query))


async def write(request: Request, method: str, path: str, body: Any = None, *,
                query: Mapping[str, Any] | None = None) -> Any:
    """POST, PUT, PATCH or DELETE a node route, or pass its refusal on."""
    return unwrap(await call(request, method, path, query=query, body=body))


async def write_raw(request: Request, method: str, path: str, raw: bytes, *, content_type: str,
                    query: Mapping[str, Any] | None = None) -> Any:
    """POST a node route the bytes of a file, as they are, or pass its refusal on."""
    return unwrap(await call(request, method, path, query=query, raw=raw, content_type=content_type))


def _kept(fn: Handler, org: bool) -> Handler:
    async def handler(request: Request) -> Any:
        if org and request.path_params.get("org") != ORG:
            raise ApiError("not_found", "This computer has no such organisation.")
        try:
            return await fn(request)
        except Refused as refused:
            return refused.response()

    handler.__name__ = fn.__name__
    return handler


def org_route(api: Api, path: str, fn: Handler, methods: list[str], **kwargs: Any) -> Route:
    """A route of the contract at `/api/orgs/{org}<path>`. It takes `endpoint`'s `public`, `owner`
    and `cap`, and answers a 404 for any org but this node's."""
    return Route(f"/api/orgs/{{org}}{path}", endpoint(api, _kept(fn, True), **kwargs), methods=methods)


def plain_route(api: Api, path: str, fn: Handler, methods: list[str], **kwargs: Any) -> Route:
    """A route of the contract with no org in it, such as `/api/session`."""
    return Route(path, endpoint(api, _kept(fn, False), **kwargs), methods=methods)


class ThroughToTheNode:
    """Everything the contract does not reshape: `/api/orgs/local/x` is the node's `/api/x`, with
    its own auth, its own checks and its own shape. WebSockets and the event stream pass here too."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        params = scope.get("path_params", {})
        if params.get("org") != ORG:
            await self._not_here(scope, receive, send)
            return
        rest = scope["path"].split(f"/api/orgs/{ORG}", 1)[-1]
        path = "/api" + rest
        inner = {**scope, "path": path, "raw_path": path.encode(), "root_path": "", "path_params": {}}
        await scope["app"].router(inner, receive, send)

    async def _not_here(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4404})
            return
        body = JSONResponse(error_body("not_found", "This computer has no such organisation."), status_code=404)
        await body(scope, receive, send)
