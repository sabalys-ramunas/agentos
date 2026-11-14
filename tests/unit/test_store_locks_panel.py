"""panel.db: a CLI holding its write lock never freezes the daemon. A session's touch on the
event loop never waits for the lock (it is left for the next request), and a sign-in, made on
the store's worker thread, is committed before the call returns (agentos.core.sqlite)."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from lockheld import loop_stays_free

from agentos.api.auth import PanelStore
from agentos.api.errors import ApiError


async def test_a_sign_in_waits_for_the_lock_off_the_loop_and_is_saved(tmp_path):
    store = PanelStore(tmp_path / "panel.db")
    try:
        _, session = await loop_stays_free(
            lambda: store.run(store.create_session, "owner", 3600, member_id="owner"), store.path)
        n = sqlite3.connect(store.path).execute("SELECT COUNT(*) FROM sessions WHERE id_hash = ?",
                                                (session.id_hash,)).fetchone()[0]
        assert n == 1  # another process sees it at once
    finally:
        store.close()


async def test_a_session_read_and_its_touch_never_wait_on_the_loop(tmp_path):
    store = PanelStore(tmp_path / "panel.db")
    try:
        sid, _ = store.create_session("owner", 3600, member_id="owner")
        store._db.execute("UPDATE sessions SET last_seen_at = 0")  # due for a touch
        store._db.commit()

        async def read():
            s = store.get_session(sid)  # the lock is held now
            await asyncio.sleep(0.05)
            return s

        s = await loop_stays_free(read, store.path)
        assert s is not None and s.principal_id == "owner"  # the read went on; the touch was skipped
        assert store.get_session(sid) is not None  # and lands on the next request
        seen = sqlite3.connect(store.path).execute("SELECT last_seen_at FROM sessions").fetchone()[0]
        assert seen > 0
    finally:
        store.close()


async def test_a_sign_out_waits_off_the_loop_and_is_gone_before_it_returns(tmp_path):
    store = PanelStore(tmp_path / "panel.db")
    try:
        sid, _ = store.create_session("owner", 3600, member_id="owner")
        await loop_stays_free(lambda: store.run(store.delete_session, sid), store.path)
        assert sqlite3.connect(store.path).execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert store.get_session(sid) is None
    finally:
        store.close()


async def test_a_refused_write_on_the_worker_never_keeps_the_lock(tmp_path):
    store = PanelStore(tmp_path / "panel.db")
    try:
        await store.run(store.create_workspace, id="biz-a", name="Biz A", color=None)
        with pytest.raises(ApiError) as e:
            await store.run(store.create_workspace, id="biz-a", name="Biz A", color=None)
        assert e.value.code == "conflict"
        other = sqlite3.connect(store.path, timeout=0.2)
        other.execute("BEGIN IMMEDIATE")  # the worker's failed insert left no transaction open
        other.execute("ROLLBACK")
    finally:
        store.close()
