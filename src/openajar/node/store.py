"""SQLite store for lane specs, plus each lane's last_used time, last seen exit IP, control
(who may act in it: agent, human or paused), quarantine and last signal. Also the exit IP
first seen through each proxy label (the self-check pin, AUD-14) and an
append-only log of lane events (holds, quarantines, proxy changes, deletes).

Runtime state (warm, active) is never stored: every lane is cold after a daemon restart.
Control and quarantine are stored, so a human's take-over or stop, a hold after a bad signal
and a quarantine all survive a restart. The CLI writes lanes here while the daemon runs: each
thread keeps one connection in autocommit mode, so every statement sees what other processes
committed (WAL mode, cheap reads; openajar.core.sqlite).

A row this version cannot read (a newer schema, a hand edit) never hides the other lanes:
list() skips it with a warning, and get() names it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from openajar.core.errors import InvalidRequest, LaneExists, LaneNotFound
from openajar.core.models import ControlState, DeviceSpec, LaneControl, LaneSpec, Signal, utcnow
from openajar.core.sqlite import SqliteStore

log = logging.getLogger("openajar.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lanes (
    id         TEXT PRIMARY KEY,
    spec       TEXT NOT NULL,
    last_used  TEXT,
    exit_ip    TEXT
)
"""
CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS lane_control (
    id       TEXT PRIMARY KEY,
    control  TEXT NOT NULL
)
"""
PROXY_EXITS_SCHEMA = """
CREATE TABLE IF NOT EXISTS proxy_exits (
    label       TEXT PRIMARY KEY,
    exit_ip     TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    pinned_by   TEXT NOT NULL
)
"""
EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS lane_events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    lane_id  TEXT NOT NULL,
    at       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    detail   TEXT
)
"""
DEVICES_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    name  TEXT PRIMARY KEY,
    spec  TEXT NOT NULL
)
"""
# Columns added after the first release; added in place on open (ALTER TABLE ADD COLUMN).
LANE_COLUMNS = {"quarantine": "TEXT", "signal": "TEXT"}
SELECT = ("SELECT l.id, l.spec, l.last_used, l.exit_ip, c.control, l.quarantine, l.signal FROM lanes l "
          "LEFT JOIN lane_control c ON c.id = l.id")


class QuarantineRecord(BaseModel):
    """Why a lane is quarantined. Cleared only by a human (hand back)."""
    reason: str
    at: datetime
    by: str = "node"
    kind: str = "manual"            # manual | exit_ip_changed | exit_ip_mismatch | exit_ip_is_node
    proxy_label: str | None = None  # exit_ip_changed: the label whose pin moved
    exit_ip: str | None = None      # exit_ip_changed: the new exit, pinned if the human accepts it


class SignalRecord(BaseModel):
    """The last signal a tool saw in the lane."""
    signal: Signal
    at: datetime
    detail: str | None = None


class ProxyExit(BaseModel):
    label: str
    exit_ip: str
    first_seen: datetime
    last_seen: datetime
    pinned_by: str = "self-check"


class LaneEvent(BaseModel):
    lane_id: str
    at: datetime
    kind: str
    detail: dict[str, Any] = {}


class LaneRecord(BaseModel):
    spec: LaneSpec
    last_used: datetime | None = None
    exit_ip: str | None = None
    control: LaneControl | None = None  # None: never changed, so agents have control
    quarantine: QuarantineRecord | None = None
    signal: SignalRecord | None = None


class LaneStore(SqliteStore):
    """Lanes, devices, proxy exits and lane events. On the event loop, call writes and hot reads
    through `await store.run(store.method, ...)` (openajar.core.sqlite)."""

    def __init__(self, path: Path, *, timeout: float = 10.0):
        super().__init__(path, timeout=timeout)
        self._unreadable: set[str] = set()  # ids already warned about
        with self._connect() as conn:
            for ddl in (SCHEMA, CONTROL_SCHEMA, PROXY_EXITS_SCHEMA, EVENTS_SCHEMA, DEVICES_SCHEMA):
                conn.execute(ddl)
            have = {row[1] for row in conn.execute("PRAGMA table_info(lanes)")}
            for name, kind in LANE_COLUMNS.items():
                if name not in have:
                    try:
                        conn.execute(f"ALTER TABLE lanes ADD COLUMN {name} {kind}")
                    except sqlite3.OperationalError as e:  # another process added it first
                        if "duplicate column" not in str(e):
                            raise

    @staticmethod
    def _control(lane_id: str, raw: str | None) -> LaneControl | None:
        if raw is None:
            return None
        try:
            return LaneControl.model_validate_json(raw)
        except ValueError:
            # Fail safe: a broken record must never hand a human's lane to an agent.
            log.warning("%s: unreadable control record; treating the lane as paused", lane_id)
            return LaneControl(state=ControlState.PAUSED, by="recovery", reason="unreadable control record")

    @staticmethod
    def _quarantine(lane_id: str, raw: str | None) -> QuarantineRecord | None:
        if raw is None:
            return None
        try:
            return QuarantineRecord.model_validate_json(raw)
        except ValueError:  # fail safe: still quarantined
            log.warning("%s: unreadable quarantine record; keeping the lane quarantined", lane_id)
            return QuarantineRecord(reason="unreadable quarantine record", at=utcnow(), by="recovery")

    @staticmethod
    def _signal(raw: str | None) -> SignalRecord | None:
        if raw is None:
            return None
        try:
            return SignalRecord.model_validate_json(raw)
        except ValueError:
            return None  # only informative

    @classmethod
    def _record(cls, row: tuple) -> LaneRecord:
        _, spec_json, last_used, exit_ip, control, quarantine, signal = row
        spec = LaneSpec.model_validate_json(spec_json)
        return LaneRecord(
            spec=spec,
            last_used=datetime.fromisoformat(last_used) if last_used else None,
            exit_ip=exit_ip,
            control=cls._control(spec.id, control),
            quarantine=cls._quarantine(spec.id, quarantine),
            signal=cls._signal(signal),
        )

    def _readable(self, row: tuple) -> LaneRecord | None:
        try:
            return self._record(row)
        except (ValidationError, ValueError, TypeError) as e:
            lane_id = row[0]
            if lane_id not in self._unreadable:
                self._unreadable.add(lane_id)
                log.warning("lane %s has a record this version cannot read (%s); skipping it",
                            lane_id, type(e).__name__)
            return None

    # ---- lanes ----

    def add(self, spec: LaneSpec) -> LaneRecord:
        try:
            with self._connect() as conn:
                conn.execute("INSERT INTO lanes (id, spec) VALUES (?, ?)", (spec.id, spec.model_dump_json()))
        except sqlite3.IntegrityError:
            raise LaneExists(f"lane {spec.id} already exists") from None
        return LaneRecord(spec=spec)

    def exists(self, lane_id: str) -> bool:
        """A row exists for this id, readable or not."""
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM lanes WHERE id = ?", (lane_id,)).fetchone() is not None

    def get(self, lane_id: str) -> LaneRecord | None:
        with self._connect() as conn:
            row = conn.execute(f"{SELECT} WHERE l.id = ?", (lane_id,)).fetchone()
        if not row:
            return None
        rec = self._readable(row)
        if rec is None:
            raise InvalidRequest(f"lane {lane_id} has a record this version of openajar cannot read. Upgrade "
                                 f"openajar, or remove the lane with: openajar lane delete {lane_id}")
        return rec

    def require(self, lane_id: str) -> LaneRecord:
        rec = self.get(lane_id)
        if rec is None:
            raise LaneNotFound(f"no lane named {lane_id}. Create it with: openajar lane create {lane_id}")
        return rec

    def list(self) -> list[LaneRecord]:
        with self._connect() as conn:
            rows = conn.execute(f"{SELECT} ORDER BY l.id").fetchall()
        return [rec for rec in map(self._readable, rows) if rec is not None]

    def update_spec(self, spec: LaneSpec) -> None:
        with self._connect() as conn:
            if conn.execute("UPDATE lanes SET spec = ? WHERE id = ?", (spec.model_dump_json(), spec.id)).rowcount == 0:
                raise LaneNotFound(f"no lane named {spec.id}")

    def touch(self, lane_id: str, when: datetime | None = None) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE lanes SET last_used = ? WHERE id = ?", ((when or utcnow()).isoformat(), lane_id))

    def set_exit_ip(self, lane_id: str, exit_ip: str | None) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE lanes SET exit_ip = ? WHERE id = ?", (exit_ip, lane_id))

    def set_control(self, lane_id: str, control: LaneControl) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO lane_control (id, control) VALUES (?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET control = excluded.control",
                         (lane_id, control.model_dump_json()))

    def set_quarantine(self, lane_id: str, quarantine: QuarantineRecord | None) -> None:
        raw = quarantine.model_dump_json() if quarantine is not None else None
        with self._connect() as conn:
            conn.execute("UPDATE lanes SET quarantine = ? WHERE id = ?", (raw, lane_id))

    def set_signal(self, lane_id: str, signal: SignalRecord) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE lanes SET signal = ? WHERE id = ?", (signal.model_dump_json(), lane_id))

    def delete(self, lane_id: str) -> bool:
        with self._connect() as conn:
            conn.execute("DELETE FROM lane_control WHERE id = ?", (lane_id,))
            return conn.execute("DELETE FROM lanes WHERE id = ?", (lane_id,)).rowcount > 0

    # ---- devices (AUD-02) ----

    def add_device(self, device: DeviceSpec) -> DeviceSpec:
        try:
            with self._connect() as conn:
                conn.execute("INSERT INTO devices (name, spec) VALUES (?, ?)", (device.name, device.model_dump_json()))
        except sqlite3.IntegrityError:
            raise InvalidRequest(f"device {device.name} already exists (openajar device show {device.name})") from None
        return device

    def device(self, name: str) -> DeviceSpec | None:
        with self._connect() as conn:
            row = conn.execute("SELECT spec FROM devices WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        try:
            return DeviceSpec.model_validate_json(row[0])
        except ValidationError:
            raise InvalidRequest(f"device {name} has a record this version of openajar cannot read") from None

    def devices(self) -> list[DeviceSpec]:
        with self._connect() as conn:
            rows = conn.execute("SELECT name, spec FROM devices ORDER BY name").fetchall()
        out = []
        for name, raw in rows:
            try:
                out.append(DeviceSpec.model_validate_json(raw))
            except ValidationError:
                log.warning("device %s: its record cannot be read by this version of openajar; skipped", name)
        return out

    def update_device(self, device: DeviceSpec) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE devices SET spec = ? WHERE name = ?", (device.model_dump_json(), device.name))

    def delete_device(self, name: str) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM devices WHERE name = ?", (name,)).rowcount > 0

    # ---- exit IP pins per proxy label ----

    def proxy_exit(self, label: str) -> ProxyExit | None:
        with self._connect() as conn:
            row = conn.execute("SELECT label, exit_ip, first_seen, last_seen, pinned_by FROM proxy_exits "
                               "WHERE label = ?", (label,)).fetchone()
        if row is None:
            return None
        return ProxyExit(label=row[0], exit_ip=row[1], first_seen=datetime.fromisoformat(row[2]),
                         last_seen=datetime.fromisoformat(row[3]), pinned_by=row[4])

    def pin_proxy_exit(self, label: str, exit_ip: str, *, by: str = "self-check", when: datetime | None = None) -> None:
        at = (when or utcnow()).isoformat()
        with self._connect() as conn:
            conn.execute("INSERT INTO proxy_exits (label, exit_ip, first_seen, last_seen, pinned_by) "
                         "VALUES (?, ?, ?, ?, ?) ON CONFLICT(label) DO UPDATE SET exit_ip = excluded.exit_ip, "
                         "first_seen = excluded.first_seen, last_seen = excluded.last_seen, "
                         "pinned_by = excluded.pinned_by", (label, exit_ip, at, at, by))

    def pin_proxy_exit_if_absent(self, label: str, exit_ip: str, *, by: str = "self-check",
                                 when: datetime | None = None) -> ProxyExit:
        """Pin `exit_ip` for `label` only when no pin is there yet, and return the pin that holds:
        two lanes on one gateway opening at once never overwrite each other's first exit."""
        at = (when or utcnow()).isoformat()
        with self._connect() as conn:
            conn.execute("INSERT INTO proxy_exits (label, exit_ip, first_seen, last_seen, pinned_by) "
                         "VALUES (?, ?, ?, ?, ?) ON CONFLICT(label) DO NOTHING", (label, exit_ip, at, at, by))
            row = conn.execute("SELECT label, exit_ip, first_seen, last_seen, pinned_by FROM proxy_exits "
                               "WHERE label = ?", (label,)).fetchone()
        return ProxyExit(label=row[0], exit_ip=row[1], first_seen=datetime.fromisoformat(row[2]),
                         last_seen=datetime.fromisoformat(row[3]), pinned_by=row[4])

    def touch_proxy_exit(self, label: str, when: datetime | None = None) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE proxy_exits SET last_seen = ? WHERE label = ?",
                         ((when or utcnow()).isoformat(), label))

    def forget_proxy_exit(self, label: str) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM proxy_exits WHERE label = ?", (label,)).rowcount > 0

    # ---- events ----

    def add_event(self, lane_id: str, kind: str, detail: dict[str, Any] | None = None,
                  when: datetime | None = None) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO lane_events (lane_id, at, kind, detail) VALUES (?, ?, ?, ?)",
                         (lane_id, (when or utcnow()).isoformat(), kind,
                          json.dumps(detail or {}, default=str, sort_keys=True)))

    def events(self, lane_id: str | None = None, *, limit: int = 100) -> list[LaneEvent]:
        """Newest first."""
        query = "SELECT lane_id, at, kind, detail FROM lane_events"
        args: tuple = ()
        if lane_id is not None:
            query += " WHERE lane_id = ?"
            args = (lane_id,)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY seq DESC LIMIT ?", (*args, max(1, limit))).fetchall()
        out = []
        for lid, at, kind, detail in rows:
            try:
                data = json.loads(detail) if detail else {}
            except ValueError:
                data = {}
            out.append(LaneEvent(lane_id=lid, at=datetime.fromisoformat(at), kind=kind,
                                 detail=data if isinstance(data, dict) else {}))
        return out
