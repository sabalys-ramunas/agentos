"""The lane viewer: signed links, the page, the live-view relay, control actions and events.

Two pieces for the daemon's HTTP server (docs/API.md, "Lane control"):

- `build_viewer_app(lanes, config, traces=None)`: a Starlette app with the routes a viewer
  link opens. They carry their own auth (the signed `t` token), so mount it OUTSIDE the
  bearer check, at the root:
      GET  /view/{ws}/{name}?t=...           the page
      GET  /viewer-ws/{ws}/{name}?t=...      WebSocket relay to the lane's x11vnc
      POST /view/{ws}/{name}/control?t=...   {"action": take|hand_back|stop_agent|deny_request, "note"?}
      POST /view/{ws}/{name}/renew?t=...     a fresh token for an open page
      POST /view/{ws}/{name}/sites?t=...     {"add": entry}: an owner link allows a refused host
      POST /view/{ws}/{name}/clipboard?t=... {} -> {text, chars}: what the lane copied, for a person in control
      GET  /view/{ws}/{name}/events?t=...    SSE: hello, control, lane, action, egress, ended
      GET  /view/{ws}/{name}/login?t=...     the lane's assisted login (agentos.node.login_assist)
      POST /view/{ws}/{name}/login?t=...     {"action": start|step|refresh|take|resume|cancel|save, ...}
      GET  /view/{ws}/{name}/login.png?t=... the login step's redacted screenshot
      GET  /viewer-assets/...                the page's JS, CSS and vendored noVNC
- `control_api_routes(lanes, links)`: the owner's API, mounted under /api BEHIND the
  bearer (or panel session) check:
      POST /api/lanes/{ws}/{name}/control     {"action": ..., "note"?, "done"?} -> Lane (note, done: hand_back only)
      POST /api/lanes/{ws}/{name}/view-link   {"mode": view|control, "ttl_s"?} -> {url, expires_at}
      POST /api/lanes/{ws}/{name}/sites       {"op": add|remove|set, "sites", "strict"?} -> the site list
      GET  /api/lanes/{ws}/{name}/gate-log?since=24h&observed=1  the shapes the gate classified without a rule
      POST /api/lanes/{ws}/{name}/gate-log    {"allow_read": id} | {"as_commit": name, "shape": id} | {"remove": id}
      GET  /api/rules/health?lane=&stale_days=14  effect rule health: last matches, unknown shapes, drift

A link token is HMAC-SHA256 over (lane, mode, expiry, nonce[, owner]) with
AGENTOS_HOME/secrets/viewer.key (32 random bytes, chmod 600, created on first use).
Deleting or replacing the key (`ViewTokens.revoke`) revokes every link, open ones included: the relay
and the event stream sit in the live registry (agentos.core.live), which closes them on a freeze (not an
owner link), at the link's expiry and on revoke. A view token only watches; a control token may act,
and the relay forwards its input only while control is HUMAN. An owner token is a control
token the owner asked for in person (the view-link route, called from the owner's panel session or
the node's CLI with its one-time code; never lane_view, never a grant, never the node token alone):
only it may add a refused host to the lane's site list from a block notice, and only it keeps its
view and its take over through a freeze. Any other link a freeze touched stays dead after it, as a
grant the freeze revoked does.

lane_view gives its link to an agent, which can open it itself, so a link that is not an owner link is
never a person: a hand-back, a take over of a held, stopped or quarantined lane (`person_needed`), the
assisted login and adding a site also need the owner's panel session in the same browser
(`ViewerApp._person_here`; the viewer is served at the panel's address, so its SameSite cookie comes
along), and the page then swaps to an owner link of its own. Without one, the relay passes no input from
the link, and the lane's clipboard (which can hold what never was on the screen) is not read for it. Every link has a hard end signed into it that a renew never moves: AGENT_LINK_MAX for a
link anyone but a person made, OWNER_LINK_MAX for an owner link (docs/THREAT-MODEL.md, "Viewer links").
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import inspect
import json
import logging
import os
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from agentos.core import live as live_sockets
from agentos.core.config import NodeConfig
from agentos.core.errors import AgentosError, InvalidRequest, LaneNotFound
from agentos.core.live import CLOSE_DENIED, CLOSE_REVOKED, LiveEntry, LiveSockets
from agentos.core.models import ControlState, LaneControl, LaneInfo, LaneState, parse_lane_id, validate_sites
from agentos.core.presence import NEEDS_A_PERSON, Presence, presence_of
from agentos.node import lane_clipboard
from agentos.node.vnc_relay import CLOSE_LANE_NOT_RUNNING, CLOSE_POLICY, VncRelay, upstream

log = logging.getLogger("agentos.viewer")

VIEWER_DIR = Path(__file__).parent / "viewer"
ASSETS_DIR = VIEWER_DIR / "assets"
PAGE_TEMPLATE = VIEWER_DIR / "page.html"
ASSETS_PATH = "/viewer-assets"

KEY_BYTES = 32
TOKEN_CONTEXT = b"agentos-view-v2\n"  # v2 signs the hard end: a v1 link no longer verifies
MIN_TTL = 30
MAX_TTL = 24 * 3600
# The hard end of a link, from when it was first made: renewing moves its expiry, never this. A link
# anything but a person made (lane_view, a grant, the node token, the device credential) may be an
# agent's, so it is short; an owner link is made in person.
AGENT_LINK_MAX = 30 * 60
OWNER_LINK_MAX = 8 * 3600
MAX_TOKEN_CHARS = 512
VIEWER_BY = "owner"                # `by` for actions taken from a viewer link or the owner API
MAX_RELAYS_PER_LANE = 8
EVENT_POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 15.0
RECENT_ACTIONS = 40

ACTIONS = ("take", "hand_back", "stop_agent", "deny_request")
HANDBACK_NOTE_CHARS = 300
SITES_OPS = ("add", "remove", "set")
OWNER_FLAG = "owner"
PERSON_ACTIONS = ("hand_back",)    # always a person's: it clears holds, quarantines and a person's stop
# Reading the lane's clipboard: at most this many reads per link in CLIPBOARD_WINDOW seconds. A person
# copies a few times a minute; the limit only stops a loop.
CLIPBOARD_READS = 30
CLIPBOARD_WINDOW = 60.0
NEEDS_PERSON_HERE = ("Only a person can do this, and an agent may hold this link. Sign in to the panel on this node "
                     "in this browser (the same address, at /login), then press it again. Or run "
                     "`agentos lane handback` on the node.")


# ---- signed links -----------------------------------------------------------------------

class ViewMode(StrEnum):
    VIEW = "view"
    CONTROL = "control"


class TokenError(Exception):
    """A viewer token was refused. `message` is safe to show to the human."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ViewLink:
    lane_id: str
    mode: ViewMode
    expires_at: float  # unix seconds
    nonce: str
    token: str
    owner: bool = False  # made in person (an owner link): may do what only a person may
    hard_until: float = 0.0  # unix seconds: no renew moves the link past this

    @property
    def can_control(self) -> bool:
        return self.mode == ViewMode.CONTROL

    @property
    def expires_iso(self) -> str:
        return iso(self.expires_at)

    def path(self, base: str = "") -> str:
        workspace, name = parse_lane_id(self.lane_id)
        return f"{base}/view/{workspace}/{name}?t={quote(self.token)}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_or_create_key(path: Path) -> bytes:
    """The viewer key: 32 random bytes, chmod 600. Created atomically on first use (a
    concurrent creator never sees a half-written file); a short or empty key is replaced."""
    try:
        key = path.read_bytes()
        exists = True
    except FileNotFoundError:
        key, exists = b"", False
    if len(key) >= KEY_BYTES:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            log.warning("%s was readable by others (mode %o); fixing it to 600", path, mode)
            path.chmod(0o600)
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, secrets.token_bytes(KEY_BYTES))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if exists:  # present but empty or too short: replace it
            log.warning("%s is too short; replacing it (old viewer links stop working)", path)
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)  # fails if another process created it first
            except FileExistsError:
                pass
    finally:
        tmp.unlink(missing_ok=True)
    key = path.read_bytes()
    if len(key) < KEY_BYTES:
        raise RuntimeError(f"{path} is unusable; delete it and try again")
    return key


