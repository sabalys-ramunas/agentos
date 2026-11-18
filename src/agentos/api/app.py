"""`build_api(runtime) -> Starlette`: the node's HTTP API (docs/API.md) plus the built web app."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentos.api.access import Access, MemberAccess, PolicyAccess, Presence
from agentos.api.auth import (
    CSRF_HEADER,
    NODE,
    PRESENCE_HEADER,
    SESSION_COOKIE,
    PanelStore,
    Session,
    origin_allowed,
    tokens_equal,
)
from agentos.api.bridge import Bridge
from agentos.api.errors import ApiError, error_response
from agentos.api.events import EventBus
from agentos.api.jsonable import jsonable
from agentos.api.members import Members, MemberStore
from agentos.core import live as live_sockets
from agentos.core import webapp
from agentos.core.aio import maybe_await
from agentos.core.errors import AgentosError, InvalidRequest, NotFound, ServiceUnavailable
from agentos.core.logs import log_context
from agentos.core.logs import update as update_log_context
from agentos.core.models import Principal
from agentos.core.ratelimit import RateLimiter
from agentos.core.zone import node_zone
from agentos.policy.confirm import ConfirmLevel, Proof, legacy_requires
from agentos.policy.errors import ProofTooWeak

log = logging.getLogger("agentos.api")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
MAX_BODY = 1_000_000


@dataclass
class ApiSettings:
    owner_token: str | None = None        # default: the node's mcp_token from config
    home: Path | None = None              # default: config.home; panel.db lives here
    store: PanelStore | None = None       # tests pass an in-memory store
    panel_dir: Path | None = None         # default: agentos.core.webapp.panel_dir (env, checkout, installed)
    mcp_url: str | None = None            # shown in the first-run wizard
    session_ttl: int = 7 * 24 * 3600
    device_ttl: int = 90 * 24 * 3600      # the owner's device credential (`agentos connect` on the Mac)
    device_max_ttl: int = 366 * 24 * 3600
    login_per_ip: int = 10
    login_global: int = 60
    login_window: float = 300.0
    sse_heartbeat: float = 15.0
    sse_max_seconds: float = 1800.0       # clients reconnect with Last-Event-ID
    allowed_origins: tuple[str, ...] = field(default_factory=tuple)  # extra origins for dev servers
    secure_cookies: bool | None = None    # None: Secure when the request came over https
    lane_pump_seconds: float = 2.0        # how often lane state is polled for live updates (0: off)
    mount_features: bool = True           # the feature packages' extra routes (agentos.api.mounts)
    schedule_pump_seconds: float = 5.0    # how often schedule changes the scheduler makes are published (0: off)
    brain_pump_seconds: float = 5.0       # how often brain task changes are published (0: off); brains every 30 s
    timezone: str | None = None           # IANA name shown to the panel; default: the host's zone (agentos.core.zone)
    public_base_url: str | None = None    # where people open the panel (invite links, the passkey RP id);
                                          # default: the notifier's public_base_url, else localhost
    invite_ttl: int = 24 * 3600           # an invite link works once, for this long
    step_up_window: int = 15 * 60         # a member's decisions need a passkey check this recent
    step_up_fresh: int = 120              # pay and irreversible: a check for that approval, this recent
    live_recheck_seconds: float = 5.0     # how often open streams re-check access (agentos.core.live)


@dataclass(frozen=True)
class Auth:
    principal: Principal
    via: str                     # "bearer" | "cookie"
    session: Session | None = None
    sid: str | None = None
    access: Access = field(default_factory=Access)  # what this caller may do (agentos.api.access); AGENT unless set


class Api:
    """Everything a route needs: the runtime, settings, store, bus and authentication."""

    def __init__(self, runtime: Any, settings: ApiSettings):
        self.runtime = runtime
        self.settings = settings
        self.timezone: str = node_zone(settings.timezone)  # IANA; a bad setting fails here, not per request
        self.bridge = Bridge(runtime, timezone=self.timezone)
        bus = runtime.get("events")
        if bus is None:
            bus = EventBus()
            runtime.register("events", bus)
        self.bus: EventBus = bus
        cfg = runtime.get("config")
        self.config = cfg
        self.node_name: str = getattr(cfg, "name", None) or "node-1"
        self.store = settings.store or PanelStore.for_home(settings.home or getattr(cfg, "home", None))
        self.members = Members(MemberStore(self.store), runtime)
        approvals = runtime.get("approvals")
        if approvals is not None and hasattr(approvals, "passkey_holder"):  # who a fresh check can be asked of
            approvals.passkey_holder = lambda person: bool(self.members.store.credentials(person))
        self.owner_token = settings.owner_token or getattr(cfg, "mcp_token", None) or None
        self.limiter = RateLimiter(per_key=settings.login_per_ip, global_max=settings.login_global,
                                   window=settings.login_window)
        self._panel_dir = settings.panel_dir
        self._home = settings.home or getattr(cfg, "home", None)
        if settings.mcp_url:
            self.mcp_url = settings.mcp_url
        elif cfg is not None:
            self.mcp_url = f"http://localhost:{cfg.mcp_port}/mcp"
        else:
            self.mcp_url = None

    @property
    def panel_dir(self) -> Path:
        """The built web app to serve (agentos.core.webapp.panel_dir), looked up on each request, so an
        app `agentos web install` adds or `remove` takes away shows without a restart."""
        return self._panel_dir or webapp.panel_dir(self._home)

    # ---- services (real ones through agentos.api.bridge) ----
    def svc(self, name: str) -> Any:
        svc = self.optional(name)
        if svc is None:
            raise ServiceUnavailable(f"{name} is not configured on this node.")
        return svc

    def optional(self, name: str) -> Any | None:
        svc = self.runtime.get(name)
        return None if svc is None else self.bridge.adapt(name, svc)

    def publish(self, kind: str, payload: Any) -> None:
        try:
            self.bus.publish(kind, payload)
        except Exception as exc:  # noqa: BLE001 - publishing must never fail the request
            log.warning("event publish failed: %s", type(exc).__name__)

    # ---- auth ----
    async def authenticate(self, conn: HTTPConnection, *, touch: bool = True) -> Auth | None:
        """Bearer token or session cookie -> Auth, else None (401). With a policy service the
        token is resolved there (the owner token or a grant) and scoped to its grant. `touch=False`:
        a live socket's recheck (agentos.core.live), which is not activity for the idle limit."""
        header = conn.headers.get("authorization", "")
        policy = self.runtime.get("policy")
        if header:
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                return None
            token = token.strip()
            principal = None
            node_token = tokens_equal(token, self.owner_token)
            if node_token:  # the node's own token: the owner's authority, its own kind (never a person)
                principal = NODE
            elif policy is not None and hasattr(policy, "authenticate_panel"):
                principal = await maybe_await(policy.authenticate_panel(token))
            if principal is None:
                return None
            access = self._token_access(policy, token, self._bearer_presence(conn, node_token))
            return None if access is None else Auth(principal=principal, via="bearer", access=access)
        sid = conn.cookies.get(SESSION_COOKIE)
        if sid:
            session = self.store.get_session(sid, touch=touch)
            if session is not None and session.is_member:
                return self._member_auth(policy, session, sid)
            if session is not None:
                # the owner: a one-time code from `agentos panel login`, the owner's passkey, the node's
                # CLI, or the device credential `agentos connect` keeps on the owner's computer
                principal = Principal(kind="human", id="owner", owner="owner", grant_id="owner")
                presence = self.session_presence(session)
                if presence < Presence.PERSON and self.owner_token:
                    # the device credential: routed exactly as the node token that made it, never as the
                    # owner in person (whose context skips pacing, taint and the trifecta)
                    access = self._token_access(policy, self.owner_token, presence)
                else:
                    access = self._session_access(policy, presence)
                if access is not None:
                    return Auth(principal=principal, via="cookie", session=session, sid=sid, access=access)
        return None

    def refusal(self, conn: HTTPConnection) -> tuple[str, str] | None:
        """(code, message) when the bearer token is refused for a reason the caller must be told (a
        grant a freeze revoked: `frozen`), else None (a plain unauthorized)."""
        scheme, _, token = conn.headers.get("authorization", "").partition(" ")
        policy = self.runtime.get("policy")
        explain = getattr(policy, "refusal", None)
        if scheme.lower() != "bearer" or not token.strip() or not callable(explain):
            return None
        try:
            told = explain(token.strip())
        except Exception:  # noqa: BLE001 - the plain refusal stands
            return None
        return told if isinstance(told, tuple) and len(told) == 2 else None

    def _bearer_presence(self, conn: HTTPConnection, node_token: bool) -> Presence:
        """A grant is an agent's; the node token is NODE_TOKEN, or a person for this one request when
        the node's CLI sends a one-time code it wrote into panel.db beside it (PRESENCE_HEADER), which
        only the node's operator can write. The code is spent here."""
        if not node_token:
            return Presence.AGENT
        code = (conn.headers.get(PRESENCE_HEADER) or "").strip()
        return Presence.PERSON if code and self.store.use_code(code) else Presence.NODE_TOKEN

    def session_presence(self, session: Session) -> Presence:
        """An owner session's presence. The device credential lives in a login Keychain an agent on that
        computer can read, so it is DEVICE however it was made; any other session is a person, VERIFIED
        while a passkey check is inside the step-up window."""
        if session.via == "device":
            return Presence.DEVICE
        verified = session.last_verified_at
        if verified is not None and time.time() - verified <= self.settings.step_up_window:
            return Presence.VERIFIED
        return Presence.PERSON

    def _member_auth(self, policy: Any, session: Session, sid: str) -> Auth | None:
        """A member's session: re-read the member, their roles and their live grants on every request,
        so a removal, a suspension or a role change takes effect at the next one."""
        store = self.members.store
        member = store.member(session.member_id or "")
        if member is None or member.state != "active" or not self.members.enabled:
            self.store.delete_session(sid)
            return None
        try:
            grants = self.members.sync(member.id, by="owner")  # an ended role, or a grant close to its end
            if not grants:
                return None
            recent = session.last_verified_at is not None and \
                time.time() - session.last_verified_at <= self.settings.step_up_window
            access = MemberAccess(policy, member, store.roles(member.id), grants, via=session.via,
                                  verified_recently=recent)
        except AgentosError:
            return None
        principal = access.principal_for(Principal(kind="human", id=member.id, owner=member.id))
        return Auth(principal=principal, via="cookie", session=session, sid=sid, access=access)

    @staticmethod
    def _token_access(policy: Any, token: str, presence: Presence = Presence.AGENT) -> Access | None:
        make = getattr(policy, "context_for_token", None) if policy is not None else None
        if not callable(make):
            return Access(presence=presence)  # a policy that vouches without scopes (fakes): the owner
        try:
            return PolicyAccess(make(token, tool="panel"), presence=presence, bearer=True)
        except AgentosError:
            return None  # revoked or expired since authenticate_panel: unauthenticated

    @staticmethod
    def _session_access(policy: Any, presence: Presence = Presence.PERSON) -> Access | None:
        """The owner's session. Below PERSON (the device credential) its actions are routed like a
        token's, Ask me first and approvals included, so it can start what a person then approves."""
        make = getattr(policy, "owner_context", None) if policy is not None else None
        if not callable(make):
            return Access(presence=presence)
        try:
            return PolicyAccess(make(tool="panel"), presence=presence)
        except AgentosError:
            return None

    def still_signed_in(self, auth: Auth) -> bool:
        """For long requests (the event stream): is a member's session still live and the member
        still active? The owner and tokens are checked when they connect."""
        if auth.access.member is None or not auth.sid:
            return True
        session = self.store.get_session(auth.sid)
        member = self.members.store.member(auth.access.member.id)
        return session is not None and member is not None and member.state == "active"

    def origin_ok(self, conn: HTTPConnection) -> bool:
        return origin_allowed(conn.headers.get("origin"), conn.headers.get("host"), self.settings.allowed_origins)

    def secure_cookie(self, request: Request) -> bool:
        """Secure when the request came over https. `X-Forwarded-Proto` is a proxy's word, so it counts
        only from a proxy on this machine (loopback); any other client could send it."""
        if self.settings.secure_cookies is not None:
            return self.settings.secure_cookies
        if request.url.scheme == "https":
            return True
        peer = request.client.host if request.client else ""
        return peer in LOOPBACK and request.headers.get("x-forwarded-proto") == "https"


