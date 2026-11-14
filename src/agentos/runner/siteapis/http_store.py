"""siteapis/http.db: site API keys, the result cache and the usage rows.

`$AGENTOS_HOME/siteapis/http.db`, WAL, mode 600 in a 700 directory. The schema comes from
agentos.runner.siteapis.http_migrations through the node's migrator.

A key row holds the SHA-256 of the key and a short prefix, never the key. A usage row holds names, a
status, a code, a duration and a time: never params, data or the caller's address. A cache row holds
a run's typed output, which the node keeps anyway in runner.db.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from agentos.core.errors import ServiceUnavailable
from agentos.core.sqlite import SqliteStore
from agentos.ops import migrate
from agentos.ops.layout import adopt_owner
from agentos.runner.siteapis.http_migrations import DB, MIGRATIONS

KEY_FIELDS = frozenset({"label", "apis", "endpoints", "surfaces", "ceiling", "rate_per_min", "grant_id", "old_grants",
                        "expires_at", "last_used_at", "revoked_at", "revoked_by"})
JSON_FIELDS = frozenset({"apis", "endpoints", "surfaces", "old_grants"})


def _key(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for name in JSON_FIELDS:
        d[name] = json.loads(d[name]) if d[name] is not None else None
    return d


class HttpStore(SqliteStore):
    """One kept connection per thread (agentos.core.sqlite). Per-request writes (usage rows, a key's
    or a cached result's last use, a new cached result) go through soon(): never waiting for a lock
    another process holds. Key changes (add, edit, revoke, a new grant) are plain writes, done
    before the call returns."""

    row_factory = sqlite3.Row

    def __init__(self, home: Path):
        path = Path(home) / DB
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            adopt_owner(path.parent, Path(home))  # root at the node's CLI, before the service ever ran
            path.parent.chmod(0o700)
            migrate.migrate(Path(home), [MIGRATIONS], backup=path.exists())
            path.chmod(0o600)
            super().__init__(path, timeout=30.0, private=False)
        except (OSError, sqlite3.Error, migrate.MigrationError) as e:
            raise ServiceUnavailable(f"cannot open {path}: {e}") from None

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, tuple(args)).fetchall()

    # ---- keys ----
    def add_key(self, row: dict[str, Any]) -> None:
        names = list(row)
        values = [json.dumps(row[n]) if n in JSON_FIELDS and row[n] is not None else row[n] for n in names]
        with self._tx() as conn:
            conn.execute(f"INSERT INTO site_api_keys ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", values)

    def key(self, key_id: str) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM site_api_keys WHERE id = ?", (key_id,))
        return _key(rows[0]) if rows else None

    def all_keys(self) -> list[dict[str, Any]]:
        return [_key(r) for r in self._all("SELECT * FROM site_api_keys ORDER BY created_at, id")]

    def keys_by_prefix(self, prefix: str) -> list[dict[str, Any]]:
        return [_key(r) for r in self._all("SELECT * FROM site_api_keys WHERE prefix = ?", (prefix,))]

    def update_key(self, key_id: str, **fields: Any) -> None:
        names = sorted(fields)
        assert set(names) <= KEY_FIELDS
        values = [json.dumps(fields[n]) if n in JSON_FIELDS and fields[n] is not None else fields[n] for n in names]
        with self._tx() as conn:
            conn.execute(f"UPDATE site_api_keys SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",
                         [*values, key_id])

    # ---- cache ----
    def cache_get(self, key: str) -> sqlite3.Row | None:
        rows = self._all("SELECT * FROM site_api_cache WHERE key = ?", (key,))
        return rows[0] if rows else None

    def cache_touch(self, key: str, at: float) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE site_api_cache SET used_at = ? WHERE key = ?", (at, key))

    def cache_put(self, row: dict[str, Any], *, max_bytes: int, still: Callable[[], bool] | None = None) -> int:
        """Store one result in place of any older version of it, then evict the least recently used
        results until the whole cache fits in `max_bytes`. Returns how many were evicted. `still`,
        asked once the write lock is held, can call the put off (its API was dropped meanwhile)."""
        evicted = 0
        with self._tx() as conn:
            if still is not None and not still():
                return 0
            conn.execute("DELETE FROM site_api_cache WHERE flow_id = ? AND surface = ? AND version != ?",
                         (row["flow_id"], row["surface"], row["version"]))
            conn.execute("INSERT OR REPLACE INTO site_api_cache (key, api, flow_id, version, surface, value, bytes, "
                         "stored_at, used_at) VALUES (:key, :api, :flow_id, :version, :surface, :value, :bytes, "
                         ":stored_at, :used_at)", row)
            total = conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM site_api_cache").fetchone()[0]
            if total > max_bytes:
                for old in conn.execute("SELECT key, bytes FROM site_api_cache WHERE key != ? ORDER BY used_at, stored_at",
                                        (row["key"],)).fetchall():
                    conn.execute("DELETE FROM site_api_cache WHERE key = ?", (old["key"],))
                    evicted += 1
                    total -= old["bytes"]
                    if total <= max_bytes:
                        break
        return evicted

    def cache_delete(self, *, api: str | None = None, flow_id: str | None = None) -> int:
        with self._tx() as conn:
            if flow_id is not None:
                return conn.execute("DELETE FROM site_api_cache WHERE flow_id = ?", (flow_id,)).rowcount
            return conn.execute("DELETE FROM site_api_cache WHERE api = ?", (api,)).rowcount

    def cache_bytes(self) -> int:
        return int(self._all("SELECT COALESCE(SUM(bytes), 0) AS n FROM site_api_cache")[0]["n"])

    # ---- usage ----
    def usage_add(self, row: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute("INSERT INTO site_api_usage (key_id, api, endpoint, surface, status, code, duration_ms, cached, "
                         "at) VALUES (:key_id, :api, :endpoint, :surface, :status, :code, :duration_ms, :cached, :at)",
                         row)

    def usage_rows(self, since: float, until: float) -> list[sqlite3.Row]:
        return self._all("SELECT api, endpoint, status, code, duration_ms, cached, at FROM site_api_usage "
                         "WHERE at >= ? AND at < ? ORDER BY at", (since, until))

    def usage_first(self) -> float | None:
        return self._all("SELECT MIN(at) AS at FROM site_api_usage")[0]["at"]

    def rolled_until(self) -> int:
        return int(self._all("SELECT COALESCE(MAX(hour) + 3600, 0) AS h FROM site_api_usage_hourly")[0]["h"])

    def rollup_put(self, rows: list[dict[str, Any]]) -> None:
        with self._tx() as conn:
            conn.executemany("INSERT OR REPLACE INTO site_api_usage_hourly (api, endpoint, hour, calls, cached, fresh, "
                             "errors, p50_ms, p95_ms) VALUES (:api, :endpoint, :hour, :calls, :cached, :fresh, :errors, "
                             ":p50_ms, :p95_ms)", rows)

    def rollup_rows(self, api: str, endpoint: str, since: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM site_api_usage_hourly WHERE api = ? AND endpoint = ? AND hour >= ? ORDER BY hour",
                         (api, endpoint, since))

    def usage_purge(self, before: float) -> int:
        with self._tx() as conn:
            n = conn.execute("DELETE FROM site_api_usage WHERE at < ?", (before,)).rowcount
            n += conn.execute("DELETE FROM site_api_usage_hourly WHERE hour < ?", (int(before) - 3600,)).rowcount
        return n
