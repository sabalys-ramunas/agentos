"""keepalive.db: a write lock held by another process never stalls the daemon's loop, and a
lane's row is read without taking the write lock (agentos.core.sqlite)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from lockheld import _hold, loop_stays_free

from agentos.keepalive.store import STARTED, KeepaliveStore, Visit


async def test_status_and_visits_wait_for_the_lock_off_the_loop(tmp_path):
    store = KeepaliveStore(tmp_path)
    store.lane("a/one")
    now = datetime.now(UTC)

    async def visit():
        rec = await store.run(store.start_visit, Visit(lane_id="a/one", started_at=now, finished_at=now,
                                                       trigger="due", site="home", outcome=STARTED))
        rec.outcome = "ok"
        await store.run(store.finish_visit, rec)
        return await store.run(store.set_status, "a/one", "signed_out", "the site signed it out", notified=True)

    row = await loop_stays_free(visit, store.path)
    assert row.status == "signed_out" and row.notified
    other = KeepaliveStore(tmp_path)
    assert other.lane("a/one").status == "signed_out"
    assert [v.outcome for v in other.visits("a/one")] == ["ok"]


def test_reading_a_known_lane_never_waits_for_the_write_lock(tmp_path):
    import threading

    store = KeepaliveStore(tmp_path)
    seed = store.lane("a/one").seed
    ready, done = threading.Event(), threading.Event()
    threading.Thread(target=_hold, args=(store.path, 0.6, ready, done), daemon=True).start()
    assert ready.wait(5)
    t0 = time.monotonic()
    assert store.lane("a/one").seed == seed
    assert time.monotonic() - t0 < 0.2
    done.wait(5)
