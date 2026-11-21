"""keepalive.db migrations, found by openajar.core.migrate.discover() (and applied when the store opens)."""

from __future__ import annotations

import sqlite3

from openajar.core.migrate import Migration, MigrationSet

DB = "keepalive/keepalive.db"

SCHEMA_V1 = """
        CREATE TABLE IF NOT EXISTS lanes (
          lane_id     TEXT PRIMARY KEY,
          seed        TEXT NOT NULL,
          opted_out   INTEGER NOT NULL DEFAULT 0,
          status      TEXT,
          reason      TEXT,
          status_at   TEXT,
          notified    INTEGER NOT NULL DEFAULT 0,
          updated_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visits (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          lane_id     TEXT NOT NULL,
          started_at  TEXT NOT NULL,
          finished_at TEXT NOT NULL,
          trigger     TEXT NOT NULL,
          site        TEXT NOT NULL,
          outcome     TEXT NOT NULL,
          reason      TEXT,
          detail      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS visits_lane ON visits(lane_id, id)
"""


# node-wide values that outlive a restart: "not_before" (the gap after the last visit, ISO time)
SCHEMA_V2 = """
        CREATE TABLE IF NOT EXISTS meta (
          key         TEXT PRIMARY KEY,
          value       TEXT NOT NULL
        )
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


MIGRATIONS = MigrationSet(package="keepalive", db=DB, create=True,
                          migrations=(Migration(1, "lanes and visits", _v1), Migration(2, "node-wide values", _v2)))