Handler = Callable[[Request], Awaitable[Any]]


def endpoint(api: Api, fn: Handler, *, public: bool = False, owner: bool = False, cap: str | None = None,
             action: str | None = None, presence: Presence | None = None,
             step_up: bool = False) -> Callable[[Request], Awaitable[Response]]:
    """Wrap a route: authenticate, enforce CSRF on cookie writes, refuse non-owners on owner-only
    routes (403), map errors, serialize. `cap` (agentos.api.members.CAPABILITIES): the owner, or a
    member whose role has it in some workspace (the handler checks the workspace); a grant is
    refused as on an owner-only route. `presence`: the least the request must be (Presence;
    `presence_required` otherwise), `step_up` a member's passkey check on top (docs/THREAT-MODEL.md)."""
    name = action or fn.__name__.strip("_").replace("_", ".")

    async def handler(request: Request) -> Response:
        with log_context(**_path_ids(request)):
            return await _handle(request)

    async def _handle(request: Request) -> Response:
        try:
            if not public:
                auth = await api.authenticate(request)
                if auth is None:
                    if (told := api.refusal(request)) is not None:
                        raise ApiError(*told)
                    raise ApiError("unauthorized", "Sign in to the panel first. Run `agentos panel login` "
                                                   "on the node for a sign-in link.")
                if request.method not in SAFE_METHODS and auth.via == "cookie":
                    if not api.origin_ok(request):
                        raise ApiError("csrf_failed", "This request came from another site.")
                    if not tokens_equal(request.headers.get(CSRF_HEADER), auth.session.csrf if auth.session else None):
                        raise ApiError("csrf_failed", "Missing or wrong X-Agentos-CSRF header. Reload the panel.")
                request.state.auth = auth
                update_log_context(grant=auth.access.grant_id)
                request.state.principal = auth.principal  # what the feature packages' routes read
                if presence is not None:
                    # presence before role: an agent's grant learns that a person decides this in the
                    # panel (presence_required), not that a stronger credential would (QA-2 #9)
                    auth.access.require_presence(presence, name)
                if owner:
                    auth.access.require_owner(name)
                elif cap is not None:
                    auth.access.require_role(cap, action=name)
                if presence is not None and step_up:
                    auth.access.require_presence(presence, name, step_up=step_up)
                auth.access.defer_done = True  # an allowed action is traced with how it ended
            result = await fn(request)
        except Exception as exc:  # noqa: BLE001 - every failure becomes the error envelope
            _settle(request, exc)
            return error_response(exc, member=_is_member(request))
        _settle(request, None)
        if isinstance(result, Response):
            return result
        return JSONResponse(jsonable(result))

    return handler


