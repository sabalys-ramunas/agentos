"""The code card: a one-time code the owner types in the panel, for one login step.

docs/design/phones.md, Brief 1a. When an entry's code source is `{"kind": "relay"}` and a
login reaches the code step, `RelayCodeSource.arm()` opens a `CodeRequest` here and the
owner gets a push that links to the panel. The owner reads the SMS (or the app code) and
types it into the card's sealed field; `POST /api/codes/{id}` hands it to `submit()`,
which resolves the waiting login, and the node types it into the site.

Rules:

- A request belongs to one lane, one site (the entry's registrable domain), one entry, one
  requester and one time window. Requests live in memory only and die with the process.
- It is taken once: the first accepted code closes it. A later code, a code after the
  window, or a code for a closed request is refused.
- Who may answer: the route is owner-only (the owner, or an admin grant acting as an
  approver), and a token never answers a request its own grant family made, so an agent or
  a VA cannot type a code for its own login.
- The code is a `Secret` from the request body to the typist. It is never stored, traced,
  logged, published or returned. Pushes and live events carry the request id, lane and site.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agentos.core.errors import InvalidRequest
from agentos.vault.errors import CodeDeclined, CodeRefused, CodeRequestClosed
from agentos.vault.secret import Secret

log = logging.getLogger("agentos.vault")

CODE_RE = re.compile(r"^[A-Za-z0-9]{4,12}$")
EVENT_KIND = "code"
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_request_id(now: float | None = None) -> str:
    """ "cr_" and a ULID: 48 bits of milliseconds, then 80 random bits. Lowercase (ULIDs are case
    blind), like the policy's ids, so the trace redactor does not mistake it for a token."""
    stamp = int((time.time() if now is None else now) * 1000)
    value = int.from_bytes(stamp.to_bytes(6, "big") + secrets.token_bytes(10), "big")
    out = []
    for _ in range(26):
        out.append(_ULID_ALPHABET[value & 31])
        value >>= 5
    return "cr_" + "".join(reversed(out)).lower()


def normalize_code(raw: Secret) -> Secret:
    """The code as the site wants it: spaces and dashes dropped ("123 456", "123-456")."""
    text = "".join(raw.reveal().split()).replace("-", "")
    if not CODE_RE.match(text):
        raise InvalidRequest("A code is 4 to 12 letters or digits.")
    return Secret(text)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


@dataclass
class CodeRequest:
    id: str
    lane: str
    site: str                       # the entry's registrable domain
    entry: str                      # entry id, never a secret field
    requester: dict[str, str]       # {"kind", "id"} of the principal whose call needs the code
    grant_path: str | None          # "/root/.../leaf/" of that call's grant, for the family rule
    created_at: float
    expires_at: float
    state: str = "open"             # open | used | declined | expired | cancelled
    closed_by: str | None = None
    closed_at: float | None = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state, "lane": self.lane, "site": self.site,
                "requested_by": dict(self.requester), "created_at": _iso(self.created_at),
                "expires_at": _iso(self.expires_at)}


@dataclass
class _Slot:
    request: CodeRequest
    loop: asyncio.AbstractEventLoop
    closed: asyncio.Future[None]
    code: Secret | None = field(default=None, repr=False)


def _wake(slot: _Slot) -> None:
    if not slot.closed.done():
        slot.closed.set_result(None)


