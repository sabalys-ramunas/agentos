"""Seats: concurrency, rolling window, cooldown backoff, persistence across restarts."""

from datetime import UTC, datetime

from openajar.brains.models import BrainSpec, BrainState, TaskRecord, TaskStatus
from openajar.brains.seats import SeatManager
from openajar.brains.settings import BrainsSettings
from openajar.brains.store import Store


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def setup(tmp_path, **kw):
    clock = Clock()
    store = Store(tmp_path / "brains.db")
    seats = SeatManager(store, BrainsSettings(cooldown_base_seconds=60, cooldown_max_seconds=3600, **kw), clock)
    spec = BrainSpec.template("claude-max", "subscription", "anthropic", seats=1)
    store.put_brain(spec)
    return store, seats, spec, clock


def task(store, tid):
    rec = TaskRecord(id=tid, workspace="w", principal_id="p", queued_since=datetime.now(UTC))
    store.insert_task(rec)
    return rec


def test_one_seat_two_tasks(tmp_path):
    store, seats, spec, _ = setup(tmp_path)
    a, b = task(store, "bt_000000000001"), task(store, "bt_000000000002")
    assert seats.try_acquire(spec, a, 1, tmp_path / "a")
    assert seats.in_use("claude-max") == 1
    assert not seats.try_acquire(spec, b, 1, tmp_path / "b")
    assert store.get_task(b.id).status == TaskStatus.QUEUED and b.status == TaskStatus.QUEUED
    a.status = TaskStatus.SUCCEEDED_UNVERIFIED
    store.save_task(a)
    assert seats.try_acquire(spec, b, 1, tmp_path / "b")


def test_acquire_only_from_queued(tmp_path):
    store, seats, spec, _ = setup(tmp_path)
    a = task(store, "bt_000000000003")
    a.status = TaskStatus.CANCELLED
    store.save_task(a)
    a.status = TaskStatus.QUEUED  # stale copy in memory
    assert not seats.try_acquire(spec, a, 1, tmp_path / "a")
    assert store.get_task(a.id).status == TaskStatus.CANCELLED


def test_seats_are_shared_across_processes(tmp_path):
    store, seats, spec, clock = setup(tmp_path)
    a = task(store, "bt_000000000004")
    assert seats.try_acquire(spec, a, 1, tmp_path / "a")
    other = SeatManager(Store(tmp_path / "brains.db"), BrainsSettings(), clock)  # e.g. the CLI
    b = task(store, "bt_000000000005")
    assert not other.try_acquire(spec, b, 1, tmp_path / "b")


def test_cooldown_backoff_and_reset(tmp_path):
    store, seats, spec, clock = setup(tmp_path)
    u1 = seats.mark_rate_limited(spec, resets_at=None, detail="429")
    assert u1 == clock.t + 60
    assert seats.override("claude-max")[0] == BrainState.COOLING_DOWN
    u2 = seats.mark_rate_limited(spec, resets_at=None, detail="429")
    assert u2 == clock.t + 120  # doubled
    clock.t += 121
    assert seats.override("claude-max")[0] is None  # expired on its own
    seats.mark_success("claude-max")
    assert store.brain_state("claude-max")["backoff_level"] == 0


def test_cooldown_uses_reported_reset_time(tmp_path):
    _store, seats, spec, clock = setup(tmp_path)
    until = seats.mark_rate_limited(spec, resets_at=clock.t + 5 * 3600, detail="usage limit")
    assert until == clock.t + 5 * 3600


def test_window_capacity_is_learned_from_a_429(tmp_path):
    store, seats, spec, clock = setup(tmp_path)
    seats.record_usage("claude-max", "t1", 800_000, 0)
    assert seats.window_pct(spec) is None  # unknown until we learn it
    seats.mark_rate_limited(spec, resets_at=None, detail="limit")
    assert store.brain_state("claude-max")["window_capacity"] == 800_000
    assert seats.window_pct(spec) == 100.0 and seats.window_saturated(spec)
    clock.t += spec.window_hours * 3600 + 1  # the window rolls
    assert seats.window_pct(spec) == 0.0 and not seats.window_saturated(spec)


def test_owner_set_window_and_reported_utilization(tmp_path):
    _store, seats, spec, _clock = setup(tmp_path)
    spec.window_tokens = 1_000_000
    seats.record_usage("claude-max", "t1", 400_000, 0)
    assert seats.window_pct(spec) == 40.0
    spec.window_tokens = None
    seats.note_utilization("claude-max", 0.97)
    assert seats.window_pct(spec) == 97.0 and seats.window_saturated(spec)


def test_needs_auth_persists_until_cleared(tmp_path):
    _store, seats, _spec, clock = setup(tmp_path)
    seats.mark_needs_auth("claude-max", "401 token expired")
    again = SeatManager(Store(tmp_path / "brains.db"), BrainsSettings(), clock)  # after a restart
    state, reason, _ = again.override("claude-max")
    assert state == BrainState.NEEDS_AUTH and "expired" in reason
    again.clear_auth("claude-max")
    assert again.override("claude-max")[0] is None


def test_state_survives_restart(tmp_path):
    store, seats, spec, clock = setup(tmp_path)
    seats.mark_rate_limited(spec, resets_at=None, detail="429")
    store.close()
    store2 = Store(tmp_path / "brains.db")
    seats2 = SeatManager(store2, BrainsSettings(), clock)
    assert seats2.override("claude-max")[0] == BrainState.COOLING_DOWN