def _settle(request: Request, error: BaseException | None) -> None:
    auth = getattr(request.state, "auth", None)
    settle = getattr(getattr(auth, "access", None), "settle", None)
    if callable(settle):
        settle(error)


def _is_member(request: Request) -> bool:
    """Whether this request is a member's session: what they read is worded for a member (no commands)."""
    auth = getattr(request.state, "auth", None)
    return getattr(getattr(auth, "access", None), "member", None) is not None


def _path_ids(request: Request) -> dict[str, str | None]:
    """The lane and workspace a route names in its path ({ws}/{name}, {lane}, {workspace})."""
    p = request.path_params
    ws, name = p.get("ws"), p.get("name")
    lane = f"{ws}/{name}" if ws and name else p.get("lane")
    return {"lane": lane if isinstance(lane, str) else None, "workspace": ws or p.get("workspace")}


def decision_proof(api: Api, request: Request, approval: dict, decision: str) -> Proof | None:
    """How strong a decision made here is, worked out from the session it arrived on and nothing the
    caller sent (docs/CONFIRMATIONS.md). None for a bearer token: the approvals store words it, and a
    token proves no person.

    The windows are today's step-up: a member's session reaches `signed_in` only with a passkey check
    within `step_up_window` (the owner's session always does), so a member needs that for any approve
    or answer, however little the approval requires. `present` is a fresh check made for this very
    approval (`POST /api/session/verify` with its id) within `step_up_fresh`, used once. Denying never
    needs one."""
    auth = request.state.auth
    session = auth.session
    if session is None:
        # the node's CLI: the node token with a one-time code from panel.db is a person for this one
        # request (docs/THREAT-MODEL.md), and decides as `agentos approvals approve` on the node does
        if auth.access.presence >= Presence.PERSON:
            return Proof("cli", ConfirmLevel.SIGNED_IN, time.time())
        return None
    now = time.time()
    member = auth.access.member is not None
    verified = session.last_verified_at
    recent = not member or (verified is not None and now - verified <= api.settings.step_up_window)
    if decision != "approve":
        return Proof("panel", ConfirmLevel.SIGNED_IN if recent else None, now)
    approval_id = str(approval.get("id") or "")
    if not recent:
        auth.access.trace("approval.step_up", status="denied", lane=approval.get("lane"),
                          summary="the last passkey check is too old", approval=approval_id)
        raise ApiError("step_up_required", "Confirm with your passkey first: your last check was more than "
                                           f"{api.settings.step_up_window // 60} minutes ago.")
    word = approval.get("requires")
    is_action = approval.get("kind", "action") == "action"
    need = ConfirmLevel.of(word) if word else legacy_requires(approval.get("effect") if is_action else None)
    if need <= ConfirmLevel.SIGNED_IN:
        return Proof("panel", ConfirmLevel.SIGNED_IN, now)
    if need > ConfirmLevel.PRESENT:  # no check here reaches it, so none is spent on it
        raise ProofTooWeak(need.word, f"This needs {need.word}, which the panel cannot give.", approval_id)
    if api.store.use_step_up(session, approval_id, max_age=api.settings.step_up_fresh):
        return Proof("passkey", ConfirmLevel.PRESENT, now)
    auth.access.trace("approval.step_up", status="denied", lane=approval.get("lane"),
                      summary=f"{approval.get('effect')} needs a fresh passkey check", approval=approval_id)
    raise ProofTooWeak(need.word, f"Approving {approval.get('effect')} needs a fresh passkey check for this "
                                  "approval. Confirm with your passkey and try again.", approval_id)


