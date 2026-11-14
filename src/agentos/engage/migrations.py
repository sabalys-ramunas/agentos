"""engage.db migrations, found by agentos.ops.migrate.discover() (and applied when the store opens)."""

from __future__ import annotations

import sqlite3

from agentos.ops.migrate import Migration, MigrationSet

DB = "engage/engage.db"


SCHEMA_V1 = """
        CREATE TABLE IF NOT EXISTS programs (
          id          TEXT NOT NULL,
          version     INTEGER NOT NULL,
          hash        TEXT NOT NULL,
          surface     TEXT NOT NULL,
          body        TEXT NOT NULL,
          state       TEXT NOT NULL,
          created_at  TEXT NOT NULL,
          created_by  TEXT,
          approved_at TEXT,
          approved_by TEXT,
          starts_on   TEXT,
          PRIMARY KEY (id, version),
          UNIQUE (id, hash)
        );
        CREATE INDEX IF NOT EXISTS programs_surface ON programs(surface, state);
        CREATE TABLE IF NOT EXISTS personas (
          surface    TEXT PRIMARY KEY,
          seed       TEXT NOT NULL,
          redraws    INTEGER NOT NULL DEFAULT 0,
          body       TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plans (
          surface      TEXT NOT NULL,
          day          TEXT NOT NULL,
          program_hash TEXT,
          body         TEXT NOT NULL,
          created_at   TEXT NOT NULL,
          PRIMARY KEY (surface, day)
        );
        CREATE TABLE IF NOT EXISTS counters (
          surface TEXT NOT NULL,
          day     TEXT NOT NULL,
          kind    TEXT NOT NULL,
          planned INTEGER NOT NULL DEFAULT 0,
          done    INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (surface, day, kind)
        );
        CREATE TABLE IF NOT EXISTS caps (
          surface TEXT NOT NULL,
          kind    TEXT NOT NULL,
          value   INTEGER NOT NULL,
          set_by  TEXT NOT NULL,
          set_at  TEXT NOT NULL,
          PRIMARY KEY (surface, kind)
        );
        CREATE TABLE IF NOT EXISTS notices (
          id      INTEGER PRIMARY KEY AUTOINCREMENT,
          at      TEXT NOT NULL,
          surface TEXT,
          kind    TEXT NOT NULL,
          text    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


# Version 2 (brief B1, the program executor): what ran. `sessions` is one row per window that
# was opened, refused, missed or added by a reschedule; `ops` is one row per operation of a
# window, from planned to its outcome; `holds` are the engine's own holds on a surface (or one
# kind on it); `history` is a general per-surface log that later briefs also write and read.
SCHEMA_V2 = """
        CREATE TABLE IF NOT EXISTS sessions (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          surface       TEXT NOT NULL,
          day           TEXT NOT NULL,
          idx           INTEGER NOT NULL,
          origin        TEXT NOT NULL DEFAULT 'plan',
          planned_start TEXT NOT NULL,
          minutes       REAL NOT NULL DEFAULT 0,
          started_at    TEXT,
          ended_at      TEXT,
          status        TEXT NOT NULL DEFAULT 'scheduled',
          end_reason    TEXT,
          UNIQUE (surface, day, idx)
        );
        CREATE INDEX IF NOT EXISTS sessions_status ON sessions(status, planned_start);
        CREATE TABLE IF NOT EXISTS ops (
          id             INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id     INTEGER NOT NULL REFERENCES sessions(id),
          seq            INTEGER NOT NULL,
          surface        TEXT NOT NULL,
          day            TEXT NOT NULL,
          kind           TEXT NOT NULL,
          target         TEXT,
          status         TEXT NOT NULL DEFAULT 'planned',
          reason         TEXT,
          run_id         TEXT,
          rule_id        TEXT,
          content_origin TEXT,
          content_id     TEXT,
          moved_from     INTEGER,
          at             TEXT,
          UNIQUE (session_id, seq)
        );
        CREATE INDEX IF NOT EXISTS ops_day ON ops(surface, day, kind, status);
        CREATE INDEX IF NOT EXISTS ops_at ON ops(surface, kind, at);
        CREATE INDEX IF NOT EXISTS ops_run ON ops(run_id);
        CREATE TABLE IF NOT EXISTS holds (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          surface    TEXT NOT NULL,
          kind       TEXT,
          signal     TEXT,
          reason     TEXT NOT NULL,
          since      TEXT NOT NULL,
          until      TEXT,
          detail     TEXT,
          cleared_at TEXT,
          cleared_by TEXT
        );
        CREATE INDEX IF NOT EXISTS holds_surface ON holds(surface, cleared_at);
        CREATE TABLE IF NOT EXISTS history (
          id      INTEGER PRIMARY KEY AUTOINCREMENT,
          surface TEXT NOT NULL,
          at      TEXT NOT NULL,
          kind    TEXT NOT NULL,
          by      TEXT,
          payload TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS history_surface ON history(surface, at);
"""


# Version 3 (brief B4, the items inbox): approved content the owner adds from outside agentOS.
# An item waits `queued` until a window of its surface takes it (`reserved`, by that session's id),
# then is `used` (with the run id), `awaiting` (its run waits for an approval card), `failed` (the
# run failed and may have acted, so it is left for the owner) or handed back to `queued`; past
# `expires_at` it is `expired`, and the owner (or whoever asked for it, before it is used) can mark
# it `withdrawn`. Every item is submitted by the owner (`submitted_by`); `requested_by` is the
# principal that asked, when an agent asked and the owner approved the exact item (`approval_id`).
# `files` keeps what each attachment was when the item was approved (id, name, mime, size, sha256).
SCHEMA_V3 = """
        CREATE TABLE IF NOT EXISTS items (
          id                 TEXT PRIMARY KEY,
          surface            TEXT NOT NULL,
          kind               TEXT NOT NULL,
          text               TEXT NOT NULL,
          title              TEXT,
          targets            TEXT NOT NULL DEFAULT '[]',
          attachments        TEXT NOT NULL DEFAULT '[]',
          files              TEXT NOT NULL DEFAULT '[]',
          expires_at         TEXT NOT NULL,
          status             TEXT NOT NULL DEFAULT 'queued',
          submitted_by       TEXT NOT NULL,
          submitted_at       TEXT NOT NULL,
          requested_by       TEXT,
          approval_id        TEXT,
          reserved_by_window INTEGER,
          reserved_at        TEXT,
          used_run_id        TEXT,
          settled_at         TEXT
        );
        CREATE INDEX IF NOT EXISTS items_queue ON items(surface, kind, status, submitted_at);
        CREATE INDEX IF NOT EXISTS items_status ON items(status, expires_at);
        CREATE INDEX IF NOT EXISTS items_approval ON items(approval_id)
"""


# Version 4 (brief B3, the draft provider): candidate texts a configured provider wrote for a
# surface. One provider answer is a `batch`: the candidate a window takes is `pending` until the
# window settles it (`used`, with the run id, or `returned` to be offered again; `taken_at` is when
# a window took it, `offers` how many windows took it), and the others are kept as `alternative`
# for the owner to see. A draft whose run may have acted and did not go out is `failed` (never
# offered again). A used draft becomes `approved` once its run was verified, `denied` when the
# owner refused it, `expired` when its approval card expired; drafts nobody used become
# `expired`. No draft is ever approved content: approve mode always asks the owner.
SCHEMA_V4 = """
        CREATE TABLE IF NOT EXISTS drafts (
          id          TEXT PRIMARY KEY,
          surface     TEXT NOT NULL,
          kind        TEXT NOT NULL,
          batch       TEXT NOT NULL,
          text        TEXT NOT NULL,
          target      TEXT,
          status      TEXT NOT NULL DEFAULT 'pending',
          approval_id TEXT,
          run_id      TEXT,
          provider    TEXT NOT NULL,
          created_at  TEXT NOT NULL,
          taken_at    TEXT,
          offers      INTEGER NOT NULL DEFAULT 0,
          settled_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS drafts_queue ON drafts(surface, kind, status, created_at);
        CREATE INDEX IF NOT EXISTS drafts_batch ON drafts(batch)
"""


# Version 5 (brief B5, the daily digest): the digest reads a surface's notices and its settled
# items by time, so both get an index that starts with the surface.
SCHEMA_V5 = """
        CREATE INDEX IF NOT EXISTS notices_surface ON notices(surface, at);
        CREATE INDEX IF NOT EXISTS items_settled ON items(surface, settled_at)
"""


def _run(conn: sqlite3.Connection, schema: str) -> None:
    # one statement at a time: executescript would commit the migrator's transaction
    for statement in schema.split(";"):
        if statement.strip():
            conn.execute(statement)


def _v1(conn: sqlite3.Connection) -> None:
    _run(conn, SCHEMA_V1)


def _v2(conn: sqlite3.Connection) -> None:
    _run(conn, SCHEMA_V2)


def _v3(conn: sqlite3.Connection) -> None:
    _run(conn, SCHEMA_V3)


def _v4(conn: sqlite3.Connection) -> None:
    _run(conn, SCHEMA_V4)


def _v5(conn: sqlite3.Connection) -> None:
    _run(conn, SCHEMA_V5)


MIGRATIONS = MigrationSet(package="engage", db=DB, create=True,
                          migrations=(Migration(1, "programs, personas, plans and counters", _v1),
                                      Migration(2, "sessions, operations, holds and history", _v2),
                                      Migration(3, "the items inbox", _v3),
                                      Migration(4, "drafts from the draft provider", _v4),
                                      Migration(5, "indexes the daily digest reads by", _v5)))
