"""The node's one HTTP app: MCP, and the panel and viewer when they are given.

    app = build_app(runtime, mcp, api_app=panel, root_app=panel, viewer_app=viewer)

- Every request first passes a trusted-Host check (localhost, 127.0.0.1 and [::1] on any port,
  plus `extra_allowed_hosts` from node.toml when set); anything else gets 421, so a DNS
  rebinding page cannot reach /mcp, /api or /view.
- `/api/...` goes to `api_app`; `/view/...` and `/viewer-ws/...` go to `viewer_app`. They are
  mounted only when given, see the full path (their routes are declared as "/api/...") and
  bring their own auth (panel sessions, signed viewer links).
- `/mcp` (and `/.well-known/...`, where MCP clients look for auth metadata) needs
  `Authorization: Bearer <token>`. The token is bound (agentos.mcp.registry.bind_request_token,
  plus an optional extra binder such as agentos.policy's) for the duration of the request, with
  the MCP session id and the `?toolsets=` the URL asks for (agentos.mcp.toolsets). Each HTTP request
  also carries an event in its scope (CLIENT_GONE_KEY), set when the client disconnects or the
  request ends: a tool call still running for it is cancelled (agentos.mcp.registry.call_tool).
- Everything else goes to `root_app` when given (the built panel: public static files and the
  page shell, never data), else to the bearer-guarded MCP app as well.
- ASGI lifespan reaches every mounted app (the MCP session manager, the panel's live-update
  pump): started in order, MCP first, and stopped in reverse. Feature services start and stop
  with the daemon, not with an HTTP app.
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager, ExitStack
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from agentos.core.http_errors import WWW_AUTHENTICATE, error_body, status_of
from agentos.core.logs import log_context, new_request_id
from agentos.core.runtime import Runtime
from agentos.mcp.registry import CLIENT_GONE_KEY, bind_request_token
from agentos.mcp.toolsets import bind_connection

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
API_PREFIX = "/api"
SITES_PREFIX = "/v1/sites"  # the developer surface of site APIs, on the same app as /api (docs/SITE-APIS.md)
VIEW_PREFIXES = ("/view", "/viewer-ws", "/viewer-assets")
WELL_KNOWN = "/.well-known"
LIFESPAN_STOP_TIMEOUT = 10.0
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")

TokenBinder = Callable[..., AbstractContextManager[Any]]
"""`bind(token)` (or `bind(token, session=...)`) -> a context manager held for the whole request."""
TokenCheck = Callable[[str], bool]
TokenRefusal = Callable[[str], "tuple[str, str] | None"]  # (code, message) for a refusal the caller must be told
"""`accept(token)` -> True for a token the node did not issue itself (a policy grant)."""

HOST_RE = re.compile(r"^(?P<host>\[[0-9a-f:.]+\]|[a-z0-9._-]+)(?::(?P<port>\d{1,5}))?$")


def parse_host(value: str) -> tuple[str, int | None] | None:
    """`localhost:7400` -> ("localhost", 7400); `[::1]` -> ("[::1]", None); malformed -> None."""
    match = HOST_RE.match(value.strip().lower())
    if not match:
        return None
    port = match.group("port")
    return match.group("host"), int(port) if port else None


def allowed_hosts(config: Any) -> list[str]:
    """The local names plus `extra_allowed_hosts` from the config, when it has that setting."""
    extra = getattr(config, "extra_allowed_hosts", None) or []
    return [*LOCAL_HOSTS, *extra]


class TrustedHosts:
    """Pure ASGI: refuse any request whose Host header is not one of ours, on every route.

    An entry without a port allows any port; `host:port` allows that port only.
    """

    def __init__(self, app: ASGIApp, allowed: Iterable[str]):
        self.app = app
        self._any_port: set[str] = set()
        self._exact: set[tuple[str, int]] = set()
        for entry in allowed:
            parsed = parse_host(entry)
            if parsed is None:
                raise ValueError(f"not a host name or host:port: {entry!r}")
            host, port = parsed
            if port is None:
                self._any_port.add(host)
            else:
                self._exact.add((host, port))

    def allows(self, header: str | None) -> bool:
        parsed = parse_host(header) if header else None
        if parsed is None:
            return False
        host, port = parsed
        return host in self._any_port or (port is not None and (host, port) in self._exact)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = _header(scope, b"host")
            if host is None or not self.allows(host):
                log.warning("refused a request for host %r", (host or "")[:100])
                detail = "this node answers only on localhost and the hosts in extra_allowed_hosts (node.toml)"
                response = JSONResponse(error_body("misdirected", detail), status_code=status_of("misdirected"))
                if scope.get("path", "").startswith(SITES_PREFIX):  # every error there is a problem document
                    response = JSONResponse({"type": "urn:agentos:site-api:misdirected", "title": "Misdirected request",
                                             "status": 421, "code": "misdirected", "detail": detail}, status_code=421,
                                            media_type="application/problem+json")
                if scope["type"] == "http":
                    await response(scope, receive, send)
                elif "websocket.http.response" in scope.get("extensions", {}):
                    await WebSocket(scope, receive, send).send_denial_response(response)
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
            if host != host.lower():  # host names are case-insensitive; the SDK's own check is not
                scope = dict(scope)
                scope["headers"] = [(k, v.lower() if k == b"host" else v) for k, v in scope["headers"]]
        await self.app(scope, receive, send)


class PrefixRouter:
    """Pure ASGI: the first app whose prefix matches gets the request, path unchanged; the
    default app gets everything else. Lifespan goes to every distinct app (see Lifespans)."""

    def __init__(self, default: ASGIApp, routes: Sequence[tuple[str, ASGIApp]] = (),
                 lifespan_order: Sequence[ASGIApp] | None = None):
        self.default = default
        self.routes = [(prefix.rstrip("/"), app) for prefix, app in routes]
        apps: list[ASGIApp] = []
        for app in [*(lifespan_order or []), default, *(a for _, a in self.routes)]:
            if all(app is not seen for seen in apps):
                apps.append(app)
        self.lifespan_apps = apps

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            for prefix, app in self.routes:
                if path == prefix or path.startswith(prefix + "/"):
                    await app(scope, receive, send)
                    return
        elif scope["type"] == "lifespan" and len(self.lifespan_apps) > 1:
            await Lifespans(self.lifespan_apps)(scope, receive, send)
            return
        await self.default(scope, receive, send)


class _LifespanChild:
    """One app's lifespan, driven over its own queues in its own task."""

    def __init__(self, app: ASGIApp, scope: Scope):
        self.app, self.scope = app, scope
        self.inbox: asyncio.Queue[dict] = asyncio.Queue()
        self.outbox: asyncio.Queue[dict] = asyncio.Queue()
        self.task: asyncio.Task | None = None

    async def _run(self) -> None:
        try:
            await self.app(dict(self.scope), self.inbox.get, self.outbox.put)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001  (reported to the parent as a failure or "no lifespan")
            await self.outbox.put({"type": "agentos.exited", "error": exc})
            return
        await self.outbox.put({"type": "agentos.exited"})

    async def startup(self) -> str | None:
        """None when started (or when the app has no lifespan), else the failure message."""
        self.task = asyncio.create_task(self._run())
        await self.inbox.put({"type": "lifespan.startup"})
        msg = await self.outbox.get()
        if msg["type"] == "lifespan.startup.complete":
            return None
        if msg["type"] == "lifespan.startup.failed":
            return str(msg.get("message") or "startup failed")
        if msg.get("error") is not None:
            log.info("%s has no lifespan (%s); serving it without one", type(self.app).__name__,
                     type(msg["error"]).__name__)
        return None

    async def shutdown(self) -> None:
        if self.task is None or self.task.done():
            return
        await self.inbox.put({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(self._until_stopped(), LIFESPAN_STOP_TIMEOUT)
        except TimeoutError:
            log.error("%s did not finish its shutdown within %ss", type(self.app).__name__, LIFESPAN_STOP_TIMEOUT)
            self.task.cancel()

    async def _until_stopped(self) -> None:
        while True:
            msg = await self.outbox.get()
            if msg["type"] in ("lifespan.shutdown.complete", "lifespan.shutdown.failed", "agentos.exited"):
                break
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)


class Lifespans:
    """Pure ASGI lifespan for several apps (a mounted app otherwise never hears startup or
    shutdown): each starts in order and stops in reverse. An app without lifespan support is
    served without one; a failed startup stops the ones already started and fails the server."""

    def __init__(self, apps: Sequence[ASGIApp]):
        self.apps = list(apps)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        if message["type"] != "lifespan.startup":
            return
        started: list[_LifespanChild] = []
        for app in self.apps:
            child = _LifespanChild(app, scope)
            failure = await child.startup()
            if failure is not None:
                for done in reversed(started):
                    await done.shutdown()
                await send({"type": "lifespan.startup.failed", "message": failure})
                raise RuntimeError(f"a mounted app failed to start: {failure}")  # as Starlette does
            started.append(child)
        await send({"type": "lifespan.startup.complete"})
        while (await receive())["type"] != "lifespan.shutdown":
            pass
        for child in reversed(started):
            await child.shutdown()
        await send({"type": "lifespan.shutdown.complete"})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


def _takes_session(binder: TokenBinder) -> bool:
    try:
        params = inspect.signature(binder).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "session" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


class BearerAuth:
    """Pure ASGI middleware: every HTTP request needs `Authorization: Bearer <token>`.

    Accepted: the node's own token (node.toml), or any token `accept` vouches for. The token is
    bound for the whole request, so the tool call the SDK dispatches sees it. Pure ASGI (not
    BaseHTTPMiddleware) so streamed responses pass through untouched; lifespan events pass
    through so the MCP session manager starts and stops.
    """

    def __init__(self, app: ASGIApp, token: str, *, accept: TokenCheck | None = None,
                 bind: TokenBinder | None = None, explain: TokenRefusal | None = None):
        if not token:
            raise ValueError("mcp_token is empty; run `agentos node init` first")
        self.app = app
        self._token = token.encode()
        self._accept = accept
        self._explain = explain
        self._bind = bind
        self._bind_session = bind is not None and _takes_session(bind)

    def _supplied(self, scope: Scope) -> bytes | None:
        header = b""
        for key, value in scope.get("headers", ()):
            if key == b"authorization":
                header = value
                break
        scheme, _, supplied = header.partition(b" ")
        supplied = supplied.strip()
        if scheme.lower() != b"bearer" or not supplied:
            return None
        return supplied

    def _authorized(self, supplied: bytes | None) -> bool:
        if supplied is None:
            return False
        # compare_digest is constant time for equal-length inputs; length alone leaks nothing useful.
        if hmac.compare_digest(supplied, self._token):
            return True
        if self._accept is None:
            return False
        try:
            return bool(self._accept(supplied.decode("latin-1")))
        except Exception:
            log.warning("token check failed; refusing the request", exc_info=True)
            return False

    def _refusal(self, supplied: bytes | None) -> tuple[str, str] | None:
        if supplied is None or self._explain is None:
            return None
        try:
            return self._explain(supplied.decode("latin-1"))
        except Exception:
            log.warning("token refusal check failed", exc_info=True)
            return None

    def _bind_all(self, stack: ExitStack, token: str, scope: Scope) -> None:
        stack.enter_context(bind_request_token(token))
        stack.enter_context(bind_connection(session=_header(scope, b"mcp-session-id"),
                                            query_string=scope.get("query_string", b"")))
        if self._bind is not None:
            if self._bind_session:
                stack.enter_context(self._bind(token, session=_header(scope, b"mcp-session-id")))
            else:
                stack.enter_context(self._bind(token))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        supplied = self._supplied(scope)
        if not self._authorized(supplied):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            surface = "mcp" if str(scope.get("path", "")).startswith("/mcp") else None
            told = self._refusal(supplied)
            if told is not None:  # a grant a freeze revoked: say so, never send the agent to sign in again
                code, message = told
                response = JSONResponse(error_body(code, message, surface=surface), status_code=status_of(code))
            else:
                response = JSONResponse(
                    error_body("unauthorized", "Send Authorization: Bearer <token> with the token from node.toml.",
                               surface=surface),
                    status_code=status_of("unauthorized"), headers=WWW_AUTHENTICATE)
            await response(scope, receive, send)
            return
        assert supplied is not None
        with ExitStack() as stack:
            self._bind_all(stack, supplied.decode("latin-1"), scope)
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return
            gone = asyncio.Event()  # the client went away, or nobody waits for this answer any more
            scope[CLIENT_GONE_KEY] = gone

            async def watched() -> dict:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    gone.set()
                return message

            try:
                await self.app(scope, watched, send)
            finally:
                gone.set()  # the request is over: a tool call still running for it answers nobody


def sdk_transport_security(hosts: Iterable[str]) -> TransportSecuritySettings:
    """The SDK's own DNS-rebinding check on /mcp, kept in line with TrustedHosts (plus Origin)."""
    allowed_hosts: list[str] = []
    allowed_origins: list[str] = []
    for entry in hosts:
        parsed = parse_host(entry)
        if parsed is None:
            raise ValueError(f"not a host name or host:port: {entry!r}")
        host, port = parsed
        if port is None:
            allowed_hosts += [host, f"{host}:*"]
            allowed_origins += [f"{scheme}://{host}{suffix}" for scheme in ("http", "https") for suffix in ("", ":*")]
        else:
            allowed_hosts.append(f"{host}:{port}")
            allowed_origins += [f"http://{host}:{port}", f"https://{host}:{port}"]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts,
                                     allowed_origins=allowed_origins)


