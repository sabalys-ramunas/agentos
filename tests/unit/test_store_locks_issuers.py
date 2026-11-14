"""grant_issuers.db: a CLI holding its write lock never freezes the daemon. A revocation from the
controller takes effect in memory at once and is on disk before the handler answers, waiting
for the lock off the event loop (agentos.core.sqlite)."""

from __future__ import annotations

import asyncio
import sqlite3

from lockheld import loop_stays_free

from agentos.policy.issuers import DB_NAME, TrustedIssuers


async def test_a_revocation_is_refused_at_once_and_saved_off_the_loop(tmp_path):
    issuers = TrustedIssuers(tmp_path)
    try:
        async def revoke():
            assert issuers.revoke(["rid-1", "rid-2"]) == ["rid-1", "rid-2"]
            assert issuers.is_revoked(["rid-1"])  # in effect before the row lands
            await issuers.flush()

        await loop_stays_free(revoke, tmp_path / DB_NAME)
        rows = sqlite3.connect(tmp_path / DB_NAME).execute("SELECT id FROM revoked ORDER BY id").fetchall()
        assert rows == [("rid-1",), ("rid-2",)]
        assert TrustedIssuers(tmp_path).is_revoked(["rid-2"])  # and after a restart
    finally:
        issuers.close()


async def test_a_revocation_off_the_loop_is_committed_before_it_returns(tmp_path):
    issuers = TrustedIssuers(tmp_path)
    try:
        await loop_stays_free(lambda: asyncio.to_thread(issuers.revoke, ["rid-9"]), tmp_path / DB_NAME)
        assert not issuers.queued
        assert sqlite3.connect(tmp_path / DB_NAME).execute("SELECT COUNT(*) FROM revoked").fetchone()[0] == 1
    finally:
        issuers.close()


async def test_the_controllers_revoke_writes_again_what_a_queued_write_lost(tmp_path):
    issuers = TrustedIssuers(tmp_path)
    try:
        issuers.revoke(["rid-1"])  # on the loop: in memory, its row written (or queued)
        issuers._db.execute("DELETE FROM revoked")  # as if that write had failed
        await loop_stays_free(lambda: issuers.run(issuers.save_revoked, ["rid-1"]), tmp_path / DB_NAME)
        assert sqlite3.connect(tmp_path / DB_NAME).execute("SELECT id FROM revoked").fetchall() == [("rid-1",)]
    finally:
        issuers.close()