class CodeRelay:
    """The open code requests of this node. `runtime` (set by the vault's build) gives the
    notifier for pushes and the event bus for the panel's live updates."""

    def __init__(self, *, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        self.runtime: Any = None
        self.max_wait: float | None = None   # tests: cap every wait at this many seconds

    def __repr__(self) -> str:
        return f"CodeRelay(open={len(self._slots)})"

    # ---- side channels (never carry a code) ----
    def _service(self, name: str) -> Any:
        rt = self.runtime
        return rt.get(name) if rt is not None else None

    def _publish(self, request: CodeRequest) -> None:
        bus = self._service("events")
        if bus is None:
            return
        try:
            bus.publish(EVENT_KIND, request.public())
        except Exception as e:  # noqa: BLE001 - a live update never fails a login
            log.warning("vault: code event not published: %s", type(e).__name__)

    def _notify(self, request: CodeRequest) -> None:
        notifier = self._service("notifier")
        send = getattr(notifier, "send", None)
        if send is None:
            return
        lane = request.lane.replace("/", " / ")
        try:
            send({"type": "code.requested", "title": f"Login code for {request.site}",
                  "body": f"{lane} is at the code step on {request.site}. Open the panel and type the code "
                          "the site sent you. It goes into the site only.",
                  "code_request_id": request.id, "lane": request.lane, "site": request.site,
                  "expires_ts": request.expires_at, "urgent": True})
        except Exception as e:  # noqa: BLE001
            log.warning("vault: code push not queued: %s", type(e).__name__)

    # ---- lifecycle ----
    def open(self, *, lane: str, site: str, entry: str, requester: dict[str, str], grant_path: str | None,
             window_seconds: float) -> tuple[CodeRequest, _Slot]:
        now = self._clock()
        request = CodeRequest(id=new_request_id(now), lane=lane, site=site, entry=entry, requester=requester,
                              grant_path=grant_path, created_at=now, expires_at=now + window_seconds)
        loop = asyncio.get_running_loop()
        slot = _Slot(request=request, loop=loop, closed=loop.create_future())
        with self._lock:
            self._slots[request.id] = slot
        self._publish(request)
        self._notify(request)
        return request, slot

    def list(self) -> list[CodeRequest]:
        now = self._clock()
        with self._lock:
            return [s.request for s in self._slots.values() if s.request.expires_at > now]

    def _take(self, request_id: str, family: str | None) -> _Slot:
        """The open slot, checked, out of the table (call with the lock held)."""
        slot = self._slots.get(request_id)
        if slot is None or slot.request.expires_at <= self._clock():
            raise CodeRequestClosed("This code request is closed. The login stopped waiting, or the code was "
                                    "already entered.")
        path = slot.request.grant_path
        if family is not None and path is not None and path.startswith(f"/{family}/"):
            raise CodeRefused("A code for this login must come from the owner or another approver, not from "
                              "the grant that asked for it.")
        return self._slots.pop(request_id)

    def _close(self, slot: _Slot, state: str, by: str | None) -> CodeRequest:
        request = slot.request
        request.state, request.closed_by, request.closed_at = state, by, self._clock()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is slot.loop:
            _wake(slot)
        else:
            try:
                slot.loop.call_soon_threadsafe(_wake, slot)
            except RuntimeError:  # the waiting loop is gone; nobody types this code
                slot.code = None
        self._publish(request)
        return request

    def submit(self, request_id: str, code: Secret, *, by: str, family: str | None = None) -> CodeRequest:
        """Accept the code once and wake the waiting login. `family` is the caller's root grant
        id when it acts under a token (None for the owner signed in to the panel)."""
        if not isinstance(code, Secret):
            raise InvalidRequest("A code must be passed as a Secret.")
        clean = normalize_code(code)
        with self._lock:
            slot = self._take(request_id, family)
            slot.code = clean
            request = self._close(slot, "used", by)
        log.info("vault: code request %s answered on %s", request.id, request.lane)
        return request

    def decline(self, request_id: str, *, by: str, family: str | None = None) -> CodeRequest:
        """The owner has no code to give (none arrived, or it was not them): the login stops."""
        with self._lock:
            slot = self._take(request_id, family)
            request = self._close(slot, "declined", by)
        log.info("vault: code request %s declined on %s", request.id, request.lane)
        return request

    async def wait(self, slot: _Slot, timeout: float) -> Secret | None:
        """The code, None when the window or the timeout ran out, CodeDeclined when the owner
        said no. Closes the request either way."""
        request = slot.request
        remaining = min(timeout, request.expires_at - self._clock())
        if self.max_wait is not None:
            remaining = min(remaining, self.max_wait)
        try:
            await asyncio.wait_for(asyncio.shield(slot.closed), timeout=max(0.0, remaining))
        except TimeoutError:
            pass
        except asyncio.CancelledError:
            self._abandon(slot, "cancelled")
            raise
        self._abandon(slot, "expired")
        if request.state == "declined":
            raise CodeDeclined("The owner dismissed the code card, so no code is coming for this login.")
        code, slot.code = slot.code, None
        return code if request.state == "used" else None

    def cancel_all(self) -> int:
        """Close every open request (the vault was sealed): their logins stop without a code."""
        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            self._abandon(slot, "cancelled")
        return len(slots)

    def _abandon(self, slot: _Slot, state: str) -> None:
        with self._lock:
            if self._slots.get(slot.request.id) is not slot:
                return
            del self._slots[slot.request.id]
            self._close(slot, state, None)


class _RelayPending:
    def __init__(self, relay: CodeRelay, request: CodeRequest, slot: _Slot, timeout: float):
        self.request = request
        self.request_id = request.id
        self._relay, self._slot, self._timeout = relay, slot, timeout

    async def wait(self) -> Secret | None:
        return await self._relay.wait(self._slot, self._timeout)

    async def cancel(self) -> None:
        self._relay._abandon(self._slot, "cancelled")


def _requester(ctx: Any) -> tuple[dict[str, str], str | None]:
    principal = getattr(ctx, "principal", None)
    who = {"kind": str(getattr(principal, "kind", "agent")), "id": str(getattr(principal, "id", "unknown"))}
    grant = getattr(ctx, "grant", None)
    path = getattr(grant, "path", None)
    if not isinstance(path, str):
        gid = getattr(principal, "grant_id", None)
        path = f"/{gid}/" if gid else None
    return who, path


class RelayCodeSource:
    """CodeSource "relay": open a code card for this lane and site, push the owner, wait."""

    kind = "relay"

    def __init__(self, relay: CodeRelay, cfg: dict[str, Any]):
        self.relay = relay
        self.window_seconds = float(cfg.get("window_seconds", 300))
        self.timeout_seconds = float(cfg.get("timeout_seconds", 300))

    async def arm(self, ctx: Any, entry: Any, *, lane_id: str, site: str, not_before: datetime) -> _RelayPending:
        requester, path = _requester(ctx)
        request, slot = self.relay.open(lane=lane_id, site=site, entry=entry.id, requester=requester,
                                        grant_path=path, window_seconds=self.window_seconds)
        return _RelayPending(self.relay, request, slot, self.timeout_seconds)
