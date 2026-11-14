"""SQLite store for environments (`$AGENTOS_HOME/environments.db`, WAL). A closed environment's
record stays (what it cost, what it pushed, why it closed) until the owner prunes it."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agentos.core.errors import NotFound
from agentos.core.sqlite import SqliteStore
from agentos.environments.models import OPEN_STATES, Environment

SCHEMA = """
CREATE TABLE IF NOT EXISTS environments (
  id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  binding TEXT NOT NULL,
  grant_id TEXT NOT NULL,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS environments_state ON environments (state);
CREATE INDEX IF NOT EXISTS environments_binding ON environments (binding);
"""


class EnvironmentStore(SqliteStore):
    """On agentos.core.sqlite: a kept connection per thread, WAL, a 5 s busy timeout as before."""

    def __init__(self, path: Path):
        super().__init__(path, timeout=5.0)
        self._conn().executescript(SCHEMA)
        self.path.chmod(0o600)  # an existing file too, as the store always did

    def put(self, env: Environment) -> None:
        self._q("INSERT OR REPLACE INTO environments (id, state, binding, grant_id, title, created_at, record) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (env.id, env.state, env.binding, env.grant_id, env.title, env.created_at.isoformat(),
                 env.model_dump_json()))

    def update(self, env_id: str, change: Callable[[Environment], bool | None]) -> Environment:
        """Read, change and write one record in one write transaction, so agentosd and the CLI (two
        processes) never lose each other's writes. `change` edits the record in place; returning
        False leaves it as it was. Returns the record as stored."""
        with self._tx() as db:
            rows = db.execute("SELECT record FROM environments WHERE id = ?", (env_id,)).fetchall()
            if not rows:
                raise NotFound(f"no environment {env_id}")
            env = Environment.model_validate_json(rows[0][0])
            if change(env) is not False:
                db.execute("UPDATE environments SET state = ?, binding = ?, record = ? WHERE id = ?",
                           (env.state, env.binding, env.model_dump_json(), env.id))
                return env
            return Environment.model_validate_json(rows[0][0])

    def has(self, env_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM environments WHERE id = ?", (env_id,)))

    def get(self, env_id: str) -> Environment:
        rows = self._q("SELECT record FROM environments WHERE id = ?", (env_id,))
        if not rows:
            raise NotFound(f"no environment {env_id}")
        return Environment.model_validate_json(rows[0][0])

    def open(self) -> list[Environment]:
        marks = ",".join("?" for _ in OPEN_STATES)
        rows = self._q(f"SELECT record FROM environments WHERE state IN ({marks}) ORDER BY created_at", OPEN_STATES)
        return [Environment.model_validate_json(r[0]) for r in rows]

    def bound(self, binding: str) -> Environment | None:
        marks = ",".join("?" for _ in OPEN_STATES)
        rows = self._q(f"SELECT record FROM environments WHERE binding = ? AND state IN ({marks}) "
                       "ORDER BY created_at DESC LIMIT 1", (binding, *OPEN_STATES))
        return Environment.model_validate_json(rows[0][0]) if rows else None

    def recent(self, limit: int = 100) -> list[Environment]:
        rows = self._q("SELECT record FROM environments ORDER BY created_at DESC LIMIT ?", (limit,))
        return [Environment.model_validate_json(r[0]) for r in rows]
