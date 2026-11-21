"""SQLite storage for events, routes, deliveries and trigger proposals.

One file (`$OPENAJAR_HOME/events/events.db`), WAL mode, `synchronous=FULL`, the daemon the
only writer, like the runner and brains stores. Node-local, so it keeps working with the
controller down.

- `events` is idempotent on (source, dedupe_key): a duplicate tick, a restart between
  "fired" and "recorded", or a replayed webhook stores nothing new. Content sits in its own
  column so it can be purged after `content_ttl_days` while the row and its facts stay.
- `delivery_items` is keyed by (route, event): an event reaches one route at most once,
  whatever batches or digests it joins.
- A row that cannot be read is skipped and logged once, so one bad row never stops the
  dispatcher.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from openajar.events.models import OPEN_STATES, Content, Delivery, DeliveryState, Event, Route
from openajar.runner.util import iso

log = logging.getLogger("openajar.events")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL, workspace TEXT NOT NULL, lane TEXT,
  dedupe_key TEXT NOT NULL, received_at TEXT NOT NULL, schedule_id TEXT, delivery TEXT,
  doc TEXT NOT NULL, content TEXT, UNIQUE (source, dedupe_key));
CREATE INDEX IF NOT EXISTS events_received ON events(received_at);
CREATE INDEX IF NOT EXISTS events_hook ON events(schedule_id, delivery);
CREATE TABLE IF NOT EXISTS routes (
  id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, workspace TEXT NOT NULL, source TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries (
  id TEXT PRIMARY KEY, route_id TEXT NOT NULL, state TEXT NOT NULL, due_at TEXT NOT NULL, rank INTEGER NOT NULL,
  key TEXT NOT NULL UNIQUE, target_kind TEXT NOT NULL, target_lane TEXT, open_until TEXT,
  created_at TEXT NOT NULL, delivered_at TEXT, finished_at TEXT, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(state, rank, due_at);
CREATE INDEX IF NOT EXISTS deliveries_route ON deliveries(route_id, created_at);
CREATE INDEX IF NOT EXISTS deliveries_lane ON deliveries(target_lane, delivered_at);
CREATE TABLE IF NOT EXISTS delivery_items (
  route_id TEXT NOT NULL, event_id TEXT NOT NULL, delivery_id TEXT NOT NULL, PRIMARY KEY (route_id, event_id));
CREATE TABLE IF NOT EXISTS proposals (
  approval_id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class EventStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(SCHEMA)
        self._bad: set[str] = set()
        if str(path) != ":memory:":
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _rows(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(args))

    def _skip(self, what: str, key: str, e: Exception) -> None:
        if f"{what}:{key}" not in self._bad:
            self._bad.add(f"{what}:{key}")
            log.error("skipping unreadable %s row %s: %s", what, key, str(e)[:300])

    # ------------------------------------------------------------ meta

    def get_meta(self, key: str) -> Any:
        rows = self._rows("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else None

    def set_meta(self, key: str, value: Any) -> None:
        self._exec("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value, default=str)))

    # ------------------------------------------------------------ events

    def insert_event(self, ev: Event) -> tuple[Event, bool]:
        """Insert, or return the stored event with the same (source, dedupe_key)."""
        doc = ev.model_dump_json(exclude={"content"})
        content = ev.content.model_dump_json() if ev.content is not None else None
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO events (id, source, kind, workspace, lane, dedupe_key, received_at, schedule_id, "
                    "delivery, doc, content) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ev.id, ev.source, ev.kind, ev.workspace, ev.lane, ev.dedupe_key, iso(ev.received_at),
                     ev.facts.get("schedule_id"), ev.facts.get("delivery"), doc, content))
                return ev, True
            except sqlite3.IntegrityError:
                rows = self._db.execute("SELECT * FROM events WHERE source=? AND dedupe_key=?",
                                        (ev.source, ev.dedupe_key)).fetchall()
                if not rows:
                    raise
                return self._event(rows[0]), False

    def _event(self, row: sqlite3.Row) -> Event:
        ev = Event.model_validate_json(row["doc"])
        if row["content"]:
            ev.content = Content.model_validate_json(row["content"])
        return ev

    def get_event(self, event_id: str) -> Event | None:
        rows = self._rows("SELECT * FROM events WHERE id=?", (event_id,))
        return self._event(rows[0]) if rows else None

    def events(self, ids: Iterable[str]) -> list[Event]:
        ids = list(ids)
        if not ids:
            return []
        rows = self._rows(f"SELECT * FROM events WHERE id IN ({','.join('?' * len(ids))})", ids)
        by_id = {}
        for r in rows:
            try:
                by_id[r["id"]] = self._event(r)
            except ValueError as e:
                self._skip("event", r["id"], e)
        return [by_id[i] for i in ids if i in by_id]

    def find_webhook(self, schedule_id: str, delivery: str) -> Event | None:
        rows = self._rows("SELECT * FROM events WHERE source='webhook' AND schedule_id=? AND delivery=? LIMIT 1",
                          (schedule_id, delivery))
        return self._event(rows[0]) if rows else None

    def list_events(self, *, source: str | None = None, workspace: str | None = None,
                    since: datetime | None = None, limit: int = 100) -> list[Event]:
        where, args = [], []
        for col, val in (("source", source), ("workspace", workspace)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        if since is not None:
            where.append("received_at > ?")
            args.append(iso(since))
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY received_at DESC, rowid DESC LIMIT ?"
        out = []
        for r in self._rows(sql, [*args, limit]):
            try:
                out.append(self._event(r))
            except ValueError as e:
                self._skip("event", r["id"], e)
        return out

    def count_events_since(self, since: datetime, *, exclude_source: str | None = None) -> int:
        sql, args = "SELECT COUNT(*) AS n FROM events WHERE received_at > ?", [iso(since)]
        if exclude_source:
            sql += " AND source != ?"
            args.append(exclude_source)
        return int(self._rows(sql, args)[0]["n"])

    def purge_content(self, before: datetime) -> int:
        return self._exec("UPDATE events SET content=NULL WHERE content IS NOT NULL AND received_at <= ?",
                          (iso(before),)).rowcount

    def purge_events(self, before: datetime) -> int:
        """Old events nobody points at any more (a dead letter keeps its events)."""
        return self._exec(
            "DELETE FROM events WHERE received_at <= ? AND id NOT IN (SELECT i.event_id FROM delivery_items i "
            "JOIN deliveries d ON d.id=i.delivery_id WHERE d.state IN ('dead','failed','pending','waiting',"
            "'delivered'))", (iso(before),)).rowcount

    # ------------------------------------------------------------ routes

    def put_route(self, r: Route) -> None:
        self._exec("INSERT INTO routes (id, enabled, workspace, source, doc) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, workspace=excluded.workspace, "
                   "source=excluded.source, doc=excluded.doc",
                   (r.id, int(r.enabled), r.workspace, r.match.source, r.model_dump_json(by_alias=True)))

    def _route(self, row: sqlite3.Row) -> Route | None:
        try:
            return Route.model_validate_json(row["doc"])
        except ValueError as e:
            self._skip("route", row["id"], e)
            return None

    def get_route(self, route_id: str) -> Route | None:
        rows = self._rows("SELECT * FROM routes WHERE id=?", (route_id,))
        return self._route(rows[0]) if rows else None

    def list_routes(self, *, workspace: str | None = None, source: str | None = None,
                    enabled_only: bool = False) -> list[Route]:
        where, args = [], []
        for col, val in (("workspace", workspace), ("source", source)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        if enabled_only:
            where.append("enabled=1")
        sql = "SELECT * FROM routes" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        return [r for r in (self._route(row) for row in self._rows(sql, args)) if r is not None]

    def delete_route(self, route_id: str) -> bool:
        return self._exec("DELETE FROM routes WHERE id=?", (route_id,)).rowcount > 0

    # ------------------------------------------------------------ deliveries

    def _dargs(self, d: Delivery, target_lane: str | None) -> tuple:
        return (d.route_id, d.state.value, iso(d.due_at), d.rank, d.key, d.target_kind, target_lane,
                iso(d.open_until), iso(d.created_at), iso(d.delivered_at), iso(d.finished_at), d.model_dump_json())

    def insert_delivery(self, d: Delivery, *, target_lane: str | None) -> bool:
        """Insert a delivery and claim its events for its route. False (nothing stored) when
        one of the events already reached that route."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for eid in d.event_ids:
                    self._db.execute("INSERT INTO delivery_items (route_id, event_id, delivery_id) VALUES (?,?,?)",
                                     (d.route_id, eid, d.id))
                self._db.execute(
                    "INSERT INTO deliveries (route_id, state, due_at, rank, key, target_kind, target_lane, "
                    "open_until, created_at, delivered_at, finished_at, doc, id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*self._dargs(d, target_lane), d.id))
            except sqlite3.IntegrityError:
                self._db.execute("ROLLBACK")
                return False
            self._db.execute("COMMIT")
            return True

    def add_item(self, d: Delivery, event_id: str, *, target_lane: str | None) -> bool:
        """Append an event to an open delivery (a batch or digest). False if it already
        reached this route or the delivery is no longer open."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT state FROM deliveries WHERE id=?", (d.id,)).fetchone()
                if row is None or row["state"] not in {s.value for s in OPEN_STATES}:
                    self._db.execute("ROLLBACK")
                    return False
                self._db.execute("INSERT INTO delivery_items (route_id, event_id, delivery_id) VALUES (?,?,?)",
                                 (d.route_id, event_id, d.id))
                d.event_ids.append(event_id)
                self._db.execute(
                    "UPDATE deliveries SET route_id=?, state=?, due_at=?, rank=?, key=?, target_kind=?, "
                    "target_lane=?, open_until=?, created_at=?, delivered_at=?, finished_at=?, doc=? WHERE id=?",
                    (*self._dargs(d, target_lane), d.id))
            except sqlite3.IntegrityError:
                self._db.execute("ROLLBACK")
                if event_id in d.event_ids:
                    d.event_ids.remove(event_id)
                return False
            self._db.execute("COMMIT")
            return True

    def save_delivery(self, d: Delivery, *, target_lane: str | None,
                      expect: Iterable[DeliveryState] | None = None) -> bool:
        """Write the delivery. With `expect`, only if its stored state is one of those (CAS)."""
        sql = ("UPDATE deliveries SET route_id=?, state=?, due_at=?, rank=?, key=?, target_kind=?, target_lane=?, "
               "open_until=?, created_at=?, delivered_at=?, finished_at=?, doc=? WHERE id=?")
        args: list[Any] = [*self._dargs(d, target_lane), d.id]
        if expect is not None:
            states = [s.value for s in expect]
            sql += f" AND state IN ({','.join('?' * len(states))})"
            args += states
        return self._exec(sql, args).rowcount == 1

    def _delivery(self, row: sqlite3.Row) -> Delivery | None:
        try:
            return Delivery.model_validate_json(row["doc"])
        except ValueError as e:
            self._skip("delivery", row["id"], e)
            return None

    def get_delivery(self, delivery_id: str) -> Delivery | None:
        rows = self._rows("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        return self._delivery(rows[0]) if rows else None

    def list_deliveries(self, *, states: Iterable[DeliveryState] | None = None, route_id: str | None = None,
                        due_before: datetime | None = None, target_lane: str | None = None,
                        event_id: str | None = None, limit: int = 200, by_due: bool = False) -> list[Delivery]:
        where, args = [], []
        if states is not None:
            vals = [s.value for s in states]
            where.append(f"state IN ({','.join('?' * len(vals))})")
            args += vals
        for col, val in (("route_id", route_id), ("target_lane", target_lane)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        if due_before is not None:
            where.append("due_at <= ?")
            args.append(iso(due_before))
        if event_id is not None:
            where.append("id IN (SELECT delivery_id FROM delivery_items WHERE event_id=?)")
            args.append(event_id)
        sql = "SELECT * FROM deliveries" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY rank, due_at, rowid" if by_due else " ORDER BY created_at DESC, rowid DESC"
        return [d for d in (self._delivery(r) for r in self._rows(sql + " LIMIT ?", [*args, limit])) if d]

    def count_deliveries(self, *, states: Iterable[DeliveryState] = OPEN_STATES, route_id: str | None = None,
                         target_lane: str | None = None, target_kind: str | None = None) -> int:
        vals = [s.value for s in states]
        where, args = [f"state IN ({','.join('?' * len(vals))})"], list(vals)
        for col, val in (("route_id", route_id), ("target_lane", target_lane), ("target_kind", target_kind)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        return int(self._rows("SELECT COUNT(*) AS n FROM deliveries WHERE " + " AND ".join(where), args)[0]["n"])

    def delivered_since(self, since: datetime, *, route_id: str | None = None,
                        target_lane: str | None = None) -> list[datetime]:
        """When deliveries of a route (or to a lane) were handed to their target, oldest first,
        for the rate limits."""
        where, args = ["delivered_at IS NOT NULL", "delivered_at > ?"], [iso(since)]
        for col, val in (("route_id", route_id), ("target_lane", target_lane)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        rows = self._rows("SELECT delivered_at FROM deliveries WHERE " + " AND ".join(where) +
                          " ORDER BY delivered_at", args)
        return [datetime.fromisoformat(r["delivered_at"]) for r in rows]

    def open_batch(self, route_id: str, now: datetime) -> Delivery | None:
        """The route's newest pending delivery that still takes items (its window is open)."""
        rows = self._rows("SELECT * FROM deliveries WHERE route_id=? AND state='pending' AND open_until > ? "
                          "ORDER BY created_at DESC LIMIT 1", (route_id, iso(now)))
        return self._delivery(rows[0]) if rows else None

    def newest_open(self, route_id: str) -> Delivery | None:
        rows = self._rows("SELECT * FROM deliveries WHERE route_id=? AND state IN ('pending','waiting') "
                          "ORDER BY created_at DESC LIMIT 1", (route_id,))
        return self._delivery(rows[0]) if rows else None

    def recent_subject(self, route_id: str, subject: str, since: datetime) -> Delivery | None:
        for d in self.list_deliveries(route_id=route_id, limit=200):
            if d.created_at <= since:
                break
            if d.subject == subject and d.state != DeliveryState.DROPPED:
                return d
        return None

    def recent_route_ids(self, since: datetime) -> list[str]:
        rows = self._rows("SELECT DISTINCT route_id FROM deliveries WHERE created_at > ?", (iso(since),))
        return [r["route_id"] for r in rows]

    def purge_deliveries(self, before: datetime) -> int:
        """Finished deliveries past retention (dead letters stay until the owner looks)."""
        with self._lock:
            ids = [r["id"] for r in self._db.execute(
                "SELECT id FROM deliveries WHERE state IN ('done','dropped') AND created_at <= ?", (iso(before),))]
            for i in ids:
                self._db.execute("DELETE FROM delivery_items WHERE delivery_id=?", (i,))
                self._db.execute("DELETE FROM deliveries WHERE id=?", (i,))
            return len(ids)

    # ------------------------------------------------------------ proposals

    def put_proposal(self, approval_id: str, state: str, created_at: datetime, doc: dict[str, Any]) -> None:
        self._exec("INSERT INTO proposals (approval_id, state, created_at, doc) VALUES (?,?,?,?) "
                   "ON CONFLICT(approval_id) DO UPDATE SET state=excluded.state, doc=excluded.doc",
                   (approval_id, state, iso(created_at), json.dumps(doc, default=str)))

    def pending_proposals(self) -> list[tuple[str, dict[str, Any]]]:
        rows = self._rows("SELECT approval_id, doc FROM proposals WHERE state='pending' ORDER BY created_at")
        return [(r["approval_id"], json.loads(r["doc"])) for r in rows]
