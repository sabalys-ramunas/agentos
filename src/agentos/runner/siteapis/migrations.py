"""site_api_drafts in runner.db, found by agentos.core.migrate.discover() through
agentos.runner.migrations, and applied when the store opens. Drafts only: an activated API is its
flows, and keys, cache and usage are the HTTP half's (http_migrations.py)."""

from __future__ import annotations

import sqlite3

from agentos.core.migrate import Migration, MigrationSet

DB = "runner.db"

SCHEMA_V1 = """
        CREATE TABLE IF NOT EXISTS site_api_drafts (
          api         TEXT PRIMARY KEY,
          state       TEXT NOT NULL,
          definition  TEXT NOT NULL,
          digest      TEXT NOT NULL,
          person      INTEGER NOT NULL,
          author      TEXT NOT NULL,
          proposal    TEXT,
          installed   TEXT,
          created_at  TEXT NOT NULL,
          updated_at  TEXT NOT NULL
        )
"""


def _v1(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA_V1)


MIGRATIONS = MigrationSet(package="site_api_drafts", db=DB, migrations=(Migration(1, "site api drafts", _v1),))
