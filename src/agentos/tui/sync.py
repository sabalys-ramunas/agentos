"""Keeps a DashState in step with the node.

- A full load of every resource at start, each one independent: a 503 marks just that
  resource "not configured" and the rest still load.
- Live updates from /api/events, reconnecting with backoff. After a reconnect the
  state is reloaded, since events may have been missed while the stream was down.
- A poll loop: status and nodes every few seconds, everything every `full_every`
  seconds (or every poll while live updates are off).
- When the node stops answering the data stays on screen, marked stale, and the
  poll loop retries with backoff until it answers again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import timedelta
from typing import Any

from agentos.tui import fmt
from agentos.tui.client import ApiClient, ApiError, AuthFailed, NodeUnreachable, Unavailable
from agentos.tui.state import RESOURCES, DashState, Resource

log = logging.getLogger("agentos.tui")

LOADERS: dict[str, Callable[[ApiClient], Awaitable[Any]]] = {
    "status": lambda c: c.status(),
    "workspaces": lambda c: c.workspaces(),
    "nodes": lambda c: c.nodes(),
    "approvals": lambda c: c.approvals("pending"),
    "questions": lambda c: c.questions("open"),
    "activity": lambda c: c.activity(limit=300),
    "runs": lambda c: c.runs(limit=100),
    "schedules": lambda c: c.schedules(),
    "flows": lambda c: c.flows(),
    "brains": lambda c: c.brains(),
    "tasks": lambda c: c.brain_tasks(),
    "environments": lambda c: load_environments(c),
}

DETAILS_MAX = 6  # open environments whose git and proofs are read on each full load


async def load_environments(client: ApiClient) -> list[dict]:
    """Every environment, and for the open ones (a few at most) their git and kept proofs from the detail.
    A detail that does not answer leaves those fields empty, never the list."""
    items = await client.environments()
    for item in [i for i in items if isinstance(i, dict) and i.get("state") not in ("closed", "failed")][:DETAILS_MAX]:
        try:
            detail = await client.environment(str(item.get("id")))
        except (ApiError, NodeUnreachable):
            continue
        git = detail.get("git") or {}
        item["ahead"] = git.get("ahead")
        item["pushed"] = len(git.get("pushed") or [])
        item["proofs"] = len(detail.get("proofs") or []) if "proofs" in detail else None
    return items


class Syncer:
    def __init__(self, client: ApiClient, state: DashState, on_change: Callable[[set[str]], None], *,
                 status_every: float = 10.0, full_every: float = 30.0, max_backoff: float = 30.0,
                 first_backoff: float = 2.0):
        self.client = client
        self.state = state
        self.on_change = on_change
        self.status_every = status_every
        self.full_every = full_every
        self.max_backoff = max_backoff
        self.first_backoff = first_backoff
        self._backoff = first_backoff
        self._wake = asyncio.Event()
        self._sse_wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._last_full = 0.0

    # ---- lifecycle ----
    async def start(self) -> None:
        await self.refresh()
        self._tasks = [asyncio.create_task(self._poll_loop(), name="agentos-dash-poll"),
                       asyncio.create_task(self._sse_loop(), name="agentos-dash-sse")]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []

    def retry_now(self) -> None:
        """Wake both loops (the `r` key, or after an action)."""
        self._backoff = self.first_backoff
        self._wake.set()
        self._sse_wake.set()

    # ---- loading ----
    def _conn_key(self) -> tuple:
        c = self.state.conn
        return (c.online, c.auth_failed, c.live, c.reason)

    async def refresh(self, names: Iterable[str] = RESOURCES) -> bool:
        """Load the named resources concurrently. Returns False if the node did not answer."""
        names = list(names)
        before = self._conn_key()
        results = await asyncio.gather(*(self._load(n) for n in names))
        topics: set[str] = set()
        for t in results:
            topics |= t
        topics.discard("conn")
        if self._conn_key() != before:
            topics.add("conn")
        if set(names) >= set(RESOURCES):
            self._last_full = time.monotonic()
        self.on_change(topics or {"tick"})
        return self.state.conn.online is not False

    async def load(self, name: str) -> None:
        before = self._conn_key()
        topics = await self._load(name)
        self.on_change(topics | ({"conn"} if self._conn_key() != before else set()))

    async def _load(self, name: str) -> set[str]:
        res = self.state.res
        try:
            data = await LOADERS[name](self.client)
        except NodeUnreachable as exc:
            self.went_offline(exc.message)
            return {"conn"}
        except AuthFailed as exc:
            self.state.conn.auth_failed = True
            self.state.conn.online = True
            self.state.conn.reason = exc.message
            return {"conn"}
        except Unavailable as exc:
            res[name] = Resource("unavailable", exc.message, fmt.now_utc())
        except ApiError as exc:
            res[name] = Resource("denied" if exc.status == 403 else "error", exc.message, fmt.now_utc())
            self.came_online()
        except Exception as exc:  # noqa: BLE001 - a malformed answer must not take the dashboard down
            log.warning("loading %s failed: %s", name, type(exc).__name__)
            res[name] = Resource("error", "could not read the answer", fmt.now_utc())
        else:
            self.state.set(name, data)
            res[name] = Resource("ok", "", fmt.now_utc())
            self.came_online()
        return {name}

    def went_offline(self, reason: str) -> None:
        conn = self.state.conn
        changed = conn.online is not False
        conn.online = False
        conn.reason = reason
        if conn.live == "live":
            conn.live = "reconnecting"
        if changed:
            self._backoff = self.first_backoff
            self._wake.set()  # let the poll loop start its retry schedule now

    def came_online(self) -> None:
        conn = self.state.conn
        conn.online = True
        conn.auth_failed = False
        conn.reason = ""
        conn.retry_at = None
        conn.last_ok = fmt.now_utc()

    # ---- loops ----
    async def _sleep(self, event: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=seconds)
        event.clear()

    async def _poll_loop(self) -> None:
        while True:
            conn = self.state.conn
            if conn.online is False or conn.auth_failed:
                delay = self._backoff if conn.online is False else self.max_backoff
                self._backoff = min(self._backoff * 2, self.max_backoff)
                conn.retry_at = fmt.now_utc() + timedelta(seconds=delay)
                self.on_change({"conn"})
            else:
                delay = self.status_every
            await self._sleep(self._wake, delay)
            try:
                await self._poll_once()
            except Exception as exc:  # noqa: BLE001 - the poll loop must outlive any one bad poll
                log.warning("poll failed: %s", type(exc).__name__)

    async def _poll_once(self) -> None:
        conn = self.state.conn
        if conn.online is False or conn.auth_failed:
            await self.refresh(("status",))
            if conn.online and not conn.auth_failed:
                self._backoff = self.first_backoff
                await self.refresh()
                self._sse_wake.set()
            return
        stale = time.monotonic() - self._last_full >= self.full_every
        if stale or conn.live != "live":
            await self.refresh()
        else:
            await self.refresh(("status", "nodes"))

    async def _sse_loop(self) -> None:
        backoff = 1.0
        first = True
        while True:
            conn = self.state.conn
            if conn.online is False or conn.auth_failed:
                await self._sleep(self._sse_wake, self.max_backoff)
                continue
            wait = backoff
            try:
                async for ev in self.client.events(self.state.last_event_id):
                    if ev.event == "open":
                        conn.live = "live"
                        backoff = 1.0
                        if not first:
                            await self.refresh()
                        first = False
                        self.on_change({"conn"})
                        continue
                    try:
                        topics = self.state.apply_event(ev.event, ev.data, ev.id)
                    except Exception as exc:  # noqa: BLE001 - one odd event must not stop the stream
                        log.warning("event %s ignored: %s", ev.event, type(exc).__name__)
                        continue
                    if topics:
                        self.on_change(topics)
                conn.live = "reconnecting"
                wait = 1.0
            except Unavailable:
                conn.live = "polling"
                wait = 300.0
            except AuthFailed as exc:
                conn.auth_failed = True
                conn.reason = exc.message
                conn.live = "reconnecting"
            except ApiError:
                conn.live = "polling"
                wait = 300.0
            except NodeUnreachable:
                # a dropped stream is not proof the node is down; let the poll loop check
                conn.live = "reconnecting"
                self._wake.set()
            self.on_change({"conn"})
            await self._sleep(self._sse_wake, wait)
            backoff = min(backoff * 2, self.max_backoff)
