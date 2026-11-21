"""keepalive.db: a write lock held by another process never stalls the daemon's loop, and a
lane's row is read without taking the write lock (openajar.core.sqlite)."""

from __future__ import annotations

from datetime import UTC, datetime

from lockheld import held_lock, loop_stays_free

from openajar.keepalive.store import STARTED, KeepaliveStore, Visit


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
    store = KeepaliveStore(tmp_path)
    seed = store.lane("a/one").seed
    with held_lock(store.path) as lock:
        assert store.lane("a/one").seed == seed
        assert lock.still_held  # read while the other connection held the lock: it did not wait