class RequestIds:
    """Pure ASGI middleware: each HTTP request and websocket gets a request id on every log line
    it causes (agentos.core.logs). Tool calls and panel routes add their own ids below it."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        with log_context(request=new_request_id()):
            await self.app(scope, receive, send)


def build_app(
    runtime: Runtime,
    mcp: MCPServer,
    *,
    api_app: ASGIApp | None = None,
    viewer_app: ASGIApp | None = None,
    root_app: ASGIApp | None = None,
    bind_token: TokenBinder | None = None,
    accept_token: TokenCheck | None = None,
    explain_token: TokenRefusal | None = None,
) -> ASGIApp:
    """The node's HTTP app. `mcp` is the server from agentos.mcp.registry.build_mcp; the config
    (token, host, extra_allowed_hosts) comes from the runtime. `root_app` serves every path no
    other app claims (the panel's page shell); without it those go to MCP's bearer check."""
    config = runtime.require("config")
    hosts = allowed_hosts(config)
    mcp_app = mcp.streamable_http_app(streamable_http_path=MCP_PATH, host=config.mcp_host,
                                      transport_security=sdk_transport_security(hosts))
    guarded = BearerAuth(mcp_app, config.mcp_token, accept=accept_token, bind=bind_token, explain=explain_token)
    routes: list[tuple[str, ASGIApp]] = []
    if api_app is not None:
        routes += [(API_PREFIX, api_app), (SITES_PREFIX, api_app)]
    if viewer_app is not None:
        routes += [(prefix, viewer_app) for prefix in VIEW_PREFIXES]
    if root_app is not None:
        routes += [(MCP_PATH, guarded), (WELL_KNOWN, guarded)]
    return RequestIds(TrustedHosts(PrefixRouter(root_app or guarded, routes, lifespan_order=[guarded]), hosts))
