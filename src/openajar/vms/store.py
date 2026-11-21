"""SQLite store for VM records (`$OPENAJAR_HOME/vms.db`, WAL). A record outlives its VM: the
history (who made it, why, for how long, what it cost) stays until the owner prunes it."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openajar.core.errors import NotFound
from openajar.core.sqlite import SqliteStore
from openajar.vms.models import LIVE_STATES, VmRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS vms (
  id TEXT PRIMARY KEY,
  lane_id TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vms_lane ON vms (lane_id);
CREATE INDEX IF NOT EXISTS vms_state ON vms (state);
"""


class VmStore(SqliteStore):
    """On openajar.core.sqlite: a kept connection per thread, WAL, a 5 s busy timeout as before."""

    def __init__(self, path: Path):
        super().__init__(path, timeout=5.0)
        self._conn().executescript(SCHEMA)
        self.path.chmod(0o600)  # an existing file too, as the store always did

    def put(self, rec: VmRecord) -> None:
        self._q("INSERT OR REPLACE INTO vms (id, lane_id, state, created_at, record) VALUES (?, ?, ?, ?, ?)",
                (rec.id, rec.lane_id, rec.state, rec.created_at.isoformat(), rec.model_dump_json()))

    def get(self, vm_id: str) -> VmRecord:
        rows = self._q("SELECT record FROM vms WHERE id = ?", (vm_id,))
        if not rows:
            raise NotFound(f"no VM {vm_id}")
        return VmRecord.model_validate_json(rows[0][0])

    def live_for_lane(self, lane_id: str) -> VmRecord | None:
        marks = ",".join("?" for _ in LIVE_STATES)
        rows = self._q(f"SELECT record FROM vms WHERE lane_id = ? AND state IN ({marks}) "
                       "ORDER BY created_at DESC LIMIT 1", (lane_id, *LIVE_STATES))
        return VmRecord.model_validate_json(rows[0][0]) if rows else None

    def live(self) -> list[VmRecord]:
        marks = ",".join("?" for _ in LIVE_STATES)
        rows = self._q(f"SELECT record FROM vms WHERE state IN ({marks}) ORDER BY created_at", LIVE_STATES)
        return [VmRecord.model_validate_json(r[0]) for r in rows]

    def recent(self, limit: int = 50) -> list[VmRecord]:
        rows = self._q("SELECT record FROM vms ORDER BY created_at DESC LIMIT ?", (limit,))
        return [VmRecord.model_validate_json(r[0]) for r in rows]

    def since(self, when: datetime) -> list[VmRecord]:
        """Every VM alive at some point since `when` (for the month's spend)."""
        rows = self._q("SELECT record FROM vms")
        out = []
        for (raw,) in rows:
            r = VmRecord.model_validate_json(raw)
            if r.deleted_at is None or r.deleted_at >= when:
                out.append(r)
        return out