class ViewTokens:
    """Signs and checks viewer links for one node."""

    def __init__(self, key_path: Path, *, default_ttl: int = 900, clock: Callable[[], float] = time.time):
        self.key_path = Path(key_path)
        self.default_ttl = default_ttl
        self.clock = clock
        self._key: bytes | None = None
        self._key_stat: tuple[int, int] | None = None

    @classmethod
    def for_config(cls, config: NodeConfig, **kw: Any) -> ViewTokens:
        return cls(config.secrets_dir / "viewer.key", default_ttl=config.view_token_seconds, **kw)

    def _stat(self) -> tuple[int, int] | None:
        try:
            st = self.key_path.stat()
        except FileNotFoundError:
            return None
        return st.st_ino, st.st_mtime_ns

    @property
    def key(self) -> bytes:
        """The key, read again when the file changed: replacing or deleting it revokes every link,
        including those already open (the live sockets recheck them)."""
        stat = self._stat()
        if self._key is None or stat is None or stat != self._key_stat:
            self._key = load_or_create_key(self.key_path)
            self._key_stat = stat if stat is not None else self._stat()  # stat first: a newer key reloads
        return self._key

    def revoke(self) -> None:
        """Revoke every link: a new key replaces the old one. Open viewers close on their next recheck."""
        tmp = self.key_path.with_name(f".{self.key_path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_bytes(KEY_BYTES))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.key_path)
        self._key = None

    def signed(self, link: ViewLink) -> bool:
        """Does the link still carry this node's signature (not revoked)? Expiry is not checked here."""
        body, _, sig = link.token.partition(".")
        try:
            return hmac.compare_digest(_unb64(sig), self._mac(_unb64(body)))
        except ValueError:
            return False

    def _mac(self, payload: bytes) -> bytes:
        return hmac.new(self.key, TOKEN_CONTEXT + payload, hashlib.sha256).digest()

    def issue(self, lane_id: str, mode: ViewMode | str = ViewMode.VIEW, ttl: float | None = None, *,
              owner: bool = False, nonce: str | None = None, hard_until: float | None = None) -> ViewLink:
        """A signed link. `owner` (control links only) is for a link made in person: only the view-link
        route called in person, and a viewer request carrying the owner's panel session, set it. `nonce`
        and `hard_until` keep an old link's (a renew), so the sockets it opened follow the new expiry and
        the link still ends where it first would have: AGENT_LINK_MAX after it was made, OWNER_LINK_MAX
        for an owner link."""
        parse_lane_id(lane_id)
        mode = ViewMode(mode)
        owner = owner and mode == ViewMode.CONTROL
        now = int(self.clock())
        longest = now + (OWNER_LINK_MAX if owner else AGENT_LINK_MAX)
        hard = longest if hard_until is None else min(int(hard_until), longest)
        if hard <= now:
            raise TokenError("expired", "This viewer link reached the end of its life. Ask for a new one.")
        ttl = self.default_ttl if ttl is None else ttl
        ttl = int(max(MIN_TTL, min(MAX_TTL, ttl)))
        expires = min(now + ttl, hard)
        nonce = nonce or _b64(secrets.token_bytes(9))
        payload = f"{lane_id}|{mode.value}|{expires}|{nonce}|{hard}{'|' + OWNER_FLAG if owner else ''}".encode()
        token = f"{_b64(payload)}.{_b64(self._mac(payload))}"
        return ViewLink(lane_id, mode, float(expires), nonce, token, owner, float(hard))

    def verify(self, token: str | None, lane_id: str) -> ViewLink:
        """Raise TokenError unless `token` is a valid, unexpired link for `lane_id`. The
        signature is checked first, so a tampered token is always "invalid"."""
        if not token:
            raise TokenError("missing", "This page needs a viewer link. Ask the agent for one with lane_view.")
        invalid = TokenError("invalid", "This viewer link is not valid. Ask the agent for a new one.")
        if len(token) > MAX_TOKEN_CHARS or token.count(".") != 1:
            raise invalid
        body, sig = token.split(".")
        try:
            payload, mac = _unb64(body), _unb64(sig)
        except ValueError:
            raise invalid from None
        if not hmac.compare_digest(mac, self._mac(payload)):
            raise invalid
        try:
            parts = payload.decode().split("|")
            owner = len(parts) == 6 and parts[5] == OWNER_FLAG
            if owner:
                parts.pop()
            lane, mode, expires, nonce, hard = parts
            link = ViewLink(lane, ViewMode(mode), float(int(expires)), nonce, token, owner, float(int(hard)))
        except ValueError:
            raise invalid from None
        if link.expires_at > link.hard_until:
            raise invalid
        if link.lane_id != lane_id:
            raise TokenError("wrong_lane", "This viewer link is for a different lane.")
        if min(link.expires_at, link.hard_until) <= self.clock():
            raise TokenError("expired", "This viewer link has expired. Ask the agent for a new one with lane_view.")
        return link


def make_view_link(tokens: ViewTokens, lane_id: str, mode: ViewMode | str = ViewMode.VIEW,
                   ttl: float | None = None, *, base: str = "", owner: bool = False) -> dict[str, Any]:
    """`{"url": "/view/ws/name?t=...", "expires_at": "...", "mode": ..., "owner": ...}` (docs/API.md
    view-link)."""
    link = tokens.issue(lane_id, mode, ttl, owner=owner)
    return {"url": link.path(base), "expires_at": link.expires_iso, "mode": link.mode.value, "owner": link.owner}


# ---- control hub ------------------------------------------------------------------------

class ControlHub:
    """One control listener on the lane manager, fanned out to every open relay and event
    stream, with a per-lane cache so the relay never reads the store per input event."""

    def __init__(self, lanes: Any):
        self.lanes = lanes
        self._cache: dict[str, LaneControl] = {}
        self._watchers: dict[str, list[Callable[[LaneControl], None]]] = {}
        self._notice_watchers: dict[str, list[Callable[[dict], None]]] = {}
        lanes.add_control_listener(self._changed)
        add_notices = getattr(lanes, "add_notice_listener", None)  # a manager without an egress guard has none
        if callable(add_notices):
            add_notices(self._noticed)

    def _noticed(self, lane_id: str, notice: dict) -> None:
        for watcher in list(self._notice_watchers.get(lane_id, ())):
            try:
                watcher(notice)
            except Exception:
                log.exception("notice watcher failed")

    def watch_notices(self, lane_id: str, callback: Callable[[dict], None]) -> Callable[[], None]:
        return _watch(self._notice_watchers, lane_id, callback)

    def _changed(self, lane_id: str, old: LaneControl, new: LaneControl) -> None:
        self._cache[lane_id] = new
        for watcher in list(self._watchers.get(lane_id, ())):
            try:
                watcher(new)
            except Exception:
                log.exception("control watcher failed")

    def control(self, lane_id: str) -> LaneControl:
        control = self._cache.get(lane_id)
        if control is None:
            control = self._cache[lane_id] = self.lanes.get(lane_id).control
        return control

    def state(self, lane_id: str) -> ControlState:
        """Fail closed: a lane that cannot be read counts as paused."""
        try:
            return self.control(lane_id).state
        except Exception:  # noqa: BLE001
            return ControlState.PAUSED

    def watch(self, lane_id: str, callback: Callable[[LaneControl], None]) -> Callable[[], None]:
        return _watch(self._watchers, lane_id, callback)


def _watch(table: dict[str, list[Callable]], lane_id: str, callback: Callable) -> Callable[[], None]:
    table.setdefault(lane_id, []).append(callback)

    def unwatch() -> None:
        watchers = table.get(lane_id, [])
        if callback in watchers:
            watchers.remove(callback)
        if not watchers:
            table.pop(lane_id, None)

    return unwatch


# ---- serialization ----------------------------------------------------------------------

def iso(value: float | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def control_dict(control: LaneControl) -> dict[str, Any]:
    data = control.model_dump(mode="json")
    for key in ("since", "last_human_input"):
        data[key] = iso(getattr(control, key))
    return data


def lane_dict(info: LaneInfo) -> dict[str, Any]:
    """The Lane shape from docs/API.md, as far as the lane manager knows it."""
    spec = info.spec
    out: dict[str, Any] = {
        "id": spec.id,
        "workspace": spec.workspace,
        "name": spec.name,
        "kind": spec.kind.value,
        "engine": spec.engine,
        "state": info.state.value,
        "proxy": {"label": spec.proxy_label} if spec.proxy_label else None,
        "exit_ip": info.exit_ip,
        "last_used": iso(info.last_used),
        "quarantine_reason": info.quarantine_reason,
        "live": info.viewer is not None,
        "control": control_dict(info.control),
    }
    signal = getattr(info, "signal", None)
    if signal is not None:
        out["signal"] = getattr(signal, "value", signal)
    return out


def action_dict(event: Any) -> dict[str, Any] | None:
    """A trace event (agentos.policy TraceEvent or a dict) as the page shows it."""
    if hasattr(event, "public"):
        event = event.public()
    elif hasattr(event, "model_dump"):
        event = event.model_dump(mode="json")
    if not isinstance(event, dict):
        return None
    at = event.get("at")
    if isinstance(at, (int, float)):
        at = iso(at)
    return {
        "id": event.get("id"),
        "at": at,
        "tool": event.get("tool") or event.get("kind"),
        "status": event.get("status"),
        "summary": event.get("summary") or event.get("error"),
        "principal": event.get("principal"),
        "principal_kind": event.get("principal_kind"),
        "effect": event.get("effect"),
    }


async def recent_actions(traces: Any, lane_id: str, limit: int = RECENT_ACTIONS) -> list[dict] | None:
    """Newest first, or None when this node has no traces provider."""
    if traces is None:
        return None
    query = getattr(traces, "query", None) or getattr(traces, "recent", None)
    if query is None:
        return None
    try:
        result = query(lane=lane_id, limit=limit)
        if inspect.isawaitable(result):
            result = await result
    except Exception:
        log.warning("could not read recent actions for %s", lane_id, exc_info=True)
        return []
    events = result[0] if isinstance(result, tuple) else result
    return [a for a in (action_dict(e) for e in events or ()) if a is not None]


async def _race(*coros: Any) -> None:
    """Run until the first side finishes (or fails), then cancel the rest (the relay against its live entry)."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception) and not isinstance(r, (WebSocketDisconnect, ConnectionError)):
            raise r


def _json_for_script(data: Any) -> str:
    """JSON that is safe inside <script type="application/json">."""
    return (json.dumps(data, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _error(status: int, code: str, message: str) -> JSONResponse:
    """The shared error body (agentos.core.http_errors), with the catalog's fix for a person (QA-2 #17)."""
    from agentos.core.http_errors import error_body

    return JSONResponse(error_body(code, message, surface="viewer"), status_code=status, headers={"Cache-Control": "no-store"})


ERROR_STATUS = {"invalid": 400, "lane_not_found": 404, "not_found": 404, "denied": 403,
                "human_in_control": 409, "agent_paused": 409, "busy": 409, "unavailable": 503}


def _token_refusal(exc: TokenError) -> JSONResponse:
    return _error(403, "presence_required" if exc.code == "presence_required" else "denied", exc.message)


def _login_status(exc: AgentosError) -> int:
    from agentos.api.errors import status_for

    return status_for(exc)


def _agentos_error(exc: AgentosError) -> JSONResponse:
    return _error(ERROR_STATUS.get(exc.code, 500), exc.code, exc.message)


# ---- the app ----------------------------------------------------------------------------

class ViewerApp:
    def __init__(self, lanes: Any, config: NodeConfig, *, traces: Any = None, tokens: ViewTokens | None = None,
                 login: Callable[[], Any] | None = None, live: LiveSockets | None = None,
                 freeze: Callable[[], Any] | None = None):
        """`live`: the node's live sockets (agentos.core.live), so a freeze, the link's expiry or a revoke
        closes an open viewer and its event stream. `freeze` returns the node's freeze service."""
        self.lanes = lanes
        self.live = live
        self.freeze = freeze
        self._renewed: dict[str, float] = {}  # nonce -> the latest expiry a renew gave that link
        self._dead: dict[str, float] = {}     # nonce -> its expiry: a non-owner link a freeze ended
        self._clip_reads: dict[str, list[float]] = {}  # nonce -> when that link read the lane's clipboard
        self.login_service = login
        self.config = config
        self.traces = traces
        self.tokens = tokens or ViewTokens.for_config(config)
        self.hub = ControlHub(lanes)
        self._relays: dict[str, int] = {}
        self._template: str | None = None
        self._panel: Any = None

    # -- helpers --
    def make_view_link(self, lane_id: str, mode: ViewMode | str = ViewMode.VIEW,
                       ttl: float | None = None) -> dict[str, str]:
        return make_view_link(self.tokens, lane_id, mode, ttl)

    @staticmethod
    def _lane_id(params: dict) -> str:
        lane_id = f"{params['workspace']}/{params['name']}"
        parse_lane_id(lane_id)
        return lane_id

    def _link(self, request: Request | WebSocket) -> tuple[str, ViewLink]:
        try:
            lane_id = self._lane_id(request.path_params)
        except ValueError:
            raise TokenError("invalid", "This is not a lane address.") from None
        return lane_id, self.tokens.verify(request.query_params.get("t"), lane_id)

    def _person_here(self, request: Request | WebSocket, *, origin: bool = True) -> bool:
        """Is the owner signed in to this node's panel in the browser that sent this request? The panel's
        session cookie (SameSite strict, so another site never sends it) from a one-time code or a passkey:
        Presence.PERSON. Never a member's session, and never the device credential (`agentos connect`
        keeps it in a Keychain an agent may read). `origin`: the request must also come from this page
        (every route that acts; the page itself is a plain navigation, which carries no Origin)."""
        from agentos.api.auth import SESSION_COOKIE, PanelStore

        sid = request.cookies.get(SESSION_COOKIE)
        if not sid or (origin and not self._same_origin(request, required=True)):
            return False
        try:
            if self._panel is None:
                self._panel = PanelStore.for_home(self.config.home)
            session = self._panel.get_session(sid)
        except Exception:  # no panel.db, or it cannot be read: nobody is signed in
            log.warning("could not read the panel's sessions", exc_info=True)
            return False
        return session is not None and not session.is_member and session.via != "device"

    def _owner_link(self, lane_id: str) -> ViewLink:
        """A person's own link, for a page whose owner proved themselves with their panel session."""
        return self.tokens.issue(lane_id, ViewMode.CONTROL, owner=True)

    def _frozen(self, lane_id: str) -> bool:
        service = self.freeze() if self.freeze is not None else None
        frozen_for = getattr(service, "frozen_for", None)
        return callable(frozen_for) and frozen_for(lane_id) is not None

    def _refusal(self, lane_id: str, link: ViewLink, *, person: bool = False) -> tuple[int, str] | None:
        """Why an open viewer on this link must close now, or None. The rule the panel's own viewer
        follows (agentos.api.realtime): a freeze stops everyone but the owner in person, and an owner
        link is one the owner made in person (the view-link route), so it keeps its view and its take
        over. Every link ends at its expiry (a renew moves it) and when the key is replaced."""
        if not self.tokens.signed(link):
            return CLOSE_REVOKED, "the viewer link was revoked"
        if self.tokens.clock() >= max(link.expires_at, self._renewed.get(link.nonce, 0.0)):
            return CLOSE_REVOKED, "the viewer link expired"
        if not (link.owner or person) and (link.nonce in self._dead or self._frozen(lane_id)):
            # like a grant the freeze revoked, the link does not come back when the freeze is lifted
            now = self.tokens.clock()
            for nonce in [n for n, at in self._dead.items() if at <= now]:
                self._dead.pop(nonce, None)
            self._dead[link.nonce] = max(link.expires_at, self._renewed.get(link.nonce, 0.0))
            return CLOSE_DENIED, "frozen: agents and grants are stopped here, and this link ended with them"
        return None

    def _acting_link(self, request: Request) -> tuple[str, ViewLink, bool]:
        """`_link`, for a route that acts (take over, hand back, renew, the assisted login), and whether a
        person is behind it (an owner link, or the owner's panel session in this browser). A link a
        freeze holds or ended is refused unless a person is, so it cannot hand the lane back or log in
        while frozen."""
        lane_id, link = self._link(request)
        # a GET from the page carries no Origin, and SameSite keeps the cookie off other sites' requests
        person = link.owner or (link.can_control and self._person_here(request, origin=request.method != "GET"))
        refused = self._refusal(lane_id, link, person=person)
        if refused is not None:
            raise TokenError("frozen" if refused[0] == CLOSE_DENIED else "invalid", refused[1].capitalize() + ".")
        return lane_id, link, person

    @contextlib.contextmanager
    def _tracked(self, kind: str, lane_id: str, link: ViewLink, *, person: bool = False) -> Iterator[LiveEntry]:
        """The socket's entry in the live registry for its life (a stand-in that never closes without one)."""
        async def recheck() -> tuple[int, str] | None:
            return self._refusal(lane_id, link, person=person)

        if self.live is None:
            yield LiveEntry(kind, recheck, lane=lane_id)
            return
        with self.live.track(kind, recheck, lane=lane_id) as entry:
            yield entry

    @staticmethod
    def _same_origin(request: Request | WebSocket, *, required: bool = False) -> bool:
        """Browsers send Origin on WebSockets and POSTs; it must be this server. With `required`
        (the routes that change something: take, hand back, renew) a request without one is
        refused too, so a link alone cannot drive them from a script; the viewer page always
        sends it. Otherwise clients without an Origin (the CLI, scripts) rely on the token."""
        origin = request.headers.get("origin")
        if not origin:
            return not required
        if origin == "null":
            return False
        return urlsplit(origin).netloc.lower() == request.headers.get("host", "").lower()

    def _page(self) -> str:
        if self._template is None:
            self._template = PAGE_TEMPLATE.read_text()
        return self._template

    def _security_headers(self, request: Request) -> dict[str, str]:
        host = request.headers.get("host", "")
        sockets = f" ws://{host} wss://{host}" if host and all(c.isalnum() or c in ".:-[]" for c in host) else ""
        csp = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
               f"connect-src 'self'{sockets}; font-src 'self'; base-uri 'none'; form-action 'none'; "
               "frame-ancestors 'self'")
        return {
            "Content-Security-Policy": csp,
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
        }

    def _error_page(self, request: Request, status: int, title: str, message: str) -> HTMLResponse:
        base = request.scope.get("root_path", "")
        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{html.escape(title)} · agentos</title>
<link rel="stylesheet" href="{base}{ASSETS_PATH}/viewer.css"></head>
<body class="error-page"><main class="error-card" role="main">
<div class="error-mark" aria-hidden="true"></div>
<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></main></body></html>"""
        return HTMLResponse(body, status_code=status, headers=self._security_headers(request))

    # -- routes --
    async def page(self, request: Request) -> Response:
        try:
            lane_id, link = self._link(request)
        except TokenError as e:
            titles = {"expired": "Link expired", "wrong_lane": "Wrong lane"}
            return self._error_page(request, 403, titles.get(e.code, "Link not valid"), e.message)
        try:
            info = self.lanes.get(lane_id)
        except LaneNotFound:
            return self._error_page(request, 404, "No such lane", f"There is no lane {lane_id} on this node.")
        base = request.scope.get("root_path", "")
        workspace, name = parse_lane_id(lane_id)
        person = link.owner
        here = link.can_control and not link.owner and self._person_here(request, origin=False)
        refused = self._refusal(lane_id, link, person=person or here)
        if refused is not None:  # a freeze ended this link: the page shows nothing of the lane any more
            title = "Lane frozen" if refused[0] == CLOSE_DENIED else "Link not valid"
            return self._error_page(request, 403, title, refused[1].capitalize() + ".")
        if here:
            # the owner, signed in to the panel in this browser, opened a link an agent was given: the
            # page works on a person's own link from here, so the owner can clear what the agent may not
            link, person = self._owner_link(lane_id), True
        boot = {
            "lane": lane_dict(info),
            "mode": link.mode.value,
            "owner": link.owner,
            "person": person,
            "token": link.token,
            "expires_at": link.expires_iso,
            "control_idle_seconds": self.config.control_idle_seconds,
            "node": self.config.name,
            "me": VIEWER_BY,
            "traces": self.traces is not None,
            "paths": {
                "ws": f"{base}/viewer-ws/{workspace}/{name}",
                "events": f"{base}/view/{workspace}/{name}/events",
                "control": f"{base}/view/{workspace}/{name}/control",
                "renew": f"{base}/view/{workspace}/{name}/renew",
                "sites": f"{base}/view/{workspace}/{name}/sites",
                "clipboard": f"{base}/view/{workspace}/{name}/clipboard",
                "page": f"{base}/view/{workspace}/{name}",
                "sign_in": f"{base}/login",
                # an agent environment's browser holds no account, so it has nothing to log in to
                **({"login": f"{base}/view/{workspace}/{name}/login"}
                   if self.login_service and workspace != "env" else {}),
            },
        }
        page = (self._page()
                .replace("{{TITLE}}", html.escape(f"{lane_id} · agentos"))
                .replace("{{ASSETS}}", html.escape(f"{base}{ASSETS_PATH}"))
                .replace("{{BOOT}}", _json_for_script(boot)))
        return HTMLResponse(page, headers=self._security_headers(request))

    async def control(self, request: Request) -> Response:
        """One control action. What only a person may do (`person_needed`: a hand-back, a take over of a
        held or stopped lane) needs an owner link or the owner's panel session in this browser on top of
        the link, because lane_view gives the link to an agent, which can open it itself. When the
        session is what proved it, the answer carries a person's own link for the page (`owner_link`)."""
        try:
            lane_id, link, person = self._acting_link(request)
        except TokenError as e:
            return _error(403, "denied", e.message)
        if not self._same_origin(request, required=True):
            return _error(403, "denied", "cross-site requests are not allowed")
        if not link.can_control:
            return _error(403, "denied", "This link can only watch. Ask the agent for a control link.")
        try:
            body = await request.json()
        except ValueError:
            body = None
        action = body.get("action") if isinstance(body, dict) else None
        note = body.get("note") if isinstance(body, dict) else None
        done = body.get("done") if isinstance(body, dict) else None
        try:
            if action in ACTIONS and not person and person_needed(self.lanes.get(lane_id), action):
                return _error(403, "presence_required", NEEDS_PERSON_HERE)
            info, changed = await apply_action(self.lanes, lane_id, action, by=VIEWER_BY, note=note, done=done)
        except AgentosError as e:
            return _agentos_error(e)
        out: dict[str, Any] = {"lane": lane_dict(info), "changed": changed}
        if (warning := handback_warning(action, info)) is not None:
            out["warning"] = warning
        if person and not link.owner:
            own = self._owner_link(lane_id)
            out["owner_link"] = {"token": own.token, "expires_at": own.expires_iso}
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    async def renew(self, request: Request) -> Response:
        try:
            lane_id, link, _ = self._acting_link(request)
        except TokenError as e:
            return _error(403, "denied", e.message)
        if not self._same_origin(request, required=True):
            return _error(403, "denied", "cross-site requests are not allowed")
        try:  # the same nonce and the same hard end: a renew never makes a link live longer than that
            fresh = self.tokens.issue(lane_id, link.mode, owner=link.owner, nonce=link.nonce,
                                      hard_until=link.hard_until)
        except TokenError as e:
            return _error(403, "denied", e.message)
        self._renewed[link.nonce] = max(fresh.expires_at, self._renewed.get(link.nonce, 0.0))
        now = self.tokens.clock()
        for nonce in [n for n, at in self._renewed.items() if at <= now]:
            self._renewed.pop(nonce, None)
        return JSONResponse({"token": fresh.token, "expires_at": fresh.expires_iso},
                            headers={"Cache-Control": "no-store"})

    async def sites(self, request: Request) -> Response:
        """From a block notice: the owner adds one entry to the lane's site list, through the same
        change as POST /api/lanes/{ws}/{name}/sites. Only an owner link (never one from lane_view)."""
        try:
            lane_id, link = self._link(request)
        except TokenError as e:
            return _error(403, "denied", e.message)
        if not self._same_origin(request, required=True):
            return _error(403, "denied", "cross-site requests are not allowed")
        if not (link.owner or (link.can_control and self._person_here(request))):
            return _error(403, "presence_required", "Only a person can change the lane's site list: sign in to the "
                                                    "panel in this browser, or run the command shown in the notice "
                                                    "on the node.")
        try:
            body = await request.json()
        except ValueError:
            body = None
        entry = body.get("add") if isinstance(body, dict) else None
        if not isinstance(entry, str) or not entry.strip():
            return _error(400, "invalid", 'send JSON like {"add": "frame:www.google.com"}')
        try:
            result = await change_sites(self.lanes, lane_id, {"op": "add", "sites": [entry]}, by=VIEWER_BY)
        except AgentosError as e:
            return _agentos_error(e)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    def _clipboard_paced(self, nonce: str) -> bool:
        """One more read for this link, unless it read CLIPBOARD_READS times in the last CLIPBOARD_WINDOW."""
        now = self.tokens.clock()
        for key in [k for k, at in self._clip_reads.items() if not at or at[-1] <= now - CLIPBOARD_WINDOW]:
            self._clip_reads.pop(key, None)
        recent = [at for at in self._clip_reads.get(nonce, []) if at > now - CLIPBOARD_WINDOW]
        if len(recent) >= CLIPBOARD_READS:
            self._clip_reads[nonce] = recent
            return True
        self._clip_reads[nonce] = [*recent, now]
        return False

    async def clipboard(self, request: Request) -> Response:
        """What the lane's browser copied, for the page to put on this computer's clipboard. At least as
        strict as the relay's input gate (a control link, control HUMAN, a person behind it), because a
        clipboard can hold what was never on the screen: a link an agent may hold never reads it. The
        text is returned and nothing else: the action log records who read how many characters on which
        lane, never the text, and nothing here logs it."""
        try:
            lane_id, link, person = self._acting_link(request)
        except TokenError as e:
            return _error(403, "denied", e.message)
        if not self._same_origin(request, required=True):
            return _error(403, "denied", "cross-site requests are not allowed")
        if not link.can_control:
            return _error(403, "denied", "This link can only watch, so it cannot copy from the lane. Open a control "
                                         "link from the panel.")
        if not person:
            return _error(403, "presence_required", "Only a person can copy from the lane, and an agent may hold "
                                                    "this link. Sign in to the panel on this node in this browser "
                                                    "(the same address, at /login), then copy again.")
        try:
            viewer = self.lanes.view(lane_id)
        except LaneNotFound as e:
            return _error(404, e.code, e.message)
        if self.hub.state(lane_id) != ControlState.HUMAN:
            return _error(403, "denied", "Take control first: what the lane copied comes over only while you have "
                                         "control.")
        if viewer is None:
            return _error(409, "lane_cold", "The lane is not running, so there is nothing to copy. Open it first.")
        if self._clipboard_paced(link.nonce):
            return _error(429, "rate_limited", f"That is more than {CLIPBOARD_READS} copies in a minute. Wait a "
                                               "moment, then copy again.")
        try:
            text = await lane_clipboard.read_clipboard(viewer.display, viewer.xauthority)
        except AgentosError as e:
            return _error(_login_status(e), e.code, e.message)
        self._record_clipboard(lane_id, len(text))
        return JSONResponse({"lane": lane_id, "text": text, "chars": len(text)}, headers={"Cache-Control": "no-store"})

    def _record_clipboard(self, lane_id: str, chars: int) -> None:
        """The action log's row for a clipboard read: who, which lane, how many characters. Never the text."""
        write = getattr(self.traces, "write", None)
        if write is None:
            return
        from agentos.policy.traces import TraceEvent

        summary = (f"copied {chars} characters from the lane to the live view's computer" if chars
                   else "read the lane's clipboard for the live view: nothing was copied")
        try:
            write(TraceEvent(kind="lane_clipboard", tool="lane.clipboard", status="ok", principal=VIEWER_BY,
                             principal_kind="human", lane=lane_id, summary=summary, args={"chars": chars}))
        except Exception:
            log.warning("%s: could not record a clipboard read", lane_id, exc_info=True)  # the row has no text

    async def events(self, request: Request) -> Response:
        try:
            lane_id, link = self._link(request)
            info = self.lanes.get(lane_id)
        except TokenError as e:
            return _error(403, "denied", e.message)
        except LaneNotFound as e:
            return _error(404, e.code, e.message)
        refused = self._refusal(lane_id, link)
        if refused is not None:
            return _error(409, "frozen", refused[1]) if refused[0] == CLOSE_DENIED else _error(403, "denied", refused[1])
        return StreamingResponse(self._event_stream(lane_id, link, info), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def _event_stream(self, lane_id: str, link: ViewLink, info: LaneInfo):
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=256)

        def put(item: tuple[str, Any]) -> None:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(item)

        def on_trace(event_type: str, payload: Any) -> None:
            lane = payload.get("lane") if isinstance(payload, dict) else getattr(payload, "lane", None)
            if event_type == "trace" and lane == lane_id:
                loop.call_soon_threadsafe(put, ("action", payload))

        entry_cm = self._tracked("viewer_events", lane_id, link)
        entry = entry_cm.__enter__()
        entry.on_close = lambda e: loop.call_soon_threadsafe(put, ("ended", e.reason))
        unwatch = self.hub.watch(lane_id, lambda control: put(("control", control)))
        unwatch_notices = self.hub.watch_notices(
            lane_id, lambda notice: loop.call_soon_threadsafe(put, ("egress", notice)))
        unsubscribe: Callable[[], Any] | None = None
        subscribe = getattr(self.traces, "subscribe", None)
        if subscribe is not None:
            try:
                unsubscribe = subscribe(on_trace)
            except Exception:
                log.warning("could not subscribe to traces", exc_info=True)
        try:
            actions = await recent_actions(self.traces, lane_id)
            last_lane = lane_dict(info)
            yield "retry: 3000\n\n"
            yield _sse("hello", {"lane": last_lane, "actions": actions, "mode": link.mode.value})
            newest = actions[0]["id"] if actions else None
            last_poll = last_beat = loop.time()
            while True:
                if entry.closed.is_set():  # a freeze, the link's expiry or a revoke (agentos.core.live)
                    yield _sse("ended", {"lane": lane_id, "reason": entry.reason or "access ended"})
                    return
                try:
                    kind, item = await asyncio.wait_for(queue.get(), EVENT_POLL_SECONDS)
                except TimeoutError:
                    kind, item = "", None
                if kind == "ended":
                    continue
                if kind == "control":
                    yield _sse("control", {"lane": lane_id, "control": control_dict(item)})
                elif kind == "egress":
                    yield _sse("egress", {"lane": lane_id, **item})
                elif kind == "action":
                    action = action_dict(item)
                    if action is not None:
                        newest = action["id"] or newest
                        yield _sse("action", action)
                now = loop.time()
                if now - last_poll >= EVENT_POLL_SECONDS:
                    last_poll = now
                    try:
                        current = lane_dict(self.lanes.get(lane_id))
                    except LaneNotFound:
                        yield _sse("gone", {"lane": lane_id})
                        return
                    if current != last_lane:
                        last_lane = current
                        yield _sse("lane", current)
                    if unsubscribe is None and self.traces is not None:
                        polled = await recent_actions(self.traces, lane_id)
                        if polled and polled[0]["id"] != newest:
                            newest = polled[0]["id"]
                            yield _sse("actions", {"actions": polled})
                if now - last_beat >= HEARTBEAT_SECONDS:
                    last_beat = now
                    yield ": ping\n\n"
        finally:
            entry_cm.__exit__(None, None, None)
            unwatch()
            unwatch_notices()
            if callable(unsubscribe):
                with contextlib.suppress(Exception):
                    unsubscribe()

    async def relay(self, websocket: WebSocket) -> None:
        try:
            lane_id, link = self._link(websocket)
            viewer = self.lanes.view(lane_id)
        except (TokenError, LaneNotFound) as e:
            log.info("live view refused: %s", getattr(e, "message", e))
            await websocket.close(code=CLOSE_POLICY)
            return
        if not self._same_origin(websocket):
            log.warning("live view refused: cross-site origin %s", websocket.headers.get("origin"))
            await websocket.close(code=CLOSE_POLICY)
            return
        if self._relays.get(lane_id, 0) >= MAX_RELAYS_PER_LANE:
            await websocket.close(code=1013)  # try again later
            return
        offered = websocket.scope.get("subprotocols") or []
        await websocket.accept(subprotocol="binary" if "binary" in offered else None)
        if viewer is None:
            await websocket.close(code=CLOSE_LANE_NOT_RUNNING, reason="the lane is not running")
            return
        person = link.owner or (link.can_control and self._person_here(websocket))
        refused = self._refusal(lane_id, link, person=person)
        if refused is not None:
            await websocket.close(code=refused[0], reason=refused[1])
            return
        hub = self.hub

        def allow_input() -> bool:
            # only a person types: input here counts as a human's, which the commit gate and the
            # approvals never stop, and a captcha is a person's to answer. A link an agent may hold
            # can take an unheld lane (that only stops agents) but never drive it.
            return person and link.can_control and hub.state(lane_id) == ControlState.HUMAN

        relay = VncRelay(websocket, **upstream(viewer), allow_input=allow_input,
                         on_input=lambda: self.lanes.note_human_input(lane_id))
        unwatch = hub.watch(lane_id, lambda control: relay.control_changed())
        self._relays[lane_id] = self._relays.get(lane_id, 0) + 1
        attached = link.can_control  # a forgotten view-only tab must not keep a lane warm
        if attached:
            getattr(self.lanes, "viewer_attached", lambda _: None)(lane_id)
        getattr(self.lanes, "view_opened", lambda _: None)(lane_id)  # any watcher: no exit IP probe window
        try:
            with self._tracked("viewer", lane_id, link, person=person) as entry:
                await _race(relay.run(), entry.wait_closed())
                if entry.closed.is_set():
                    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                        await websocket.close(code=entry.code or CLOSE_DENIED, reason=entry.reason or "")
        finally:
            getattr(self.lanes, "view_closed", lambda _: None)(lane_id)
            if attached:
                getattr(self.lanes, "viewer_detached", lambda _: None)(lane_id)
            unwatch()
            self._relays[lane_id] -= 1
            if not self._relays[lane_id]:
                self._relays.pop(lane_id)

    # -- the assisted login's side section (agentos.node.login_assist) --
    def _login(self, request: Request) -> tuple[str, ViewLink, Any]:
        """The assisted login is a person's (it types credentials and saves sessions): an owner link, or
        a control link with the owner's panel session in this browser."""
        lane_id, link, person = self._acting_link(request)
        if self.login_service is None:
            raise TokenError("unavailable", "Assisted login is not on this node.")
        if not link.can_control:
            raise TokenError("view_only", "This link can only watch. Logging in needs a control link.")
        if not person:
            raise TokenError("presence_required", NEEDS_PERSON_HERE)
        return lane_id, link, self.login_service()

    async def login_get(self, request: Request) -> Response:
        try:
            lane_id, _, svc = self._login(request)
        except TokenError as e:
            return _token_refusal(e)
        after, wait = request.query_params.get("after"), request.query_params.get("wait")
        try:
            if after is not None and wait is not None:
                out = await svc.wait_change(lane_id, int(after), max(0, min(25, int(wait))))
            else:
                out = svc.get(lane_id)
        except ValueError:
            return _error(400, "invalid", "after and wait are whole numbers")
        return JSONResponse(out or {"lane": lane_id, "state": "none"}, headers={"Cache-Control": "no-store"})

    async def login_shot(self, request: Request) -> Response:
        try:
            lane_id, _, svc = self._login(request)
        except TokenError as e:
            return _token_refusal(e)
        png = svc.screenshot(lane_id)
        if png is None:
            return _error(404, "not_found", "There is no screenshot of this step.")
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})

    async def login_post(self, request: Request) -> Response:
        """One action on the lane's login. Values arrive as text and become Secrets at once; nothing here
        logs, traces or returns them."""
        from agentos.api.routes_login import read_values

        try:
            lane_id, link, svc = self._login(request)
        except TokenError as e:
            return _token_refusal(e)
        if not self._same_origin(request):
            return _error(403, "denied", "cross-site requests are not allowed")
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _error(400, "invalid", 'send JSON like {"action": "start"}')
        action = body.pop("action", None)
        values: dict = {}
        try:
            values = read_values(body.pop("values", None))
            if action == "start":
                url = body.get("url")
                out = await svc.start(lane_id, by=VIEWER_BY, url=url if isinstance(url, str) else None)
            elif action == "step":
                out = await svc.submit(lane_id, step=str(body.get("step") or ""), values=values,
                                       choice=body.get("choice") if isinstance(body.get("choice"), str) else None,
                                       by=VIEWER_BY)
            elif action in ("refresh", "take", "resume"):
                out = await getattr(svc, action)(lane_id, by=VIEWER_BY)
            elif action == "cancel":
                out = await svc.cancel(lane_id, by=VIEWER_BY, done=body.get("done") is True)
            elif action == "save":  # _login let only a person through
                if not link.can_control:
                    return _error(403, "denied", "Only the owner's own link saves to the vault. Save it from the "
                                                 "panel instead.")
                name = body.get("name")
                out = await svc.save(lane_id, by=VIEWER_BY, name=name if isinstance(name, str) else None)
            else:
                return _error(400, "invalid", "action is start, step, refresh, take, resume, cancel or save")
        except AgentosError as e:
            return _error(_login_status(e), e.code, e.message)
        finally:
            values.clear()
            body.clear()
        return JSONResponse({**out, "owner_link": True}, headers={"Cache-Control": "no-store"})  # only a person

    def routes(self) -> list:
        return [
            Route("/view/{workspace}/{name}/login", self.login_get, methods=["GET"]),
            Route("/view/{workspace}/{name}/login", self.login_post, methods=["POST"]),
            Route("/view/{workspace}/{name}/login.png", self.login_shot, methods=["GET"]),
            Route("/view/{workspace}/{name}", self.page, methods=["GET"]),
            Route("/view/{workspace}/{name}/control", self.control, methods=["POST"]),
            Route("/view/{workspace}/{name}/renew", self.renew, methods=["POST"]),
            Route("/view/{workspace}/{name}/sites", self.sites, methods=["POST"]),
            Route("/view/{workspace}/{name}/clipboard", self.clipboard, methods=["POST"]),
            Route("/view/{workspace}/{name}/events", self.events, methods=["GET"]),
            WebSocketRoute("/viewer-ws/{workspace}/{name}", self.relay),
            Mount(ASSETS_PATH, app=StaticFiles(directory=ASSETS_DIR), name="viewer-assets"),
        ]


