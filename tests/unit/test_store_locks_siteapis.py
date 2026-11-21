"""siteapis/http.db: a request's usage row, key touch and cached result never wait for a lock
another process holds, and dropping an API's results stays behind a queued put
(openajar.core.sqlite)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from lockheld import _hold, loop_stays_free

from openajar.runner.fakes import FakeClock
from openajar.runner.siteapis.cache import SiteCache
from openajar.runner.siteapis.http_store import HttpStore
from openajar.runner.siteapis.usage import Usage

START = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)


def _put(store: HttpStore, key: str = "k1") -> None:
    store.soon(store.cache_put, {"key": key, "api": "shop", "flow_id": "f", "version": 1, "surface": "a/b",
                                 "value": "{}", "bytes": 2, "stored_at": 1.0, "used_at": 1.0}, max_bytes=1 << 20)


async def test_usage_rows_and_cache_puts_are_queued_while_the_lock_is_held(tmp_path):
    store = HttpStore(tmp_path)
    usage = Usage(store, clock=FakeClock(START))

    async def request():
        usage.record(key_id="key_1", api="shop", endpoint="orders", surface="a/b", status=200, code=None,
                     duration_ms=5, cached=False)
        _put(store)
        await store.flush()

    await loop_stays_free(request, store.path)
    other = HttpStore(tmp_path)
    assert len(other.usage_rows(0, START.timestamp() + 10)) == 1
    assert other.cache_get("k1") is not None


def test_a_put_queued_behind_the_lock_never_brings_a_dropped_api_back(tmp_path):
    store = HttpStore(tmp_path)
    cache = SiteCache(store, flows=None)
    ready, done = threading.Event(), threading.Event()
    threading.Thread(target=_hold, args=(store.path, 0.4, ready, done), daemon=True).start()
    assert ready.wait(5)
    store.soon(cache._put_unless_dropped, {"key": "k1", "api": "shop", "flow_id": "f", "version": 1,
                                           "surface": "a/b", "value": "{}", "bytes": 2, "stored_at": 1.0,
                                           "used_at": 1.0}, 0)
    assert store.queued  # the lock is held: queued
    cache.drop_api("shop")  # the owner paused the API meanwhile
    done.wait(5)
    store.close()  # finishes the queue
    assert HttpStore(tmp_path).cache_get("k1") is None
