"""SQLite helpers: WAL, busy timeout, private files, explicit IMMEDIATE transactions.

One `DB` per file per process, on the shared base (agentos.core.sqlite): each thread keeps its
own connection, so a read on the event loop never waits behind a write another thread has open
(WAL: readers do not wait for writers, in this process or another). Other processes (the CLI
deciding an approval while the daemon runs) and other threads are serialized by SQLite itself:
`tx()` takes the write lock up front.

`lock` is kept for callers that read several statements as one snapshot in a thread (the audit
helpers take it); `tx()` does not take it, so a write waiting for another process's lock never
holds up a reader in this one.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentos.core.sqlite import SqliteStore


def _private(path: Path) -> None:
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            os.chmod(p, 0o600)
        except FileNotFoundError:
            pass


class DB(SqliteStore):
    row_factory = sqlite3.Row
    pragmas = ("synchronous=NORMAL",)

    def __init__(self, path: Path, schema: str, *, busy_timeout_ms: int = 5000):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        super().__init__(path, timeout=busy_timeout_ms / 1000, private=False)
        self.lock = threading.RLock()
        self._conn().executescript(schema)
        _private(path)

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's connection (kept open; agentos.core.sqlite)."""
        return self._conn()

    def tx(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        """An IMMEDIATE transaction: takes the write lock up front, so check-then-update
        sequences (budgets, standing approvals, decisions) are atomic across threads and
        processes. Not reentrant."""
        return self._tx()

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Cursor:
        return self._conn().execute(sql, params)

    def all(self, sql: str, params: Sequence[Any] | dict = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, params).fetchone()
