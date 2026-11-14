"""The owner's side of the controller: sign-in, the JSON API, the pages, live view and terminal.

Sign-in reuses the panel's one-time codes (agentos.api.auth.PanelStore, in the controller
home): `agentos controller login` prints a link whose code rides in the URL fragment; the page
posts it once to `POST /api/session` and gets an HttpOnly, SameSite=Strict session cookie.
Scripts can send `Authorization: Bearer <owner_token>` from controller.toml instead. A cookie
request that changes anything must carry `X-Agentos-CSRF`. A session belongs to one org, and
every route reads and changes that org only.

    GET    /api/session                          {org, csrf} | 401
    POST   /api/session                          {code} -> sets the cookie
    DELETE /api/session
    GET    /api/status                           org, node counts, pending approvals, URLs
    GET    /api/nodes                            nodes with live state and lanes
    DELETE /api/nodes/{node_id}
    GET    /api/join-tokens ; POST {name?, capabilities?, ttl_minutes?} ; DELETE /api/join-tokens/{id}
    GET    /api/approvals?state=pending
    POST   /api/approvals/{node_id}/{approval_id}  {decision: approved|denied, answer?}
    GET    /api/activity?limit=
    GET    /api/grants ; POST {subject, workspaces?, lanes?, ceiling?, ttl_days?} ; DELETE /api/grants/{id}
    POST   /api/nodes/{node_id}/lanes/{ws}/{name}/control   {action: take|hand_back|stop_agent|deny_request}
    POST   /api/nodes/{node_id}/config           {config}
    WS     /v1/view/{node_id}/{ws}/{name}?mode=view|control     live view (noVNC), relayed as a node stream
    WS     /v1/terminal/{node_id}/{ws}/{name}?mode=view|control&cols=&rows=&session=   terminal, relayed as a
           node stream; typing reaches the shell only in control mode while the owner holds the lane

Node-supplied text is only ever placed with textContent by the pages, never as HTML.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from agentos.api.auth import tokens_equal
from agentos.controller.hooks import BODY_TIMEOUT_S, TOKEN_RE, HookGate
from agentos.core import http_errors
from agentos.core.errors import AgentosError, InvalidRequest, NotFound
from agentos.core.models import parse_lane_id
from agentos.core.ratelimit import RateLimiter
from agentos.node.channel import standing, wire
from agentos.node.channel.streams import StreamClosed
from agentos.node.channel.workflows import MAX_HOOK_BODY

log = logging.getLogger("agentos.controller.web")

STATIC = Path(__file__).parent / "static"
NOVNC = Path(__file__).parent.parent / "node" / "viewer" / "assets" / "vendor" / "novnc"
COOKIE = "agentos_controller_session"
CSRF_HEADER = "x-agentos-csrf"
CONTROL_ACTIONS = ("take", "hand_back", "stop_agent", "deny_request")
UNSUBSCRIBE_PAGE = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                    '<title>Unsubscribe</title><body>{body}</body>')
UNSUBSCRIBE_ASK = '<p>Stop these messages?</p><form method="post"><button>Unsubscribe</button></form>'
UNSUBSCRIBE_DONE = "<p>You will not get these messages any more.</p>"
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def _peer(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _slow_down(message: str, wait: float, *, status: int = 429, code: str = "rate_limited") -> JSONResponse:
    """Refused for now, and when to come back."""
    response = error(status, code, message)
    response.headers["Retry-After"] = str(max(1, math.ceil(wait)))
    return response


def error(status: int, code: str, message: str) -> JSONResponse:
    """The node's error shape and catalog (agentos.core.http_errors), with this app's headers."""
    headers = {**SECURITY_HEADERS, **(http_errors.WWW_AUTHENTICATE if status == 401 else {})}
    return JSONResponse(http_errors.error_body(code, message, surface="controller"), status_code=status,
                        headers=headers)


def agentos_error(exc: AgentosError) -> JSONResponse:
    return error(http_errors.status_for(exc), exc.code, exc.message)


def ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=SECURITY_HEADERS)


def same_origin(headers: Any) -> bool:
    origin = headers.get("origin")
    if not origin:
        return True
    return urlsplit(origin).netloc.lower() == (headers.get("host") or "").lower()


async def body_json(request: Request, *, limit: int = 64 * 1024) -> dict:
    raw = await request.body()
    if len(raw) > limit:
        raise InvalidRequest("the request body is too large")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise InvalidRequest("send a JSON object") from None
    if not isinstance(data, dict):
        raise InvalidRequest("send a JSON object")
    return data


def _bearer(conn: Request | WebSocket) -> str | None:
    """The token of an `Authorization: Bearer` header, or None (any other scheme is not a bearer)."""
    scheme, _, token = conn.headers.get("authorization", "").partition(" ")
    return (token.strip() or None) if scheme.lower() == "bearer" else None


class Web:
    def __init__(self, controller: Any):
        self.c = controller
        self.limiter = RateLimiter(per_key=10, global_max=60, window=300.0)
        self.hooks = HookGate()
        self.hook_body_timeout = BODY_TIMEOUT_S

    # ---- who is asking ----
    def _session(self, conn: Request | WebSocket) -> Any:
        sid = conn.cookies.get(COOKIE)
        return self.c.panel.get_session(sid) if sid else None

    def owner(self, conn: Request | WebSocket) -> str | None:
        """The org of the signed-in owner, or None."""
        token = _bearer(conn)
        if token is not None:
            return self.c.org["id"] if tokens_equal(token, self.c.config.owner_token) else None
        session = self._session(conn)
        if session is None:
            return None
        org = self.c.store.org(session.principal_id)
        return org["id"] if org else None

    def _guard(self, request: Request, *, change: bool) -> str | JSONResponse:
        org = self.owner(request)
        if org is None:
            return error(401, "unauthorized", "sign in with the link from `agentos controller login`")
        # only a script's bearer token skips CSRF; a cookie session always needs the header
        if change and (_bearer(request) is None or self._session(request) is not None):
            session = self._session(request)
            if session is None or not tokens_equal(request.headers.get(CSRF_HEADER), session.csrf):
                return error(403, "csrf_failed", "send the X-Agentos-CSRF header from GET /api/session")
            if not same_origin(request.headers):
                return error(403, "csrf_failed", "This request came from another site, so it was refused.")
        return org

    # ---- session ----
    async def session(self, request: Request) -> Response:
        if request.method == "GET":
            org = self.owner(request)
            if org is None:
                return error(401, "unauthorized", "sign in with the link from `agentos controller login`")
            session = self._session(request)
            return ok({"org": self.c.store.org(org), "csrf": session.csrf if session else None})
        if request.method == "DELETE":
            sid = request.cookies.get(COOKIE)
            if sid:
                self.c.panel.delete_session(sid)
            response = ok({"signed_out": True})
            response.delete_cookie(COOKIE, path="/")
            return response
        if not same_origin(request.headers):
            return error(403, "csrf_failed", "This request came from another site, so it was refused.")
        client = request.client.host if request.client else "unknown"
        wait = self.limiter.check(client)
        if wait is not None:
            return error(429, "rate_limited", f"too many sign-in attempts; try again in {int(wait)} seconds")
        try:
            data = await body_json(request)
        except AgentosError as exc:
            return agentos_error(exc)
        code = data.get("code")
        if not isinstance(code, str) or not code or not self.c.panel.use_code(code):
            return error(401, "unauthorized", "this sign-in link is not valid any more; run "
                                                 "`agentos controller login` for a new one")
        sid, session = self.c.panel.create_session(self.c.org["id"], self.c.config.session_hours * 3600)
        response = ok({"org": self.c.org, "csrf": session.csrf})
        response.set_cookie(COOKIE, sid, max_age=self.c.config.session_hours * 3600, httponly=True,
                            samesite="strict", secure=self.c.config.tls or self.c.config.behind_tls_proxy, path="/")
        return response

    # ---- API ----
    def _node_json(self, node: dict) -> dict:
        session = self.c.hub.sessions.get(node["id"])
        live = session.summary() if session is not None and session.org_id == node["org_id"] else None
        state = node.get("state") or {}
        lanes = {lane["id"]: lane for lane in self.c.store.lanes(node["org_id"], node["id"])}
        for lane in (live or state).get("lanes", []) if (live or state) else []:
            lanes.setdefault(lane["id"], {"id": lane["id"]}).update({k: v for k, v in lane.items() if v is not None})
        return {"id": node["id"], "name": node["name"], "online": live is not None,
                "degraded": live["degraded"] if live else bool(state.get("degraded")),
                "version": node.get("version"), "os": node.get("os"), "arch": node.get("arch"),
                "capabilities": node["capabilities"], "max_warm_lanes": node.get("max_warm_lanes"),
                "load": live["load"] if live else state.get("load"), "tools": live["tools"] if live else 0,
                "ceiling": live["ceiling"] if live else None,
                "reports": live.get("reports", []) if live else [],
                "created_at": wire.iso(node["created_at"]),
                "last_seen": wire.iso(node["last_seen"]) if node.get("last_seen") else None,
                "lanes": sorted(lanes.values(), key=lambda lane: lane["id"])}

    async def api(self, request: Request, name: str) -> Response:
        change = request.method != "GET"
        org = self._guard(request, change=change)
        if isinstance(org, JSONResponse):
            return org
        try:
            return await getattr(self, f"api_{name}")(request, org)
        except AgentosError as exc:
            return agentos_error(exc)

    async def api_status(self, request: Request, org: str) -> Response:
        nodes = self.c.store.nodes(org)
        online = [n for n in nodes if n["id"] in self.c.hub.sessions]
        return ok({"org": self.c.store.org(org), "nodes": len(nodes), "online": len(online),
                   "pending_approvals": len(self.c.store.approvals(org, "pending")), "mcp_url": self.c.config.mcp_url,
                   "node_url": self.c.config.node_url})

    async def api_nodes(self, request: Request, org: str) -> Response:
        return ok([self._node_json(n) for n in self.c.store.nodes(org)])

    async def api_node(self, request: Request, org: str) -> Response:
        node_id = request.path_params["node_id"]
        if not await self.c.store.run(self.c.store.remove_node, org, node_id):
            raise NotFound(f"no node {node_id}")
        session = self.c.hub.sessions.get(node_id)
        if session is not None and session.org_id == org:
            with contextlib.suppress(Exception):
                await session.ws.close(code=1008, reason="removed")
        return ok({"removed": node_id})

    async def api_join_tokens(self, request: Request, org: str) -> Response:
        if request.method == "GET":
            return ok(self.c.store.join_tokens(org))
        data = await body_json(request)
        row, token = self.c.create_join_token(org, name=data.get("name"), capabilities=data.get("capabilities"),
                                              ttl_minutes=data.get("ttl_minutes"))
        return ok({"id": row["id"], "token": token, "expires_at": wire.iso(row["expires_at"]),
                   "join_command": self.c.join_command(token)}, 201)

    async def api_join_token(self, request: Request, org: str) -> Response:
        if not await self.c.store.run(self.c.store.delete_join_token, org, request.path_params["token_id"]):
            raise NotFound("no unused join token with that id")
        return ok({"deleted": request.path_params["token_id"]})

    async def api_approvals(self, request: Request, org: str) -> Response:
        state = request.query_params.get("state") or None
        return ok(self.c.store.approvals(org, state))

    async def api_decide(self, request: Request, org: str) -> Response:
        data = await body_json(request)
        answer = data.get("answer") if isinstance(data.get("answer"), str) else None
        result = await self.c.hub.decide(org, request.path_params["node_id"], request.path_params["approval_id"],
                                         data.get("decision"), decided_by="owner", answer=answer,
                                         proof={"method": "panel", "level": "signed_in"})  # an owner signed in here
        return ok(result)

    async def api_activity(self, request: Request, org: str) -> Response:
        try:
            limit = max(1, min(int(request.query_params.get("limit", 100)), 500))
        except ValueError:
            raise InvalidRequest("limit must be a number") from None
        return ok(self.c.store.traces(org, limit))

    async def api_grants(self, request: Request, org: str) -> Response:
        if request.method == "GET":
            return ok([g.public() for g in self.c.grants.list(org)])
        data = await body_json(request)
        subject = data.get("subject")
        if not isinstance(subject, str) or not 1 <= len(subject) <= 120:
            raise InvalidRequest("subject names the agent, for example claude-code")
        ttl_days = data.get("ttl_days")
        grant, token = self.c.grants.create(
            org, subject=subject, workspaces=data.get("workspaces"), lanes=data.get("lanes"),
            ceiling=data.get("ceiling") or "irreversible", note=data.get("note"),
            ttl_s=int(ttl_days * 86400) if isinstance(ttl_days, int | float) and ttl_days > 0 else None)
        return ok({**grant.public(), "token": token, "mcp_url": self.c.config.mcp_url}, 201)

    async def api_grant(self, request: Request, org: str) -> Response:
        ids = await self.c.revoke_grant(org, request.path_params["grant_id"])
        return ok({"revoked": request.path_params["grant_id"], "revocation_ids": ids})

    async def api_lane_control(self, request: Request, org: str) -> Response:
        data = await body_json(request)
        action = data.get("action")
        if action not in CONTROL_ACTIONS:
            raise InvalidRequest(f"action must be one of {', '.join(CONTROL_ACTIONS)}")
        lane = _lane(request.path_params)
        await self.c.hub.lane_control(org, request.path_params["node_id"], lane, action, by="owner")
        return ok({"lane": lane, "action": action, "sent": True})

    async def api_standing(self, request: Request, org: str) -> Response:
        """{set | upsert, revoke}: the owner's standing approvals, passed through to one node, which checks
        each and answers which it applied (docs/PROTOCOL.md `standing`)."""
        # as much as the node takes: 500 items a list, in one frame (wire.MAX_JSON_BYTES, less room
        # for the envelope and signature)
        data = await body_json(request, limit=wire.MAX_JSON_BYTES - 16 * 1024)
        parts = {key: data.get(key) for key in ("set", "upsert", "revoke") if data.get(key) is not None}
        if not parts or any(not isinstance(v, list) for v in parts.values()):
            raise InvalidRequest("send {\"upsert\": [...]}, {\"set\": [...]} or {\"revoke\": [ids]}")
        if any(len(v) > standing.MAX_ITEMS for v in parts.values()):
            raise InvalidRequest(f"a node takes at most {standing.MAX_ITEMS} standing approvals in each list")
        return ok(await self.c.hub.push_standing(org, request.path_params["node_id"], whole=parts.get("set"),
                                                 upsert=parts.get("upsert"), revoke=parts.get("revoke")))

    async def api_freeze(self, request: Request, org: str) -> Response:
        """GET: the mesh freeze and which nodes have it. POST {reason}: freeze every node (agentos.freeze)."""
        if request.method == "GET":
            return ok(self.c.mesh_status(org))
        data = await body_json(request)
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidRequest("send {\"reason\": \"...\"}")
        return ok(await self.c.mesh_freeze(org, reason=reason, by="owner"))

    async def api_unfreeze(self, request: Request, org: str) -> Response:
        """Lift the mesh freeze: the owner only (a panel session, or the owner token in controller.toml). The
        agent token never reaches this API."""
        return ok(await self.c.mesh_unfreeze(org, by="owner"))

    async def api_node_config(self, request: Request, org: str) -> Response:
        data = await body_json(request)
        config = data.get("config")
        if not isinstance(config, dict):
            raise InvalidRequest("send {\"config\": {...}}")
        await self.c.hub.config_update(org, request.path_params["node_id"], config)
        return ok({"sent": True})

    # ---- pages ----
    def _page(self, name: str) -> Response:
        return FileResponse(STATIC / name, headers=SECURITY_HEADERS, media_type="text/html")

    async def index(self, request: Request) -> Response:
        return self._page("index.html")

    async def login(self, request: Request) -> Response:
        return self._page("login.html")

    async def view_page(self, request: Request) -> Response:
        if self.owner(request) is None:
            return RedirectResponse("/login", status_code=303)
        try:
            _lane(request.path_params)
        except AgentosError:
            return error(404, "not_found", "no such lane")
        return self._page("view.html")

    # ---- live view and terminal ----
    async def _open(self, websocket: WebSocket, kind: str, params: dict) -> tuple[Any, str, str] | None:
        org = self.owner(websocket)
        if org is None or not same_origin(websocket.headers):
            await websocket.close(code=1008)
            return None
        try:
            lane = _lane(websocket.path_params)
        except AgentosError:
            await websocket.close(code=1008)
            return None
        offered = websocket.scope.get("subprotocols") or []
        await websocket.accept(subprotocol="binary" if "binary" in offered else None)
        node_id = websocket.path_params["node_id"]
        try:
            pipe = await self.c.hub.open_stream(org, node_id, kind, lane, params)
        except AgentosError as exc:
            await websocket.close(code=4404 if exc.code in ("not_found", "lane_not_found") else 4503,
                                  reason=exc.message[:100])
            return None
        return pipe, org, node_id

    async def _pump(self, websocket: WebSocket, pipe: Any, org: str, node_id: str, *, text: bool) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        async def down() -> None:
            while True:
                data = await pipe.read()
                if data is None:
                    return
                if text:
                    chunk = decoder.decode(data)
                    if chunk:
                        await websocket.send_text(chunk)
                else:
                    await websocket.send_bytes(data)
                await pipe.consumed(len(data))

        async def up() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if data is None and message.get("text") is not None:
                    raw = message["text"]
                    if text and raw.startswith('{"type"'):
                        with contextlib.suppress(ValueError, AgentosError):
                            obj = json.loads(raw)
                            if obj.get("type") == "resize":
                                await self.c.hub.send_resize(org, node_id, pipe.id, int(obj.get("cols", 80)),
                                                             int(obj.get("rows", 24)))
                                continue
                    data = raw.encode() if text else None
                if data:
                    await pipe.write(data)

        tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, StreamClosed):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pipe.close("closed")
            session = self.c.hub.sessions.get(node_id)
            if session is not None:
                session.streams.drop(pipe.id)
            with contextlib.suppress(Exception):
                await websocket.close(code=1000)

    async def view_ws(self, websocket: WebSocket) -> None:
        mode = websocket.query_params.get("mode", "view")
        opened = await self._open(websocket, "vnc", {"mode": "control" if mode == "control" else "view"})
        if opened is not None:
            await self._pump(websocket, *opened, text=False)

    async def terminal_ws(self, websocket: WebSocket) -> None:
        q = websocket.query_params
        params: dict = {}
        try:
            params["cols"], params["rows"] = int(q.get("cols", 120)), int(q.get("rows", 32))
        except ValueError:
            await websocket.close(code=1008)
            return
        if q.get("session"):
            params["session"] = q["session"]
        params["mode"] = "control" if q.get("mode") == "control" else "view"
        params["by"] = "owner"  # who the node checks holds the lane (lane_control sends the same)
        opened = await self._open(websocket, "terminal", params)
        if opened is not None:
            await self._pump(websocket, *opened, text=True)

    async def hook(self, request: Request) -> Response:
        """A webhook for a node. No session: the sender's signature is the proof, and only the node
        holds the secret, so this forwards the bytes and keeps nothing. What may pass is decided
        before the body is read (hooks.py), and nothing queues here: over a limit is refused at once."""
        node_id, token = request.path_params["node_id"], request.path_params["token"]
        wait = self.hooks.source(_peer(request))
        if wait is not None:
            return _slow_down("Too many requests from this address.", wait)
        if not TOKEN_RE.match(token):
            return error(404, "not_found", "unknown hook")
        if node_id not in self.c.hub.sessions:
            return _slow_down("That computer is offline. Try again with a fresh timestamp.", 30, status=503, code="unavailable")
        wait = self.hooks.target(node_id, token)
        if wait is not None:
            return _slow_down("Too many requests for this webhook.", wait)
        if not self.hooks.enter(node_id):
            return _slow_down("That computer is busy with other webhooks.", 1)
        try:
            body = await self._hook_body(request)
            if isinstance(body, Response):
                return body
            headers = {k.lower(): v for k, v in request.headers.items()}
            status, payload = await self.c.hub.relay_hook(node_id, token, body, headers)
        finally:
            self.hooks.leave(node_id)
        retry = payload.pop("retry_after", None) if isinstance(payload, dict) else None
        extra = {"Retry-After": str(max(1, math.ceil(retry)))} if isinstance(retry, int | float) else {}
        return JSONResponse(payload, status_code=status, headers={**SECURITY_HEADERS, **extra})

    async def _hook_body(self, request: Request) -> bytes | Response:
        """The body, or the refusal: read in pieces against the cap, and for a bounded time."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_HOOK_BODY:
            return error(413, "invalid", "body too large")
        chunks: list[bytes] = []
        size = 0
        try:
            async with asyncio.timeout(self.hook_body_timeout):
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_HOOK_BODY:
                        return error(413, "invalid", "body too large")
                    chunks.append(chunk)
        except TimeoutError:
            return error(408, "invalid", "the body took too long to arrive")
        except ClientDisconnect:
            return error(400, "invalid", "the sender went away")
        return b"".join(chunks)

    async def unsubscribe(self, request: Request) -> Response:
        """An unsubscribe link. A GET only offers the button, because mail scanners follow links;
        a POST (the button, or a mail client's one click) does it. Same page whatever the token."""
        if request.method == "GET":
            return HTMLResponse(UNSUBSCRIBE_PAGE.format(body=UNSUBSCRIBE_ASK), headers=SECURITY_HEADERS)
        node_id = request.path_params["node_id"]
        wait = self.hooks.source(_peer(request))
        if wait is not None:
            return _slow_down("Too many requests from this address.", wait)
        if self.hooks.enter(node_id):
            try:
                await self.c.hub.relay_unsubscribe(node_id, request.path_params["token"])
            finally:
                self.hooks.leave(node_id)
        else:
            return _slow_down("Please try again in a moment.", 1)
        return HTMLResponse(UNSUBSCRIBE_PAGE.format(body=UNSUBSCRIBE_DONE), headers=SECURITY_HEADERS)

    def routes(self) -> list:
        def api(path: str, name: str, methods: list[str]) -> Route:
            async def endpoint(request: Request) -> Response:
                return await self.api(request, name)

            return Route(path, endpoint, methods=methods, name=name)

        lane = "/api/nodes/{node_id}/lanes/{workspace}/{name}"
        return [
            Route("/", self.index, methods=["GET"]),
            Route("/login", self.login, methods=["GET"]),
            Route("/nodes/{node_id}/lanes/{workspace}/{name}/view", self.view_page, methods=["GET"]),
            Route("/api/session", self.session, methods=["GET", "POST", "DELETE"]),
            api("/api/status", "status", ["GET"]),
            api("/api/nodes", "nodes", ["GET"]),
            api("/api/nodes/{node_id}", "node", ["DELETE"]),
            api("/api/nodes/{node_id}/config", "node_config", ["POST"]),
            api(f"{lane}/control", "lane_control", ["POST"]),
            api("/api/nodes/{node_id}/standing", "standing", ["POST"]),
            api("/api/freeze", "freeze", ["GET", "POST"]),
            api("/api/unfreeze", "unfreeze", ["POST"]),
            api("/api/join-tokens", "join_tokens", ["GET", "POST"]),
            api("/api/join-tokens/{token_id}", "join_token", ["DELETE"]),
            api("/api/approvals", "approvals", ["GET"]),
            api("/api/approvals/{node_id}/{approval_id}", "decide", ["POST"]),
            api("/api/activity", "activity", ["GET"]),
            api("/api/grants", "grants", ["GET", "POST"]),
            api("/api/grants/{grant_id}", "grant", ["DELETE"]),
            Route("/hooks/{node_id}/{token}", self.hook, methods=["POST"]),
            Route("/u/{node_id}/{token}", self.unsubscribe, methods=["GET", "POST"]),
            WebSocketRoute("/v1/view/{node_id}/{workspace}/{name}", self.view_ws),
            WebSocketRoute("/v1/terminal/{node_id}/{workspace}/{name}", self.terminal_ws),
            Mount("/assets/novnc", app=StaticFiles(directory=NOVNC, check_dir=False), name="novnc"),
            Mount("/assets", app=StaticFiles(directory=STATIC), name="assets"),
        ]


def _lane(params: dict) -> str:
    lane = f"{params.get('workspace')}/{params.get('name')}"
    try:
        parse_lane_id(lane)
    except ValueError:
        raise InvalidRequest("not a lane id") from None
    return lane