def person_needed(info: LaneInfo, action: Any) -> bool:
    """Does this control action need a person (docs/THREAT-MODEL.md)? A hand-back always: it clears a
    hold, a quarantine and a person's stop. Taking over too while the lane is held, stopped or
    quarantined, since a take there is how a held lane's browser gets used. Stopping an agent and
    keeping control (deny_request) only ever hold agents off, so any control link may."""
    if action in PERSON_ACTIONS:
        return True
    if action != "take":
        return False
    control = info.control
    return (control.state == ControlState.PAUSED or bool(control.reason) or bool(info.quarantine_reason)
            or info.state == LaneState.QUARANTINED)


def handback_warning(action: Any, info: LaneInfo) -> str | None:
    """What a person reads when their hand-back did not give agents the lane: the page still shows
    a challenge (the lane manager read it at the hand-back and held the lane again)."""
    control = info.control
    if action != "hand_back" or control.state == ControlState.AGENT or not control.reason:
        return None
    what = control.reason.split(" on ", 1)[0].removeprefix("needs_human:").replace("_", " ")
    return (f"Agents were not given the lane: the page still shows {what}. Agents never act on that page, "
            "so the lane is held again. Deal with it in the live view (or open another page), then hand back.")


CHALLENGES = frozenset({"captcha", "checkpoint", "logged_out", "blocked"})  # what only a person clears


