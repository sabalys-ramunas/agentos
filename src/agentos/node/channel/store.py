"""The channel's own SQLite file (`AGENTOS_HOME/channel.db`, WAL, mode 600).

- `outbox`: every `event`, `traces` and `approval_request` the node has not seen acked, as
  the exact frame it sends (so a redelivery keeps its `id` and `seq`). `seq` is one
  node-wide counter, starting at 1, stored in `meta`, never reset.
- `calls`: the at-most-once cache of `invoke` results per call_id (5 minutes).
- `waiting`: the tool and an argument digest of each call waiting for approval, so a retry
  after a restart runs that call once it is approved (the retry brings the grant token again).
- `approvals`: the last state of each approval the controller was told about.
- `meta`: small JSON values (the counter, the controller's config, degraded_since, and
  `epoch`, made with the file: a node whose channel.db was lost starts its seq at 1 again under
  a new epoch, which it announces in `hello` so the controller resets its dedupe).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentos.node.channel import wire

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
  seq INTEGER PRIMARY KEY, type TEXT NOT NULL, frame TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
  call_id TEXT PRIMARY KEY, state TEXT NOT NULL, tool TEXT, result TEXT, approval_id TEXT,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, state TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS waiting (
  call_id TEXT PRIMARY KEY, tool TEXT NOT NULL, args_digest TEXT NOT NULL, at REAL NOT NULL
);
"""

MAX_OUTBOX = 100_000  # past this, the oldest traces are dropped first (events and approvals are kept)
DB_NAME = "channel.db"


@dataclass
class CachedCall:
    call_id: str
    state: str          # running | done | waiting (approval pending)
    tool: str | None
    result: dict | None
    approval_id: str | None
    updated_at: float


class ChannelStore:
    def __init__(self, path: Path, *, clock=time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self._db.executescript(SCHEMA)
        if self.get_meta("epoch") is None:
            self.set_meta("epoch", wire.prefixed("ep"))

    @classmethod
    def for_home(cls, home: Path) -> ChannelStore:
        return cls(home / DB_NAME)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- meta ----
    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            if value is None:
                self._db.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                self._db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) "
                                 "DO UPDATE SET value = excluded.value", (key, json.dumps(value)))

    # ---- outbox ----
    def enqueue(self, kind: str, body: wire.Json) -> int:
        """Persist one message with the next seq. Raises wire.Oversize past 1 MiB (nothing stored)."""
        if kind not in wire.OUTBOX_TYPES:
            raise ValueError(f"{kind} does not go through the outbox")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT value FROM meta WHERE key = 'next_seq'").fetchone()
                seq = int(json.loads(row["value"])) if row else 1
                frame = wire.Envelope(kind, body, seq=seq).encode()
                self._db.execute("INSERT INTO outbox (seq, type, frame, created_at) VALUES (?, ?, ?, ?)",
                                 (seq, kind, frame, self.clock()))
                self._db.execute("INSERT INTO meta (key, value) VALUES ('next_seq', ?) ON CONFLICT(key) "
                                 "DO UPDATE SET value = excluded.value", (json.dumps(seq + 1),))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._cap_locked()
        return seq

    def _cap_locked(self) -> None:
        depth = self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        if depth <= MAX_OUTBOX:
            return
        self._db.execute("DELETE FROM outbox WHERE seq IN (SELECT seq FROM outbox WHERE type = 'traces' "
                         "ORDER BY seq LIMIT ?)", (depth - MAX_OUTBOX,))

    def outbox_after(self, seq: int, limit: int = 100) -> list[tuple[int, str]]:
        with self._lock:
            rows = self._db.execute("SELECT seq, frame FROM outbox WHERE seq > ? ORDER BY seq LIMIT ?",
                                    (seq, limit)).fetchall()
        return [(r["seq"], r["frame"]) for r in rows]

    def trim(self, upto: int) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM outbox WHERE seq <= ?", (upto,)).rowcount

    def depth(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def oldest_seq(self) -> int | None:
        with self._lock:
            row = self._db.execute("SELECT MIN(seq) FROM outbox").fetchone()
        return row[0]

    @property
    def high_seq(self) -> int:
        return int(self.get_meta("next_seq", 1)) - 1

    @property
    def epoch(self) -> str:
        """The life of this file's seq counter."""
        return str(self.get_meta("epoch"))

    # ---- the at-most-once cache ----
    def get_call(self, call_id: str) -> CachedCall | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        if row is None:
            return None
        return CachedCall(row["call_id"], row["state"], row["tool"],
                          json.loads(row["result"]) if row["result"] else None, row["approval_id"],
                          row["updated_at"])

    def put_call(self, call_id: str, state: str, *, tool: str | None = None, result: dict | None = None,
                 approval_id: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO calls (call_id, state, tool, result, approval_id, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(call_id) DO UPDATE SET state = excluded.state, tool = COALESCE(excluded.tool, tool), "
                "result = excluded.result, approval_id = excluded.approval_id, updated_at = excluded.updated_at",
                (call_id, state, tool, json.dumps(result) if result is not None else None, approval_id,
                 self.clock()))

    def running_calls(self) -> list[str]:
        with self._lock:
            return [r["call_id"] for r in self._db.execute("SELECT call_id FROM calls WHERE state = 'running'")]

    def purge_calls(self, ttl: float = wire.RESULT_TTL) -> int:
        with self._lock:
            self._db.execute("DELETE FROM waiting WHERE at < ?", (self.clock() - ttl,))
            return self._db.execute("DELETE FROM calls WHERE state != 'running' AND updated_at < ?",
                                    (self.clock() - ttl,)).rowcount

    def put_waiting(self, call_id: str, tool: str, args_digest: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO waiting (call_id, tool, args_digest, at) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(call_id) DO UPDATE SET tool = excluded.tool, "
                             "args_digest = excluded.args_digest, at = excluded.at",
                             (call_id, tool, args_digest, self.clock()))

    def waiting_call(self, call_id: str) -> tuple[str, str] | None:
        """(tool, argument digest) of a call waiting for approval."""
        with self._lock:
            row = self._db.execute("SELECT tool, args_digest FROM waiting WHERE call_id = ?", (call_id,)).fetchone()
        return (row["tool"], row["args_digest"]) if row else None

    def drop_waiting(self, call_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM waiting WHERE call_id = ?", (call_id,))

    # ---- approvals the controller was told about ----
    def approval_state(self, approval_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT state FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return row["state"] if row else None

    def set_approval_state(self, approval_id: str, state: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO approvals (id, state, at) VALUES (?, ?, ?) ON CONFLICT(id) "
                             "DO UPDATE SET state = excluded.state, at = excluded.at", (approval_id, state, self.clock()))
