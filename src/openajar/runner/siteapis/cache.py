"""The result cache of site API reads (docs/SITE-APIS.md, Cache).

    key      (flow id, implementation hash, surface, its binding, canonical params): a new version
             misses, and so does a surface bound to another lane or account
    value    a run's typed output with its labels: `untrusted_fields` stay with the value
    bound    by bytes, least recently used out first; one result over `max_entry` is not kept
    where    on the node only: nothing here ever goes to a relay

Runs fill it, not requests: `record` is a runner on_finished hook, so a call that answered 202 still
leaves its result. Only a successful read of a local API with `cache_s` above 0 is kept: an endpoint
with `cache_s` 0 is one the owner wants read every time, so it keeps nothing, not even for `stale_ok`.
A try never touches the cache.
A result below `expect.min_items` is kept neither: it looks like a site that changed, and must not
replace the last good one.

`Flights` is how concurrent misses share one run: the first caller starts it and the others wait on
the same run id. A run that outlives its caller stays joinable until it ends.

This module answers "what is stored". Whether a caller may have it is decided first, in
openajar.runner.siteapis.http, on every call: a hit is never a way around the key, its scope, a pause
or a freeze.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from openajar.core.errors import NotFound
from openajar.core.models import Effect
from openajar.runner.flowdoc import FlowVersion
from openajar.runner.names import is_connector_id
from openajar.runner.owner import TRY_SOURCE
from openajar.runner.records import SUCCEEDED, Run
from openajar.runner.siteapis.http_store import HttpStore
from openajar.runner.util import Clock

log = logging.getLogger("openajar.runner")

MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRY_BYTES = 2 * 1024 * 1024
TOUCH_S = 60.0  # how stale a result's used_at may get: an LRU needs no more than this
STALE_MAX_S = 7 * 86400  # the oldest result `stale_ok` answers with


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def cache_key(flow_id: str, version: str, surface: str, lane: str, params: Mapping[str, Any]) -> str:
    """Of a run's full params after validation, so a default and a value left out are one entry. Those
    hold the binding (who it acts as, the account's language), and with the lane that is who the read
    ran as: a surface bound to another login or account misses. The kind it acts as is the version's."""
    return hashlib.sha256(canonical([flow_id, version, surface, lane, params]).encode()).hexdigest()


def http_of(fv: FlowVersion) -> dict[str, Any]:
    return (fv.definition.connector or {}).get("http") or {}


def is_local(fv: FlowVersion) -> bool:
    return (fv.saved_by or {}).get("kind") == "local" and bool(fv.definition.connector)


def below_expected(fv: FlowVersion, data: Mapping[str, Any] | None) -> bool:
    """Fewer rows than `expect.min_items` in any output: the site probably changed."""
    for name, least in ((http_of(fv).get("expect") or {}).get("min_items") or {}).items():
        have = (data or {}).get(name)
        if not isinstance(have, list) or len(have) < int(least):
            return True
    return False


@dataclass(frozen=True)
class Entry:
    key: str
    value: dict[str, Any]   # data, status, untrusted_fields, private_fields, truncated, version, read_at, run
    stored_at: float

    def age(self, now: float) -> int:
        return max(0, int(now - self.stored_at))


class SiteCache:
    def __init__(self, store: HttpStore, flows: Any, *, clock: Clock | None = None, max_bytes: int = MAX_BYTES,
                 max_entry: int = MAX_ENTRY_BYTES, paused: Callable[[str], bool] = lambda _api: False):
        self.store, self.flows, self.paused = store, flows, paused
        self.clock = clock or Clock()
        self.max_bytes, self.max_entry = max_bytes, max_entry
        self.flights = Flights()
        self._drops: dict[str, int] = {}  # per API: how many times its results were dropped

    def _now(self) -> float:
        return self.clock.now().timestamp()

    def get(self, key: str) -> Entry | None:
        row = self.store.cache_get(key)
        if row is None:
            return None
        now = self._now()
        if now - row["used_at"] >= TOUCH_S:
            self.store.soon(self.store.cache_touch, key, now)  # a touch: never waits for a lock
        return Entry(key, json.loads(row["value"]), row["stored_at"])

    def fresh(self, key: str, ttl: float) -> Entry | None:
        """The stored result, when it is at most `ttl` seconds old."""
        entry = self.get(key) if ttl > 0 else None
        return entry if entry is not None and self._now() - entry.stored_at <= ttl else None

    def record(self, run: Run) -> None:
        """The runner's on_finished hook. Never raises: a run is done whatever the cache does."""
        try:
            self._record(run)
        except Exception:
            log.exception("run %s: could not store its result in the site API cache", run.id)

    def _record(self, run: Run) -> None:
        if not run.flow_id or not is_connector_id(run.flow_id) or not run.surface or run.source == TRY_SOURCE:
            return  # a try is of a draft: it says nothing of what is live, and storing it would evict that
        try:
            fv = self.flows.get(run.flow_id, run.flow_version)
        except NotFound:
            return
        if not is_local(fv):
            return
        key = cache_key(fv.id, fv.version, run.surface, run.lane or "", run.params)  # as it ran, whatever the surface is now
        self.flights.done(key)
        if fv.effect != Effect.READ or not http_of(fv).get("cache_s") or run.status not in SUCCEEDED or not run.output:
            return
        out = run.output
        meta = fv.definition.connector or {}
        # a read that was in the air when a person paused or removed its API must not put it back
        if below_expected(fv, out.get("data")) or self.paused(meta["connector"]) or self.flows.store.get_flow(fv.id) is None:
            return
        at = run.finished_at or self.clock.now()
        value = canonical({"data": out.get("data"), "status": str(run.status), "truncated": bool(out.get("truncated")),
                           "untrusted_fields": out.get("untrusted_fields") or [],
                           "private_fields": out.get("private_fields") or [], "version": meta.get("version"),
                           "read_at": at.isoformat(), "run": run.id})
        size = len(value.encode())
        if size > self.max_entry or size > self.max_bytes:
            return
        now = at.timestamp()
        api = meta["connector"]
        row = {"key": key, "api": api, "flow_id": fv.id, "version": fv.version, "surface": run.surface,
               "value": value, "bytes": size, "stored_at": now, "used_at": now}
        self.store.soon(self._put_unless_dropped, row, self._drops.get(api, 0))

    def _put_unless_dropped(self, row: dict[str, Any], drops: int) -> None:
        """The put itself (queued when another process holds the lock). Skipped when the API's
        results were dropped since the read finished, as checked under the write lock (the drop
        counts first, then waits for the lock): a queued put never puts a paused API back."""
        self.store.cache_put(row, max_bytes=self.max_bytes, still=lambda: self._drops.get(row["api"], 0) == drops)

    def drop_api(self, api: str) -> int:
        """Delete every result of an API: it was paused or removed. A put still queued or waiting for
        the lock is skipped (_put_unless_dropped); one that already holds the lock ends first."""
        self._drops[api] = self._drops.get(api, 0) + 1
        return self.store.cache_delete(api=api)


class Flights:
    """Runs in the air, by cache key: run ids, or a future while the first caller starts one."""

    def __init__(self) -> None:
        self._live: dict[str, asyncio.Future[str]] = {}

    async def share(self, key: str, start: Callable[[], Awaitable[str]], alive: Callable[[str], bool]) -> tuple[str, bool]:
        """(run id, this caller started it). A caller that joined a start that failed starts its own:
        the failure was the first caller's, under its own grant."""
        while True:
            fut = self._live.get(key)
            if fut is None:
                return await self._lead(key, start), True
            if not fut.done():
                try:
                    return await asyncio.shield(fut), False
                except asyncio.CancelledError:  # the first caller's start was cancelled, or this one is
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise
                except Exception:  # noqa: BLE001 - its failure was its own, under its grant: start ours
                    log.debug("a shared start failed; the next caller starts its own")
                continue
            if not fut.cancelled() and fut.exception() is None and alive(fut.result()):
                return fut.result(), False
            if self._live.get(key) is fut:
                del self._live[key]

    async def _lead(self, key: str, start: Callable[[], Awaitable[str]]) -> str:
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._live[key] = fut
        try:
            run_id = await start()
        except BaseException as e:
            if self._live.get(key) is fut:
                del self._live[key]
            if isinstance(e, Exception):
                fut.set_exception(e)
                fut.exception()  # read here, so a start nobody joined is not logged as unhandled
            else:
                fut.cancel()
            raise
        fut.set_result(run_id)
        return run_id

    def done(self, key: str) -> None:
        self._live.pop(key, None)
