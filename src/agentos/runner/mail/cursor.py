"""Where a mailbox's poll stands, across a changed UIDVALIDITY.

A UID means something only together with the mailbox's UIDVALIDITY: a server that rebuilt a mailbox
may give every message a new number. So a message's id here is its UID with a generation above bit
32: `(epoch << 32) | uid`. The first generation is 0, so an id is the plain UID until a change ever
happens, and no two messages share an id, which is what a trigger's dedupe key rests on.

When the validity changes, numbers say nothing about what is new. What is left is when mail arrived
and which messages were already seen: a `watermark` (the newest arrival seen) and a ring of recent
message fingerprints. After a change every message up to the top is checked against both, in UID
order and in batches, until the cursor passes `verify_to`. Old mail is not replayed, because it
arrived before the watermark or is in the ring. New mail is not lost, because it did neither.

A server can also rewrite arrival times when it moves a mailbox, so a message whose own Date is well
before the watermark counts as old as well. A sender can write any Date, so a message that claims to
be old and that arrives in the same poll as a validity change is the one case this can miss.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

RING = 1000
UID_BITS = 32
UID_MASK = (1 << UID_BITS) - 1
SLACK = timedelta(minutes=10)     # how far clocks and delivery can disagree about an arrival
OLD_DATE = timedelta(days=2)
SCHEMA = "CREATE TABLE IF NOT EXISTS mail_cursors (mailbox TEXT PRIMARY KEY, doc TEXT NOT NULL);"


def compose(epoch: int, uid: int) -> int:
    return (epoch << UID_BITS) | uid


def split(message_id: int) -> tuple[int, int]:
    return message_id >> UID_BITS, message_id & UID_MASK


def fingerprint(message_id: str, date: str) -> str:
    """What says two copies are one message: its Message-ID, or its Date when it has none."""
    return hashlib.sha256((message_id or f"date:{date}").encode("utf-8", "replace")).hexdigest()[:24]


@dataclass
class Seen:
    fingerprint: str
    arrived: datetime | None
    date: str = ""


@dataclass
class Cursor:
    validity: int | None = None
    epoch: int = 0
    watermark: str | None = None
    recent: list[str] = field(default_factory=list)
    verify_to: int = 0

    @property
    def mark(self) -> datetime | None:
        return datetime.fromisoformat(self.watermark) if self.watermark else None


def _sent_on(date: str) -> datetime | None:
    try:
        when = parsedate_to_datetime(date)
        return when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)
    except (TypeError, ValueError, IndexError, OverflowError):  # a sender writes any Date it likes
        return None


def is_new(cursor: Cursor, seen: Seen) -> bool:
    """After a change: did this message arrive after the poll last stood here?"""
    mark = cursor.mark
    if seen.fingerprint in cursor.recent or mark is None:
        return False
    if seen.arrived is not None and seen.arrived < mark - SLACK:
        return False
    sent = _sent_on(seen.date)
    return sent is None or sent >= mark - OLD_DATE


class MailCursors:
    """One row per mailbox, in the runner's database. With no store it lives in memory."""

    def __init__(self, store: Any = None, clock: Any = None):
        self.store = store
        self.now = clock or (lambda: datetime.now(UTC))
        self._memory: dict[str, str] = {}
        self._lock = threading.RLock()
        if store is not None:
            with store.runner_store._lock:
                store.runner_store._db.executescript(SCHEMA)

    def _read(self, box: str) -> Cursor | None:
        if self.store is None:
            doc = self._memory.get(box)
        else:
            rows = self.store._rows("SELECT doc FROM mail_cursors WHERE mailbox = ?", (box,))
            doc = rows[0]["doc"] if rows else None
        return Cursor(**json.loads(doc)) if doc else None

    def _write(self, box: str, cursor: Cursor) -> Cursor:
        doc = json.dumps(asdict(cursor))
        if self.store is None:
            self._memory[box] = doc
        else:
            self.store._exec("INSERT OR REPLACE INTO mail_cursors (mailbox, doc) VALUES (?, ?)", (box, doc))
        return cursor

    def observe(self, box: str, validity: int) -> Cursor:
        """The cursor as this validity leaves it, whoever saw it first: a read or the poll."""
        with self._lock:
            cursor = self._read(box)
            if cursor is None:
                return self._write(box, Cursor(validity=validity, watermark=self.now().isoformat()))
            if cursor.validity != validity:
                cursor.validity = validity
                cursor.epoch += 1
                cursor.verify_to = 0
                return self._write(box, cursor)
            return cursor

    def seed(self, box: str, seen: list[Seen]) -> None:
        """Where a poll starts: what is there now is old."""
        self.saw(box, seen)

    def saw(self, box: str, seen: list[Seen], *, settle: bool = True) -> None:
        with self._lock:
            cursor = self._read(box) or Cursor(watermark=self.now().isoformat())
            cursor.recent = [*cursor.recent, *(s.fingerprint for s in seen)][-RING:]
            if settle:
                times = [s.arrived for s in seen if s.arrived is not None]
                mark = cursor.mark
                if times and (mark is None or max(times) > mark):
                    cursor.watermark = max(times).isoformat()
            self._write(box, cursor)

    def begin_verify(self, box: str, top: int) -> Cursor:
        with self._lock:
            cursor = self._read(box) or Cursor(watermark=self.now().isoformat())
            cursor.verify_to = top
            return self._write(box, cursor)

    def finish_verify(self, box: str) -> Cursor:
        with self._lock:
            cursor = self._read(box) or Cursor()
            cursor.verify_to = 0
            cursor.watermark = self.now().isoformat() if cursor.watermark is None else cursor.watermark
            return self._write(box, cursor)

    def drop(self, box: str) -> None:
        with self._lock:
            if self.store is None:
                self._memory.pop(box, None)
            else:
                self.store._exec("DELETE FROM mail_cursors WHERE mailbox = ?", (box,))
