"""studio.db migrations (found by agentos.core.migrate.discover() through agentos.studio.migrations,
and applied when the store opens). Append only: a released migration never changes."""

from __future__ import annotations

import sqlite3

from agentos.core.migrate import Migration, MigrationSet

DB = "studio/studio.db"


def _run(conn: sqlite3.Connection, schema: str) -> None:
    # one statement at a time: executescript would commit the migrator's transaction
    for statement in schema.split(";"):
        if statement.strip():
            conn.execute(statement)


def _v1(conn: sqlite3.Connection) -> None:
    _run(conn, """
        CREATE TABLE IF NOT EXISTS blobs (
          id          TEXT PRIMARY KEY,
          media       TEXT NOT NULL,
          bytes       INTEGER NOT NULL,
          created_at  REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS captures (
          id          TEXT PRIMARY KEY,
          url         TEXT NOT NULL,
          body        TEXT NOT NULL,
          pinned      TEXT,
          created_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS captures_url ON captures(url, created_at);
        CREATE TABLE IF NOT EXISTS capture_blobs (
          capture_id  TEXT NOT NULL,
          blob_id     TEXT NOT NULL,
          PRIMARY KEY (capture_id, blob_id)
        );
        CREATE INDEX IF NOT EXISTS capture_blobs_blob ON capture_blobs(blob_id);
        CREATE TABLE IF NOT EXISTS baselines (
          url         TEXT NOT NULL,
          profile     TEXT NOT NULL,
          capture_id  TEXT NOT NULL,
          set_by      TEXT,
          set_at      REAL NOT NULL,
          PRIMARY KEY (url, profile)
        )
    """)


MIGRATIONS = MigrationSet(package="studio", db=DB, create=True,
                          migrations=(Migration(1, "captures, their blobs by content hash, pins and baselines", _v1),))