def challenge_held(info: Any) -> bool:
    """The lane is held for a person (a `needs_human:` hold) and its last page still shows the challenge
    (a robot check, a sign-in, a checkpoint): handing back does not make that go away."""
    signal = getattr(info.signal, "value", info.signal) if getattr(info, "signal", None) else None
    reason = getattr(info.control, "reason", None) or getattr(info, "hold", None) or ""
    return signal in CHALLENGES and str(reason).startswith("needs_human")


async def apply_action(lanes: Any, lane_id: str, action: Any, *, by: str, note: Any = None,
                       done: Any = None) -> tuple[LaneInfo, bool]:
    """Run one control action. Returns the lane and whether control changed. `note` (hand_back
    only) is what the human tells the agents, at most HANDBACK_NOTE_CHARS.

    `done` (hand_back only, optional, for the web app): whether the person says the challenge that
    holds the lane (a robot check, a sign-in) is done. False keeps a challenge hold: agents stay off and
    the lane waits for the check. True or left out hands back as always: the lane manager reads the page
    at the hand-back and holds the lane again if it still shows the challenge (handback_warning)."""
    if action not in ACTIONS:
        raise InvalidRequest(f"action must be one of {', '.join(ACTIONS)}")
    if note is not None and (action != "hand_back" or not isinstance(note, str) or len(note) > HANDBACK_NOTE_CHARS):
        raise InvalidRequest(f"note goes with hand_back only, as text of at most {HANDBACK_NOTE_CHARS} characters")
    if done is not None and (action != "hand_back" or not isinstance(done, bool)):
        raise InvalidRequest("done goes with hand_back only, as true or false")
    current = lanes.get(lane_id)
    before = current.control
    if action == "hand_back" and done is False and challenge_held(current):
        # The check is still there: the person lets go, and the lane stays held for it.
        info = await lanes.stop_agent(lane_id, by=by) if before.state == ControlState.HUMAN else current
        return info, info.control != before
    if action == "take":
        info = await lanes.take_control(lane_id, by=by)
    elif action == "hand_back":
        info = await lanes.hand_back(lane_id, by=by, note=note) if note else await lanes.hand_back(lane_id, by=by)
    elif action == "stop_agent":
        info = await lanes.stop_agent(lane_id, by=by)
    else:
        info = await lanes.deny_request(lane_id, by=by)
    return info, info.control != before


