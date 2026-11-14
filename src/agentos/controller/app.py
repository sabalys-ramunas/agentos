"""The open-source controller: composition, the ASGI app, and serving it.

    controller = Controller(config)          # store, keys, grants, hub, panel sessions
    app = build_app(controller)              # TrustedHosts -> /mcp (agents) | everything else (nodes, owner)
    await serve_controller(config)           # uvicorn, until `stop` is set

The Host check is the node's (agentos.mcp.http.TrustedHosts): localhost on any port, plus
the host of `public_url` and `extra_allowed_hosts`, so a DNS rebinding page cannot reach it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from agentos.api.auth import PanelStore
from agentos.controller.config import ControllerConfig, check_bind
from agentos.controller.connect import NodeEndpoint
from agentos.controller.grants import AgentGrants, Keys
from agentos.controller.hub import Hub
from agentos.controller.mcp import AgentAuth, ControllerMcp
from agentos.controller.store import JOIN_TOKEN_TTL, ControllerStore
from agentos.controller.web import Web
from agentos.core.errors import InvalidRequest, NotFound
from agentos.mcp.http import LOCAL_HOSTS, TrustedHosts, sdk_transport_security
from agentos.node.channel import wire

log = logging.getLogger("agentos.controller")

OWNER_AGENT = "owner-agent"
SHUTDOWN_GRACE = 5.0


class Controller:
    def __init__(self, config: ControllerConfig, *, clock: Callable[[], float] = time.time):
        self.config = config
        config.home.mkdir(parents=True, exist_ok=True)
        self.store = ControllerStore(config.db_path, clock=clock)
        self.keys = Keys(config.secrets_dir)
        self.grants = AgentGrants(self.store, self.keys, clock=clock)
        self.hub = Hub(self.store, self.keys, clock=clock)
        self.org = self.store.default_org()
        self.panel = PanelStore(config.home / "panel.db")
        self.ensure_agent_token()

    def ensure_agent_token(self) -> None:
        """The token in controller.toml (the one `agentos connect` reads) has a grant behind it."""
        token = self.config.agent_token
        if token and self.store.grant_by_token(token) is None:
            self.grants.create(self.org["id"], subject=OWNER_AGENT, note="the owner's agents", token=token)

    def node_config(self, node: dict) -> dict:
        return {"heartbeat_seconds": int(wire.HEARTBEAT_INTERVAL)}

    def create_join_token(self, org_id: str, *, name: Any = None, capabilities: Any = None,
                          ttl_minutes: Any = None) -> tuple[dict, str]:
        from agentos.controller.routing import NODE_NAME_RE

        if name is not None and (not isinstance(name, str) or not NODE_NAME_RE.match(name)):
            raise InvalidRequest("a node name is lowercase letters, digits and dashes (at most 31)")
        if capabilities is not None and (not isinstance(capabilities, list)
                                         or any(c not in wire.CAPABILITIES for c in capabilities)):
            raise InvalidRequest(f"capabilities are some of {', '.join(wire.CAPABILITIES)}")
        ttl = JOIN_TOKEN_TTL
        if ttl_minutes is not None:
            if not isinstance(ttl_minutes, int) or isinstance(ttl_minutes, bool) or not 1 <= ttl_minutes <= 60:
                raise InvalidRequest("a join token lasts between 1 and 60 minutes")
            ttl = ttl_minutes * 60
        return self.store.create_join_token(org_id, name=name, capabilities=capabilities, ttl=ttl)

    def join_command(self, token: str) -> str:
        # this controller is the owner's own, so the node gives it the full ceiling (agentos.policy.ceiling)
        return f"agentos node join --self-hosted {self.config.node_url} {token}"

    async def revoke_grant(self, org_id: str, grant_id: str) -> list[str]:
        ids = self.grants.revoke(org_id, grant_id)
        if ids:
            await self.hub.push_revoke(org_id, ids)
        return ids

    # ---- mesh freeze (agentos.freeze): every node at once, pending for the offline ones ----
    async def mesh_freeze(self, org_id: str, *, reason: str, by: str) -> dict:
        """Freeze every node of the org: online nodes now (concurrently), offline ones in their next
        welcome, before anything else runs there. Then every agent grant of the org is revoked (the
        owner's agent token too: agents must be granted again). Idempotent."""
        text = " ".join(str(reason or "").split())[:300] or "mesh freeze"
        current = self.store.mesh_freeze(org_id)
        state = current or await self.store.run(self.store.set_mesh_freeze, org_id,  # durable before the push
                                                freeze_id="fz_" + secrets.token_hex(6), reason=text,
                                                by=str(by)[:80], revoked=[])
        sent = await self.hub.push_mesh_freeze(org_id, state)
        revoked: list[str] = []
        grants: list[str] = []
        for g in self.store.grants(org_id):
            if g.get("revoked_at") is None and g.get("parent_id") is None:
                with contextlib.suppress(NotFound):
                    revoked += self.grants.revoke(org_id, g["id"])
                    grants.append(g["id"])
        if revoked:
            await self.hub.push_revoke(org_id, revoked)
        return {**self.mesh_status(org_id), "already_frozen": current is not None, "sent": sent,
                "revoked_grants": grants}

    async def mesh_unfreeze(self, org_id: str, *, by: str) -> dict:
        """Lift the mesh freeze on every node that has it from this controller (never a node's own
        freeze). Restores no grant."""
        was = await self.store.run(self.store.clear_mesh_freeze, org_id)
        sent = await self.hub.push_mesh_freeze(org_id, None) if was else {}
        return {"unfrozen": was, "sent": sent, "by": by,
                "next": "Nothing was restored: grant agents again (the owner's agent token needs `agentos controller "
                        "agent-token --new`)."}

    def mesh_status(self, org_id: str) -> dict:
        state = self.store.mesh_freeze(org_id)
        if state is None:
            return {"frozen": False}
        applied = self.store.mesh_freeze_nodes(org_id, state["freeze_id"])
        nodes = [{"id": n["id"], "name": n["name"], "applied": n["id"] in applied,
                  "online": n["id"] in self.hub.sessions} for n in self.store.nodes(org_id)]
        return {"frozen": True, "freeze_id": state["freeze_id"], "reason": state["reason"], "by": state["by"],
                "at": wire.iso(state["at"]), "nodes": nodes,
                "pending": [n["name"] for n in nodes if not n["applied"]]}

    def login_link(self, ttl_seconds: int = 600) -> tuple[str, float]:
        from agentos.api.auth import new_login_code

        code = new_login_code()
        expires = self.panel.add_code(code, ttl_seconds)
        return f"{self.config.local_url}/login#code={code}", expires

    def close(self) -> None:
        self.store.close()


def allowed_hosts(config: ControllerConfig) -> list[str]:
    hosts = [*LOCAL_HOSTS, *config.extra_allowed_hosts]
    if config.public_url:
        host = urlsplit(wire.connect_url(config.public_url)).hostname
        if host and host not in hosts:
            hosts.append(host)
    return hosts


class Router:
    """Pure ASGI: /mcp (and the lifespan) to the agents' MCP app, everything else to the web app."""

    def __init__(self, mcp: ASGIApp, web: ASGIApp):
        self.mcp = mcp
        self.web = web

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.mcp(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/mcp" or path.startswith("/mcp/"):
            await self.mcp(scope, receive, send)
        else:
            await self.web(scope, receive, send)


async def _not_found(request: Any, exc: Exception) -> Any:
    """An unknown route: the shared error body, as every other answer here (QA-2 #17)."""
    from agentos.controller.web import error

    return error(404, "not_found", f"There is no {request.url.path} here.")


async def _not_allowed(request: Any, exc: Exception) -> Any:
    from agentos.controller.web import error

    allow = (getattr(exc, "headers", None) or {}).get("Allow")
    response = error(405, "method_not_allowed", f"{request.url.path} does not take {request.method}"
                     + (f"; it takes {allow}." if allow else "."))
    if allow:
        response.headers["Allow"] = allow
    return response


def build_app(controller: Controller) -> ASGIApp:
    hosts = allowed_hosts(controller.config)
    mcp = ControllerMcp(controller)
    controller.mcp = mcp
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", host=controller.config.host,
                                      transport_security=sdk_transport_security(hosts))
    web = Web(controller)
    from starlette.routing import WebSocketRoute

    web_app = Starlette(routes=[WebSocketRoute(wire.CONNECT_PATH, NodeEndpoint(controller).handle), *web.routes()],
                        exception_handlers={404: _not_found, 405: _not_allowed})
    controller.web = web
    return TrustedHosts(Router(AgentAuth(mcp_app, controller.grants), web_app), hosts)


def _server(app: ASGIApp, config: ControllerConfig) -> Any:
    import uvicorn

    class Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):  # the caller owns the signals (agentos up runs a node too)
            yield

    options: dict[str, Any] = {}
    if config.tls:
        options = {"ssl_certfile": config.tls_certfile, "ssl_keyfile": config.tls_keyfile}
    return Server(uvicorn.Config(app, host=config.host, port=config.port, log_level="info", lifespan="on",
                                 timeout_graceful_shutdown=SHUTDOWN_GRACE, access_log=False,
                                 log_config=None,  # uvicorn's lines go to the root logger: ids, JSON and scrubbing
                                 ws_max_size=wire.HARD_FRAME_BYTES, ws_ping_interval=wire.PING_INTERVAL,
                                 ws_ping_timeout=wire.DEAD_AFTER, proxy_headers=config.behind_tls_proxy,
                                 **options))


