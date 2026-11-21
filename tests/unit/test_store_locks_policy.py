"""A CLI write holding policy.db's or traces.db's write lock never freezes the daemon: authorize
(its counters, a new approval, a budget debit) runs on policy.db's worker thread and is
committed before it returns, and an audit row written on the loop waits for the lock off it
(openajar.core.sqlite)."""

from __future__ import annotations

import sqlite3

import pytest
from lockheld import loop_stays_free
from test_policy_support import Clock, make_service

from openajar.core.errors import ApprovalPending
from openajar.core.models import Effect
from openajar.policy.grants import OWNER_GRANT_ID
from openajar.policy.traces import TraceEvent, TraceStore


@pytest.fixture
def env(tmp_path):
    svc, _ = make_service(tmp_path, clock=Clock())
    yield svc
    svc.close()


def _other(path) -> sqlite3.Connection:  # another process's view
    return sqlite3.connect(path)


async def test_a_read_is_authorized_off_the_loop_while_the_cli_holds_the_lock(env):
    g, _ = env.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    ctx = env.context_for_grant(g.id, tool="browser_snapshot")
    d = await loop_stays_free(lambda: ctx.authorize(Effect.READ, lane_id="biz-a/social"), env.db.path)
    assert d.route == "auto"
    n = _other(env.db.path).execute("SELECT COUNT(*) FROM counters WHERE key LIKE '%act.read%'").fetchone()[0]
    assert n >= 1  # the read's meter landed before authorize returned


async def test_an_approval_is_saved_before_the_pending_answer_while_the_lock_is_held(env):
    env.ask_first.set("lane", "biz-a/social", True, by="owner", person=True)  # a commit waits for a person
    ctx = env.context_for_grant(OWNER_GRANT_ID, tool="flow_run")

    async def ask():
        with pytest.raises(ApprovalPending) as e:
            await ctx.authorize(Effect.COMMIT, lane_id="biz-a/social", content="post")
        return e.value.approval_id

    ap = await loop_stays_free(ask, env.db.path)
    row = _other(env.db.path).execute("SELECT state FROM approvals WHERE id = ?", (ap,)).fetchone()
    assert row == ("pending",)


async def test_a_charge_is_debited_off_the_loop_and_committed(env):
    g, _ = env.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT,
                     budget={"tokens_per_day": 1000})
    ctx = env.context_for_grant(g.id, tool="brain_run")
    await loop_stays_free(lambda: ctx.charge(tokens=300), env.db.path)
    used = _other(env.db.path).execute("SELECT SUM(used) FROM counters WHERE key LIKE '%:tok:%'").fetchone()[0]
    assert used == 300


async def test_an_audit_row_waits_for_the_lock_off_the_loop_and_lands(tmp_path):
    store = TraceStore(tmp_path / "traces.db", busy_timeout_ms=5000)
    try:
        async def write():
            ev = store.write(TraceEvent(kind="tool_start", tool="browser_snapshot", status="started"))
            assert store.deferred  # the lock is held: the row went to the worker, the loop went on
            await store.settled()
            return ev

        ev = await loop_stays_free(write, store.path)
        assert not store.deferred and store.pending_count == 0
        row = _other(store.path).execute("SELECT kind FROM traces WHERE id = ?", (ev.id,)).fetchone()
        assert row == ("tool_start",)
        assert store.db_failing_since is None and store.unwritten_since is None
    finally:
        store.close()


async def test_a_lock_held_past_the_timeout_still_starts_the_fail_closed_clock(tmp_path):
    store = TraceStore(tmp_path / "traces.db", busy_timeout_ms=100)
    try:
        async def write():
            store.write(TraceEvent(kind="tool_start", tool="browser_snapshot", status="started"))
            await store.settled()  # the worker waited 100 ms for the lock and gave up

        await loop_stays_free(write, store.path)
        assert store.db_failing_since is not None  # refused rows are counted as before (audit_health)
        assert await store.aflush() == 0  # the lock is free again: the tick writes them
        assert store.db_failing_since is None and store.unwritten_since is None
        assert _other(store.path).execute("SELECT COUNT(*) FROM traces WHERE kind = 'tool_start'").fetchone()[0] == 1
    finally:
        store.close()


async def test_rows_written_on_the_loop_keep_their_order_across_a_held_lock(tmp_path):
    store = TraceStore(tmp_path / "traces.db", busy_timeout_ms=5000)
    try:
        first = store.write(TraceEvent(kind="tool_start", tool="a", status="started"))

        async def more():
            evs = [store.write(TraceEvent(kind="tool_start", tool=f"t{i}", status="started")) for i in range(5)]
            await store.settled()
            return evs

        evs = await loop_stays_free(more, store.path)
        last = store.write(TraceEvent(kind="tool_end", tool="z", status="ok"))
        ids = [r[0] for r in _other(store.path).execute("SELECT id FROM traces ORDER BY seq")]
        assert ids == [first.id, *(e.id for e in evs), last.id]
        assert store.verify().status != "tampered"  # one chain, never forked
    finally:
        store.close()