def principal_of(request: Request) -> Principal:
    return request.state.auth.principal


def access_of(request: Request) -> Access:
    return request.state.auth.access


def at_the_panel(request: Request) -> bool:
    """A person signed in to the panel, on a session cookie. A bearer token never is, the owner's token
    included: agents hold it (docs/API.md, "Access")."""
    return getattr(getattr(request.state, "auth", None), "via", None) == "cookie"
PERSON_ONLY = "Only the owner, signed in to the panel, can change this. A token cannot, and a member cannot."


def owner_in_person(request: Request) -> bool:
    """The owner at the panel with a session cookie. A bearer token never is, the node token
    included: agents use it. A member never is."""
    access = access_of(request)
    auth = getattr(request.state, "auth", None)
    return bool(access.owner and access.member is None and getattr(auth, "via", None) == "cookie")


async def scoped_list(fn: Callable[..., Any], viewer: Access | str | None, **kwargs: Any) -> Any:
    """Call an approvals list method, limited to what the caller sees: a grant family when `viewer`
    is a grant id (or a grant's Access), a member's workspaces through `viewer_grants` (the member
    grants whose roles see approvals). A service that cannot filter so gives a scoped caller nothing
    rather than everything."""
    if isinstance(viewer, Access):
        if viewer.member is not None:
            grants = viewer.grants_with("approvals")  # type: ignore[attr-defined]
            if not grants or not _takes(fn, "viewer_grants"):
                return []
            return await maybe_await(fn(viewer_grants=grants, **kwargs))
        viewer = viewer.viewer
    if viewer is None:
        return await maybe_await(fn(**kwargs))
    if not _takes(fn, "viewer"):
        return []
    return await maybe_await(fn(viewer=viewer, **kwargs))


