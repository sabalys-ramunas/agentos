"""Live sockets: every long-lived connection a caller holds open on this node, so a change in what that
caller may do closes it (docs/API.md, "Live streams end when access does").

A socket is authorized when it opens; this registry keeps it honest after that. Each entry names the
grant, panel session, member and lane it was opened under, and carries a `recheck` the opener supplies
(re-authenticate from the original request, is the lane still in scope, is it frozen). The registry runs
every recheck:

- at once, when something that can end access happens: a grant revoked, a member changed or removed, a
  panel session deleted, a freeze (`poke()`, `ended(...)`, and the `grant`/`member`/`freeze`/`session`
  events on the bus);
- and on a heartbeat (`interval` seconds) for whatever changed without telling anyone.

Closing sets the entry's `closed` event; the opener's relay races its work against `wait_closed()`
and then closes the socket with `code` and `reason`.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("openajar.live")

SERVICE_NAME = "live_sockets"
CLOSE_REVOKED = 4401  # signed out, the session or grant is gone (4000 + HTTP 401)
CLOSE_DENIED = 4403   # still signed in, but no longer allowed here (frozen, out of scope)
POKE_KINDS = frozenset({"grant", "member", "freeze", "session"})
FAILURES_BEFORE_CLOSE = 3  # a recheck that keeps raising closes the socket (fail closed, not on one blip)

Recheck = Callable[[], Awaitable[tuple[int, str] | None]]
_ids = itertools.count(1)


@dataclass(eq=False)
class LiveEntry:
    kind: str                      # events | vnc | terminal | channel:<kind>
    recheck: Recheck               # None: still fine; (code, reason): close it
    lane: str | None = None
    grant_id: str | None = None
    sid: str | None = None
    member_id: str | None = None
    id: int = field(default_factory=lambda: next(_ids))
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    code: int | None = None
    reason: str | None = None
    failures: int = 0
    on_close: Callable[[LiveEntry], Any] | None = None

    def close(self, code: int, reason: str) -> None:
        if self.closed.is_set():
            return
        self.code, self.reason = code, reason[:120]
        self.closed.set()
        if self.on_close is not None:
            try:
                self.on_close(self)
            except Exception:
                log.exception("live: closing %s failed", self.kind)

    async def wait_closed(self) -> None:
        await self.closed.wait()


class LiveSockets:
    def __init__(self, *, bus: Any = None, interval: float = 5.0):
        self.bus = bus
        self.interval = interval
        self._entries: dict[int, LiveEntry] = {}
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._poked: asyncio.Event | None = None
        self._sweeping = asyncio.Lock()

    # ---- opening ----
    @contextlib.contextmanager
    def track(self, kind: str, recheck: Recheck, **keys: Any) -> Iterator[LiveEntry]:
        """`with live.track("terminal", recheck, lane=..., grant_id=...) as entry:` for the socket's life."""
        entry = self.add(LiveEntry(kind, recheck, **keys))
        try:
            yield entry
        finally:
            self.remove(entry)

    def add(self, entry: LiveEntry) -> LiveEntry:
        """Track it, and recheck at once: access may have ended between the opener's check and now."""
        self._entries[entry.id] = entry
        self._ensure_running()
        self._set_poked()
        return entry

    def remove(self, entry: LiveEntry) -> None:
        self._entries.pop(entry.id, None)

    def entries(self) -> list[LiveEntry]:
        return list(self._entries.values())

    # ---- closing ----
    def close_where(self, *, code: int, reason: str, grants: Iterable[str] = (), sid: str | None = None,
                    member_id: str | None = None, lanes: Iterable[str] = (), workspace: str | None = None,
                    kinds: Iterable[str] | None = None) -> int:
        """Close every entry that matches any one key given. Returns how many."""
        grant_set, lane_set = {g for g in grants if g}, set(lanes)
        kind_set = set(kinds) if kinds is not None else None
        n = 0
        for e in self.entries():
            if kind_set is not None and e.kind not in kind_set:
                continue
            hit = (e.grant_id is not None and e.grant_id in grant_set) or (sid is not None and e.sid == sid) \
                or (member_id is not None and e.member_id == member_id) or (e.lane is not None and e.lane in lane_set) \
                or (workspace is not None and e.lane is not None and e.lane.split("/", 1)[0] == workspace)
            if hit and not e.closed.is_set():
                e.close(code, reason)
                n += 1
        return n

    def ended(self, *, sid: str | None = None, member_id: str | None = None, partial: str = "") -> None:
        """Panel sessions were deleted: one (`sid`), every one of a member's, or some of a member's
        (`partial`: which ones is not known, so their sockets are rechecked). Safe from any thread."""
        def run() -> None:
            if sid is not None or (member_id is not None and not partial):
                self.close_where(code=CLOSE_REVOKED, reason="signed out", sid=sid,
                                 member_id=None if partial else member_id)
            self._set_poked()
        self._call_soon(run)

    def poke(self) -> None:
        """Something that can end access changed: recheck every socket now. Safe from any thread."""
        self._call_soon(self._set_poked)

    def on_event(self, kind: str, data: Any) -> None:
        """A bus event: close what it names outright, then recheck the rest."""
        if kind not in POKE_KINDS:
            return
        if kind == "grant" and isinstance(data, dict):
            ids = [data.get("id"), *(data.get("cascades_to") or []), *(data.get("grants") or [])]
            if data.get("state") in ("revoked", "frozen", "member_removed"):
                self.close_where(code=CLOSE_REVOKED, reason="the grant was revoked",
                                 grants=[i for i in ids if isinstance(i, str)])
        if kind == "member" and isinstance(data, dict) and data.get("removed") and isinstance(data.get("id"), str):
            self.close_where(code=CLOSE_REVOKED, reason="the member was removed", member_id=data["id"])
        self._set_poked()

    # ---- the heartbeat ----
    async def sweep(self) -> int:
        """Run every recheck; close what fails. Returns how many closed."""
        async with self._sweeping:
            n = 0
            for e in self.entries():
                if e.closed.is_set():
                    continue
                try:
                    verdict = await e.recheck()
                    e.failures = 0
                except Exception:
                    e.failures += 1
                    log.warning("live: recheck of a %s socket failed (%d in a row)", e.kind, e.failures,
                                exc_info=True)
                    verdict = (CLOSE_DENIED, "access could not be checked") \
                        if e.failures >= FAILURES_BEFORE_CLOSE else None
                if verdict is not None:
                    e.close(*verdict)
                    n += 1
            return n

    def _ensure_running(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._loop is not loop:  # a new loop (tests run several): start over on it
            self._loop, self._poked, self._task = loop, asyncio.Event(), None
            self._sweeping = asyncio.Lock()
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run(), name="openajar-live-sockets")

    async def _run(self) -> None:
        listener = asyncio.create_task(self._listen()) if self.bus is not None else None
        try:
            while self._entries:
                assert self._poked is not None
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._poked.wait(), self.interval)
                self._poked.clear()
                try:
                    await self.sweep()
                except Exception:
                    log.exception("live: sweep failed")
        finally:
            if listener is not None:
                listener.cancel()

    async def _listen(self) -> None:
        subscribe = getattr(self.bus, "subscribe", None)
        if not callable(subscribe):
            return
        async with subscribe() as sub:
            while True:
                event = await sub.get()
                try:
                    self.on_event(event.kind, event.data)
                except Exception:
                    log.exception("live: handling a %s event failed", event.kind)

    def _set_poked(self) -> None:
        if self._poked is not None:
            self._poked.set()

    def _call_soon(self, fn: Callable[[], Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fn()
            return
        with contextlib.suppress(RuntimeError):  # the loop closed meanwhile
            loop.call_soon_threadsafe(fn)


def of(runtime: Any) -> LiveSockets:
    """The node's registry, made and registered on first use."""
    live = runtime.get(SERVICE_NAME) if runtime is not None else None
    if live is None:
        live = LiveSockets(bus=runtime.get("events") if runtime is not None else None)
        if runtime is not None:
            runtime.register(SERVICE_NAME, live)
    elif live.bus is None and runtime.get("events") is not None:
        live.bus = runtime.get("events")
    return live