def bound_port(server: Any) -> int | None:
    for srv in getattr(server, "servers", []) or []:
        for sock in srv.sockets:
            return sock.getsockname()[1]
    return None


async def serve_controller(config: ControllerConfig, *, stop: asyncio.Event | None = None,
                           on_started: Callable[[int, Controller], None] | None = None,
                           controller: Controller | None = None) -> None:
    """Serve until `stop` is set. `on_started(port, controller)` once listening (port 0 picks one)."""
    check_bind(config)
    controller = controller or Controller(config)
    app = build_app(controller)
    server = _server(app, config)
    stop = stop or asyncio.Event()

    async def watch() -> None:
        await stop.wait()
        server.should_exit = True

    async def announce() -> None:
        while not server.started:
            await asyncio.sleep(0.05)
        port = bound_port(server) or config.port
        log.info("controller on %s (agents: /mcp, nodes: %s)", config.local_url, wire.CONNECT_PATH)
        if on_started is not None:
            on_started(port, controller)

    tasks = [asyncio.create_task(watch()), asyncio.create_task(announce())]
    try:
        if not stop.is_set():
            await server.serve()
    finally:
        for task in tasks:
            task.cancel()
        for session in list(controller.hub.sessions.values()):
            with contextlib.suppress(Exception):
                await session.ws.close(code=1001)
        controller.close()
