"""siteapis/http.db migrations: the HTTP half of site APIs keeps its own database, so the keys, the
cache and the usage rows never share a migration with the engine's tables in runner.db. Found by
agentos.ops.migrate.discover() through agentos.runner.migrations, and applied when the store opens."""

from __future__ import annotations

import sqlite3

from agentos.ops.migrate import Migration, MigrationSet

DB = "siteapis/http.db"

SCHEMA_V1 = """
        CREATE TABLE IF NOT EXISTS site_api_keys (
          id            TEXT PRIMARY KEY,
          label         TEXT NOT NULL,
          prefix        TEXT NOT NULL,
          hash          TEXT NOT NULL UNIQUE,
          apis          TEXT NOT NULL,
          endpoints     TEXT,
          surfaces      TEXT,
          ceiling       TEXT NOT NULL,
          rate_per_min  INTEGER NOT NULL,
          grant_id      TEXT NOT NULL,
          expires_at    REAL,
          last_used_at  REAL,
          created_at    REAL NOT NULL,
          created_by    TEXT NOT NULL,
          revoked_at    REAL,
          revoked_by    TEXT
        );
        CREATE INDEX IF NOT EXISTS site_api_keys_prefix ON site_api_keys(prefix);
        CREATE TABLE IF NOT EXISTS site_api_cache (
          key           TEXT PRIMARY KEY,
          api           TEXT NOT NULL,
          flow_id       TEXT NOT NULL,
          version       TEXT NOT NULL,
          surface       TEXT NOT NULL,
          value         TEXT NOT NULL,
          bytes         INTEGER NOT NULL,
          stored_at     REAL NOT NULL,
          used_at       REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS site_api_cache_api ON site_api_cache(api);
        CREATE INDEX IF NOT EXISTS site_api_cache_used ON site_api_cache(used_at);
        CREATE TABLE IF NOT EXISTS site_api_usage (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          key_id        TEXT NOT NULL,
          api           TEXT NOT NULL,
          endpoint      TEXT NOT NULL,
          surface       TEXT,
          status        INTEGER NOT NULL,
          code          TEXT,
          duration_ms   INTEGER NOT NULL,
          cached        INTEGER NOT NULL,
          at            REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS site_api_usage_at ON site_api_usage(at);
        CREATE TABLE IF NOT EXISTS site_api_usage_hourly (
          api           TEXT NOT NULL,
          endpoint      TEXT NOT NULL,
          hour          INTEGER NOT NULL,
          calls         INTEGER NOT NULL,
          cached        INTEGER NOT NULL,
          fresh         INTEGER NOT NULL,
          errors        TEXT NOT NULL,
          p50_ms        INTEGER NOT NULL,
          p95_ms        INTEGER NOT NULL,
          PRIMARY KEY (api, endpoint, hour)
        )
"""


def _v1(conn: sqlite3.Connection) -> None:
    # one statement at a time: executescript would commit the migrator's transaction
    for statement in SCHEMA_V1.split(";"):
        if statement.strip():
            conn.execute(statement)


def _v2(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE site_api_keys ADD COLUMN old_grants TEXT NOT NULL DEFAULT '{}'")


MIGRATIONS = MigrationSet(package="siteapis_http", db=DB, create=True,
                          migrations=(Migration(1, "keys, cache and usage", _v1),
                                      Migration(2, "the grants a key's renewal left live", _v2)))