async def change_sites(lanes: Any, lane_id: str, body: Any, *, by: str) -> dict[str, Any]:
    """Change a lane's site list on this manager, so a warm lane's guard has it at once:
    `{"op": "add" | "remove" | "set", "sites": [...], "strict"?: bool}`. `add` appends what is not
    there yet, `remove` refuses entries that are not on the list, `set` replaces it (`[]`: no lane
    restriction). Returns {lane, sites, strict, changed, running}."""
    if not isinstance(body, dict):
        raise InvalidRequest('send JSON like {"op": "add", "sites": ["reddit.com"]}')
    op, sites, strict = body.get("op", "add"), body.get("sites", []), body.get("strict")
    if op not in SITES_OPS:
        raise InvalidRequest(f"op must be one of {', '.join(SITES_OPS)}")
    if not isinstance(sites, list) or not all(isinstance(x, str) for x in sites):
        raise InvalidRequest("sites must be a list of domain patterns")
    if strict is not None and not isinstance(strict, bool):
        raise InvalidRequest("strict must be true or false")
    try:
        wanted = validate_sites(sites) if sites else []
    except ValueError as e:
        raise InvalidRequest(str(e)) from None
    info = lanes.get(lane_id)
    current = list(info.spec.sites)
    if op == "add":
        new = current + [x for x in wanted if x not in current]
    elif op == "remove":
        missing = [x for x in wanted if x not in current]
        if missing:
            raise InvalidRequest(f"not on the list: {', '.join(missing)}")
        new = [x for x in current if x not in wanted]
    else:
        new = wanted
    before = (current, info.spec.egress_strict)
    info = await lanes.set_sites(lane_id, new, strict=strict, by=by)
    return {"lane": lane_id, "sites": list(info.spec.sites), "strict": info.spec.egress_strict,
            "changed": (list(info.spec.sites), info.spec.egress_strict) != before,
            "running": info.state in (LaneState.WARM, LaneState.ACTIVE)}


