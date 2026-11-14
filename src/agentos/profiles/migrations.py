"""profiles.db migrations, found by agentos.core.migrate.discover() (and applied when the store opens)."""

from __future__ import annotations

import sqlite3

from agentos.core.migrate import Migration, MigrationSet

DB = "profiles/profiles.db"


def _run(conn: sqlite3.Connection, schema: str) -> None:
    # one statement at a time: executescript would commit the migrator's transaction
    for statement in schema.split(";"):
        if statement.strip():
            conn.execute(statement)


def _v1(conn: sqlite3.Connection) -> None:
    _run(conn, """
        CREATE TABLE IF NOT EXISTS profiles (
          workspace   TEXT NOT NULL,
          id          TEXT NOT NULL,
          version     INTEGER NOT NULL,
          hash        TEXT NOT NULL,
          body        TEXT NOT NULL,
          state       TEXT NOT NULL,
          created_at  TEXT NOT NULL,
          created_by  TEXT,
          decided_at  TEXT,
          decided_by  TEXT,
          note        TEXT,
          PRIMARY KEY (workspace, id, version)
        );
        CREATE INDEX IF NOT EXISTS profiles_state ON profiles(state);
        CREATE TABLE IF NOT EXISTS photos (
          sha         TEXT PRIMARY KEY,
          workspace   TEXT NOT NULL,
          name        TEXT NOT NULL,
          type        TEXT NOT NULL,
          size        INTEGER NOT NULL,
          created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lane_photos (
          surface     TEXT NOT NULL,
          sha         TEXT NOT NULL,
          lane        TEXT NOT NULL,
          file_id     TEXT NOT NULL,
          created_at  TEXT NOT NULL,
          PRIMARY KEY (surface, sha)
        );
        CREATE TABLE IF NOT EXISTS marks (
          surface     TEXT NOT NULL,
          field       TEXT NOT NULL,
          value_hash  TEXT NOT NULL,
          by          TEXT,
          at          TEXT NOT NULL,
          PRIMARY KEY (surface, field)
        );
    """)


def _v2(conn: sqlite3.Connection) -> None:
    _run(conn, """
        CREATE TABLE IF NOT EXISTS sync (
          surface     TEXT NOT NULL,
          field       TEXT NOT NULL,
          status      TEXT NOT NULL,
          value_hash  TEXT,
          run_id      TEXT,
          error       TEXT,
          relearns    TEXT NOT NULL DEFAULT '[]',
          at          TEXT NOT NULL,
          PRIMARY KEY (surface, field)
        )
    """)


def _v3(conn: sqlite3.Connection) -> None:
    _run(conn, """
        CREATE TABLE IF NOT EXISTS lessons (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          site        TEXT NOT NULL,
          field       TEXT NOT NULL,
          surface     TEXT NOT NULL,
          lane        TEXT NOT NULL,
          by          TEXT,
          grant_id    TEXT,
          status      TEXT NOT NULL,
          error       TEXT,
          at          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS lessons_lane ON lessons(lane, at);
        CREATE INDEX IF NOT EXISTS lessons_field ON lessons(site, field, at)
    """)


def _v4(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(profiles)")}
    if "reason" not in columns:
        conn.execute("ALTER TABLE profiles ADD COLUMN reason TEXT")


MIGRATIONS = MigrationSet(package="profiles", db=DB, create=True,
                          migrations=(Migration(1, "profiles, photos, their copies in lanes, and fields changed by hand", _v1),
                                      Migration(2, "what the node synced on each account, field by field", _v2),
                                      Migration(3, "lessons: who taught which site's field, on which account", _v3),
                                      Migration(4, "why an agent proposed a version (profile_propose)", _v4)))
