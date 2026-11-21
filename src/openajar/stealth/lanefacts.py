"""What the lane check knows about a lane before it measures anything: its spec, state, control,
quarantine and last signal, from the running lane manager (ManagerFacts) or, when agentosd is
not running, read-only from openajar.db (StoreFacts).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openajar.core.errors import LaneNotFound
from openajar.core.layout import connect_ro
from openajar.core.models import LaneControl, LaneSpec


@dataclass
class LaneFacts:
    spec: LaneSpec
    state: str = "cold"
    control: LaneControl | None = None
    quarantine: dict[str, Any] | None = None    # reason, at, by, kind
    signal: dict[str, Any] | None = None        # signal, at, detail
    exit_ip: str | None = None                  # what the lane's own self-check saw last
    context: Any = None                         # the live browser context of a warm lane


def _value(x: Any) -> Any:
    return getattr(x, "value", x)


class ManagerFacts:
    """Facts from the running lane manager (inside agentosd)."""

    def __init__(self, lanes: Any):
        self.lanes = lanes

    def lane_ids(self) -> list[str]:
        return [info.spec.id for info in self.lanes.list()]

    def facts(self, lane_id: str) -> LaneFacts:
        info = self.lanes.get(lane_id)
        state = str(_value(info.state))
        quarantine = None
        store = getattr(self.lanes, "store", None)
        rec = None
        if store is not None:
            try:
                rec = store.get(lane_id)
            except Exception:  # noqa: BLE001 - the LaneInfo below still says it is quarantined
                rec = None
        if rec is not None and rec.quarantine is not None:
            q = rec.quarantine
            quarantine = {"reason": q.reason, "at": q.at, "by": q.by, "kind": q.kind}
        elif state == "quarantined":
            quarantine = {"reason": info.quarantine_reason or "failed self-check", "at": None, "by": "node",
                          "kind": "self_check"}
        signal = None
        if info.signal is not None:
            signal = {"signal": str(_value(info.signal)), "at": info.signal_at,
                      "detail": rec.signal.detail if rec is not None and rec.signal is not None else None}
        context = None
        if state in ("warm", "active"):
            lane = getattr(self.lanes, "_lanes", {}).get(lane_id)  # read only: the warm browser's context
            session = getattr(lane, "session", None)
            context = getattr(session, "context", None)
        return LaneFacts(spec=info.spec, state=state, control=info.control, quarantine=quarantine, signal=signal,
                         exit_ip=info.exit_ip, context=context)

    def pinned_exit(self, label: str) -> str | None:
        store = getattr(self.lanes, "store", None)
        pin = store.proxy_exit(label) if store is not None and hasattr(store, "proxy_exit") else None
        return pin.exit_ip if pin is not None else None


class StoreFacts:
    """Facts read straight from openajar.db, read-only (the daemon is not running). Every lane is cold."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def _rows(self, where: str = "", args: tuple = ()) -> list[tuple]:
        if not self.db_path.exists():
            return []
        conn = connect_ro(self.db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(lanes)")}
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            q = "l.quarantine" if "quarantine" in columns else "NULL"
            s = "l.signal" if "signal" in columns else "NULL"
            control = "c.control" if "lane_control" in tables else "NULL"
            join = " LEFT JOIN lane_control c ON c.id = l.id" if "lane_control" in tables else ""
            sql = f"SELECT l.id, l.spec, l.last_used, l.exit_ip, {control}, {q}, {s} FROM lanes l{join}{where}"
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def lane_ids(self) -> list[str]:
        return sorted(row[0] for row in self._rows())

    def facts(self, lane_id: str) -> LaneFacts:
        from openajar.node.store import LaneStore

        rows = self._rows(" WHERE l.id = ?", (lane_id,))
        if not rows:
            raise LaneNotFound(f"no lane {lane_id}")
        rec = LaneStore._record(rows[0])
        quarantine = None
        if rec.quarantine is not None:
            q = rec.quarantine
            quarantine = {"reason": q.reason, "at": q.at, "by": q.by, "kind": q.kind}
        signal = None
        if rec.signal is not None:
            signal = {"signal": rec.signal.signal.value, "at": rec.signal.at, "detail": rec.signal.detail}
        return LaneFacts(spec=rec.spec, state="quarantined" if quarantine else "cold", control=rec.control,
                         quarantine=quarantine, signal=signal, exit_ip=rec.exit_ip)

    def pinned_exit(self, label: str) -> str | None:
        """The pinned exit, or None when there is no pin. A read that fails raises (sqlite3.Error): an
        unreadable pin is not "no pin"."""
        if not self.db_path.exists():
            return None
        conn = connect_ro(self.db_path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "proxy_exits" not in tables:
                return None
            row = conn.execute("SELECT exit_ip FROM proxy_exits WHERE label = ?", (label,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