def _the_owner(request: Request) -> bool:
    """The owner and nobody else: not a member's session, not a grant (an admin grant is the owner's
    for most routes, but it is an agent's credential). Behind the bare node-token check (no panel)
    there is no Access, and the caller is the node token."""
    access = getattr(getattr(request.state, "auth", None), "access", None)
    if access is None:
        return True
    return bool(getattr(access, "owner", False)) and getattr(access, "member", None) is None \
        and access.presence >= Presence.NODE_TOKEN


def _owner_in_person(request: Request, lanes: Any = None) -> bool:
    """The owner, in person (Presence.PERSON: a panel session or the node's CLI, whose one-time code
    the panel.db of `lanes.config.home` holds). Never the node token or the device credential alone:
    an agent may hold either (docs/THREAT-MODEL.md)."""
    home = getattr(getattr(lanes, "config", None), "home", None)
    return _the_owner(request) and presence_of(request, home) >= Presence.PERSON


def build_viewer_app(lanes: Any, config: NodeConfig, *, traces: Any = None,
                     tokens: ViewTokens | None = None, login: Callable[[], Any] | None = None,
                     live: LiveSockets | None = None, freeze: Callable[[], Any] | None = None) -> Starlette:
    """The public viewer routes (token auth). `app.state.viewer` is the ViewerApp, whose
    `make_view_link(lane_id, mode, ttl)` the MCP lane_view tool uses. `login` returns the node's
    assisted login service (agentos.node.login_assist.service_for), for the live view's login form."""
    viewer = ViewerApp(lanes, config, traces=traces, tokens=tokens, login=login, live=live, freeze=freeze)
    app = Starlette(routes=viewer.routes())
    app.state.viewer = viewer
    return app