async def scoped_get(fn: Callable[..., Any], viewer: Access | str | None, *args: Any) -> Any:
    if isinstance(viewer, Access):
        if viewer.member is not None:
            grants = viewer.grants_with("approvals")  # type: ignore[attr-defined]
            if not grants or not _takes(fn, "viewer_grants"):
                raise NotFound("No such item.")
            return await maybe_await(fn(*args, viewer_grants=grants))
        viewer = viewer.viewer
    if viewer is None:
        return await maybe_await(fn(*args))
    if not _takes(fn, "viewer"):
        raise NotFound("No such item.")
    return await maybe_await(fn(*args, viewer=viewer))


def _takes(fn: Callable[..., Any], name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


async def read_body[M: BaseModel](request: Request, model: type[M]) -> M:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise InvalidRequest("Request body is too large.")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise InvalidRequest("Request body must be JSON.") from None
    if not isinstance(data, dict):
        raise InvalidRequest("Request body must be a JSON object.")
    return model.model_validate(data)


async def read_json_object(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise InvalidRequest("Request body is too large.")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise InvalidRequest("Request body must be JSON.") from None
    if not isinstance(data, dict):
        raise InvalidRequest("Request body must be a JSON object.")
    return data


def query_int(request: Request, name: str, default: int, lo: int = 1, hi: int = 500) -> int:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise InvalidRequest(f"{name} must be a number.") from None
    if not lo <= value <= hi:
        raise InvalidRequest(f"{name} must be between {lo} and {hi}.")
    return value


def query_str(request: Request, name: str) -> str | None:
    value = request.query_params.get(name)
    return value or None


class SecurityHeaders:
    """Headers on every response; the API is never cached, the shell gets a strict CSP."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")

        async def send_wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
                if path.startswith("/api/"):
                    if b"cache-control" not in names:
                        extra.append((b"cache-control", b"no-store"))
                else:
                    ws = f" ws://{host} wss://{host}" if host else ""
                    csp = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                           "img-src 'self' data: blob:; font-src 'self' data:; "
                           f"connect-src 'self'{ws}; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                    extra.append((b"content-security-policy", csp.encode("latin-1")))
                message["headers"] = headers + [h for h in extra if h[0] not in names]
            await send(message)

        await self.app(scope, receive, send_wrapped)


# Route modules a feature owns in full that build on this module's helpers (endpoint, read_body):
# imported by name, as agentos.api.mounts finds the others, since importing them here is a circle.
FEATURE_ROUTES = ("agentos.runner.mail.api", "agentos.environments.api")


def build_api(runtime: Any, settings: ApiSettings | None = None) -> Starlette:
    """The node's HTTP API (mounted at /api) and the built web app (at /), as one Starlette app.

    Its lifespan (agentos.mcp.http forwards it when the node mounts this app) runs the lane
    pump that turns lane state changes into live `lane` events."""
    from agentos.api import (
        mounts,
        realtime,
        routes_audit,
        routes_computers,
        routes_core,
        routes_devices,
        routes_files,
        routes_forms,
        routes_freeze,
        routes_login,
        routes_members,
        routes_memory,
        routes_meters,
        routes_ops,
        routes_overview,
        routes_profiles,
        routes_resources,
        routes_worker,
        routes_workflows,
        static,
    )
    from agentos.api import web as web_contract
    from agentos.api.lanes_view import lane_json

    api = Api(runtime, settings or ApiSettings())
    live = live_sockets.of(runtime)  # live streams end when access does (agentos.core.live)
    live.interval = api.settings.live_recheck_seconds
    api.store.on_ended = live.ended
    routes = [
        *routes_members.routes(api),
        *routes_core.routes(api),
        *routes_computers.routes(api),
        *routes_overview.routes(api),
        *routes_devices.routes(api),
        *routes_resources.routes(api),
        *routes_audit.routes(api),
        *routes_freeze.routes(api),
        *routes_meters.routes(api),
        *routes_memory.routes(api),
        *routes_profiles.routes(api),
        *routes_files.routes(api),
        *routes_worker.routes(api),
        *routes_ops.routes(api),
        *routes_workflows.routes(api),
        *(route for name in FEATURE_ROUTES for route in importlib.import_module(name).routes(api)),
        *routes_login.routes(api),
        *routes_forms.routes(api),
        *(mounts.routes(api) if api.settings.mount_features else []),
        *(mounts.site_api_routes(api) if api.settings.mount_features else []),
        *realtime.routes(api),
        *web_contract.routes(api),
        *static.routes(api),
    ]

    def lane_event(info: Any) -> dict:
        from agentos.api.routes_core import frozen_of

        lane_id = info.spec.id if hasattr(info, "spec") else (info.get("spec") or {}).get("id")
        return lane_json(info, hints=None, proxies={}, node=api.node_name,
                         frozen=frozen_of(api, lane_id) if lane_id else None)

    api.bridge.wire_events(api.bus, lane_event)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        pumps = []
        if api.settings.lane_pump_seconds > 0 and runtime.has("lanes"):
            pumps.append(asyncio.create_task(api.bridge.pump_lanes(api.bus, lane_event,
                                                                   interval=api.settings.lane_pump_seconds)))
        if api.settings.schedule_pump_seconds > 0 and runtime.has("runner"):
            pumps.append(asyncio.create_task(api.bridge.pump_schedules(
                api.bus, interval=api.settings.schedule_pump_seconds)))
        if api.settings.brain_pump_seconds > 0 and runtime.has("brains"):
            pumps.append(asyncio.create_task(api.bridge.pump_brains(
                api.bus, interval=api.settings.brain_pump_seconds)))
        try:
            yield
        finally:
            for pump in pumps:
                pump.cancel()
            for pump in pumps:
                with contextlib.suppress(asyncio.CancelledError):
                    await pump

    app = Starlette(
        routes=routes,
        middleware=[Middleware(SecurityHeaders), *mounts.site_api_middleware(),
                    Middleware(GZipMiddleware, minimum_size=1024)],
        lifespan=lifespan,
    )
    app.state.api = api
    return app