def viewer_app_from_runtime(runtime: Any) -> Starlette:
    """build_viewer_app from an agentos.core.runtime.Runtime (lanes, config, traces?)."""
    from agentos.node.login_assist import service_for

    return build_viewer_app(runtime.require("lanes"), runtime.require("config"), traces=runtime.get("traces"),
                            tokens=runtime.get("viewer_links"), login=lambda: service_for(runtime),
                            live=live_sockets.of(runtime), freeze=lambda: runtime.get("freeze"))


def control_api_routes(lanes: Any, tokens: ViewTokens, *, by: str = VIEWER_BY, base: str = "",
                       traces: Any = None) -> list:
    """The owner's control API. Mount under /api behind the bearer or panel session check."""

    async def control(request: Request) -> Response:
        try:
            lane_id = ViewerApp._lane_id(request.path_params)
            body = await request.json()
        except ValueError:
            return _error(400, "invalid", 'send JSON like {"action": "take"}')
        action = body.get("action") if isinstance(body, dict) else None
        note = body.get("note") if isinstance(body, dict) else None
        done = body.get("done") if isinstance(body, dict) else None
        try:
            if action in ACTIONS and person_needed(lanes.get(lane_id), action) \
                    and not _owner_in_person(request, lanes):
                auth = getattr(request.state, "auth", None)
                if auth is not None:
                    auth.access.trace("lane.control", status="denied", lane=lane_id, summary=f"{action}: not in person")
                return _error(403, "presence_required", NEEDS_A_PERSON)
            info, changed = await apply_action(lanes, lane_id, action, by=by, note=note, done=done)
        except AgentosError as e:
            return _agentos_error(e)
        out = {**lane_dict(info), "changed": changed}
        if (warning := handback_warning(action, info)) is not None:
            out["warning"] = warning
        return JSONResponse(out)

    async def view_link(request: Request) -> Response:
        try:
            lane_id = ViewerApp._lane_id(request.path_params)
            raw = await request.body()
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return _error(400, "invalid", 'send JSON like {"mode": "control"}')
        if not isinstance(body, dict):
            return _error(400, "invalid", 'send JSON like {"mode": "control"}')
        mode = body.get("mode", "view")
        ttl = body.get("ttl_s")
        if mode not in (ViewMode.VIEW.value, ViewMode.CONTROL.value):
            return _error(400, "invalid", "mode must be view or control")
        if ttl is not None and (not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0):
            return _error(400, "invalid", "ttl_s must be a positive number of seconds")
        try:
            lanes.get(lane_id)
        except AgentosError as e:
            return _agentos_error(e)
        return JSONResponse(make_view_link(tokens, lane_id, mode, ttl, base=base,
                                           owner=_owner_in_person(request, lanes)))

    out = [
        Route("/lanes/{workspace}/{name}/control", control, methods=["POST"]),
        Route("/lanes/{workspace}/{name}/view-link", view_link, methods=["POST"]),
    ]
    return (out + sites_api_routes(lanes, by=by) + gate_log_api_routes(lanes, by=by, traces=traces)
            + rule_health_api_routes(lanes))


def gate_log_api_routes(lanes: Any, *, by: str = VIEWER_BY, traces: Any = None) -> list:
    """GET and POST /api/lanes/{ws}/{name}/gate-log (agentos.node.gateshapes): the shapes the commit
    gate classified without a rule in this lane, and the owner's promotions of one to the node's
    read allowlist or to a named commit rule (or their removal). The owner reads it (`_the_owner`); a
    change needs the owner in person (`_owner_in_person`);
    a refusal and every change are traced, a change as `gate_shape` in the action log."""
    from agentos.node import gateshapes

    def refuse(request: Request, lane_id: str) -> Response:
        auth = getattr(request.state, "auth", None)
        if auth is not None:
            auth.access.trace("lane.gate_log", status="denied", lane=lane_id, summary="not the owner in person")
        access = getattr(auth, "access", None)
        owners = access is None or (bool(getattr(access, "owner", False)) and getattr(access, "member", None) is None)
        code = "presence_required" if owners and request.method == "POST" else "denied"  # the owner, not in person
        return _error(403, code, "Only the owner reads the gate log, and only the owner in person (the panel, "
                                     "or `agentos lane gate-log` on the node) changes what the gate allows. A "
                                     "member, a grant, the node token or a saved device credential cannot.")

    def home_and_dir(lane_id: str) -> tuple[Path, Path]:
        lanes.get(lane_id)  # the lane exists
        config = lanes.config
        return Path(config.home), config.lane_dir(lane_id)

    async def gate_log(request: Request) -> Response:
        try:
            lane_id = ViewerApp._lane_id(request.path_params)
        except AgentosError as e:
            return _agentos_error(e)
        if not (_owner_in_person(request, lanes) if request.method == "POST" else _the_owner(request)):
            return refuse(request, lane_id)
        try:
            home, lane_dir = home_and_dir(lane_id)
            if request.method == "GET":
                since = gateshapes.since_of(request.query_params.get("since"))
                promoted = gateshapes.load(gateshapes.store_path(home))
                rows = gateshapes.trace_rows(lane_dir)
                shapes = gateshapes.log(rows, since=since, promoted=promoted,
                                        observed=request.query_params.get("observed") in ("1", "true"))
                return JSONResponse({"lane": lane_id, "shapes": shapes, "promoted": promoted,
                                     "identities": gateshapes.identities(rows, since=since)},
                                    headers={"Cache-Control": "no-store"})
            try:
                body = await request.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                raise InvalidRequest('send JSON like {"allow_read": "sh_..."} or {"as_commit": "post.media", '
                                     '"shape": "sh_..."}')
            if isinstance(body.get("remove"), str):
                entry = gateshapes.remove(home, body["remove"])
                removed = True
            elif isinstance(body.get("allow_read"), str):
                entry = gateshapes.promote(home, lane_dir, body["allow_read"], as_="read", lane=lane_id, by=by,
                                           base=_base_rules(lanes, lane_id), all_lanes=body.get("all_lanes") is True)
                removed = False
            elif isinstance(body.get("as_commit"), str) and isinstance(body.get("shape"), str):
                entry = gateshapes.promote(home, lane_dir, body["shape"], as_="commit", name=body["as_commit"],
                                           lane=lane_id, by=by, base=_base_rules(lanes, lane_id),
                                           all_lanes=body.get("all_lanes") is True)
                removed = False
            else:
                raise InvalidRequest('send {"allow_read": id}, {"as_commit": name, "shape": id} or {"remove": id}')
        except AgentosError as e:
            return _agentos_error(e)
        if traces is not None:
            gateshapes.audit(traces, entry, lane=lane_id, by=by, removed=removed)
        else:
            auth = getattr(request.state, "auth", None)
            if auth is not None:
                auth.access.trace("lane.gate_log", lane=lane_id, summary=f"gate shape {entry['id']} "
                                  f"{'removed' if removed else 'promoted as ' + str(entry.get('rule') or 'read')}")
        return JSONResponse({"lane": lane_id, "removed" if removed else "promoted": entry},
                            headers={"Cache-Control": "no-store"})

    return [Route("/lanes/{workspace}/{name}/gate-log", gate_log, methods=["GET", "POST"])]


def rule_health_api_routes(lanes: Any) -> list:
    """GET /api/rules/health?lane=&stale_days= (agentos.node.rulehealth): when each commit and write
    rule last matched, the shapes no rule names per site, and what looks like drift. The owner only,
    like the gate log (names and shapes only, but they say what each account does)."""
    from agentos.node import rulehealth

    async def health(request: Request) -> Response:
        if not _the_owner(request):  # a read, like the gate log's GET: the owner, not in person
            auth = getattr(request.state, "auth", None)
            if auth is not None:
                auth.access.trace("rules.health", status="denied", summary="not the owner")
            return _error(403, "denied", "Only the owner reads effect rule health (the node token or the owner's "
                                         "panel session). A member or a grant cannot.")
        lane = request.query_params.get("lane") or None
        try:
            stale = float(request.query_params.get("stale_days") or rulehealth.STALE_DAYS)
        except ValueError:
            return _error(400, "invalid", "stale_days is a number of days")
        try:
            if lane is not None:
                lanes.get(lane)  # the lane exists
            specs = [info.spec for info in lanes.list()]
            done = await asyncio.to_thread(rulehealth.for_node, Path(lanes.config.home), specs, lane=lane,
                                           stale_days=stale)
        except AgentosError as e:
            return _agentos_error(e)
        return JSONResponse(done, headers={"Cache-Control": "no-store"})

    return [Route("/rules/health", health, methods=["GET"])]


def _base_rules(lanes: Any, lane_id: str) -> Any:
    """The built-in rules the lane's guard classifies by (with the dev site's on the dev node)."""
    from agentos.node import egress

    lane = getattr(lanes, "_lanes", {}).get(lane_id)
    guard = getattr(getattr(lane, "session", None), "guard", None)
    return getattr(guard, "_base_rules", None) or egress.EFFECT_RULES


def sites_api_routes(lanes: Any, *, by: str = VIEWER_BY) -> list:
    """POST /api/lanes/{ws}/{name}/sites: the owner changes a lane's site list on the daemon's own
    lane manager, so a warm lane's guard has it at once (`agentos lane sites` calls it while agentosd
    runs). The owner in person only (Presence.PERSON: the panel, or the node's CLI with its own session):
    an agent's grant, the node token and the device credential never widen a lane."""

    async def sites(request: Request) -> Response:
        try:
            lane_id = ViewerApp._lane_id(request.path_params)
            body = await request.json()
        except ValueError:
            return _error(400, "invalid", 'send JSON like {"op": "add", "sites": ["reddit.com"]}')
        if not _owner_in_person(request, lanes):
            auth = getattr(request.state, "auth", None)
            if auth is not None:
                auth.access.trace("lane.sites", status="denied", lane=lane_id, summary="a grant, not the owner")
            return _error(403, "presence_required", "Only the owner in person changes a lane's site list (the "
                                         "panel, or `agentos lane sites` on the node). A grant, the node token or "
                                         "a saved device credential cannot, because an agent may hold them.")
        try:
            return JSONResponse(await change_sites(lanes, lane_id, body, by=by), headers={"Cache-Control": "no-store"})
        except AgentosError as e:
            return _agentos_error(e)

    return [Route("/lanes/{workspace}/{name}/sites", sites, methods=["POST"])]


def routes(runtime: Any) -> list:
    """The control API for the panel to mount behind its own auth (agentos.api.mounts): take,
    hand back, stop and deny, signed view links, and the lane's site list. Paths are relative to /api."""
    lanes = runtime.get("lanes")
    config = runtime.get("config")
    if lanes is None or config is None:
        return []
    if not callable(getattr(lanes, "take_control", None)):  # no take over on this runtime: no control routes
        return sites_api_routes(lanes) if callable(getattr(lanes, "set_sites", None)) else []
    tokens = runtime.get("viewer_links")
    if tokens is None:
        tokens = ViewTokens.for_config(config)
        runtime.register("viewer_links", tokens)
    return control_api_routes(lanes, tokens, traces=runtime.get("traces"))

