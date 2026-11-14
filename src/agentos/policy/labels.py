"""Label views for workspace memory (docs/design/workspace-memory.md 4.5 and 4.12), in policy.db.

`EverLedger` (table `taint_ever`): every label a grant family has seen since its root grant was
issued. The taint ledger's 12-hour labels decide routing; memory trust needs longer, because an
agent that read a page at 9:00 still holds that text at 22:00. TaintLedger.add writes here too,
and a family's rows are dropped once its root grant has expired or been revoked.

`ReceiptStore` (table `context_receipts`): the decisions digest a family last loaded with
memory_context, per workspace, so a commit trace can say `decisions_loaded: current | stale |
never`. It decides nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Literal

from agentos.policy.store import DB

KNOWN_LABELS = frozenset({"untrusted", "private", "owner"})   # the same as service.TAINT_LABELS
EXPIRY_GRACE_S = 3600   # an expired root keeps its rows a little longer than clock skew

SCHEMA = """
CREATE TABLE IF NOT EXISTS taint_ever (
  family TEXT NOT NULL, label TEXT NOT NULL, first_at REAL NOT NULL, source TEXT,
  PRIMARY KEY (family, label)
);
CREATE TABLE IF NOT EXISTS context_receipts (
  family TEXT NOT NULL, workspace TEXT NOT NULL, digest TEXT NOT NULL, at REAL NOT NULL,
  PRIMARY KEY (family, workspace)
);
"""

# families ("family:<root id>") whose root grant expired or was revoked
DEAD_FAMILIES = _DEAD_FAMILIES = (
    "SELECT 'family:' || g.id FROM grants g WHERE g.parent_id IS NULL AND "
    "((g.expires_at IS NOT NULL AND g.expires_at <= ?) OR g.id IN (SELECT grant_id FROM revoked))"
)

ReceiptState = Literal["current", "stale", "never"]


def ensure_schema(db: DB) -> None:
    with db.lock:
        db.conn.executescript(SCHEMA)


class EverLedger:
    """The labels a grant family has ever seen (design 4.5, the `ever` view)."""

    def __init__(self, db: DB, *, clock: Callable[[], float]):
        self.db = db
        self.clock = clock
        ensure_schema(db)

    def add(self, family: str, labels: Iterable[str], source: str | None = None) -> None:
        now = self.clock()
        for label in labels:
            if label in KNOWN_LABELS:
                self.db.execute("INSERT OR IGNORE INTO taint_ever (family, label, first_at, source) "
                                "VALUES (?, ?, ?, ?)", (family, label, now, (source or "")[:200]))

    def get(self, family: str) -> set[str]:
        return {r["label"] for r in self.db.all("SELECT label FROM taint_ever WHERE family = ?", (family,))}

    def since(self, family: str) -> dict[str, float]:
        """Label to the time the family first saw it."""
        return {r["label"]: r["first_at"]
                for r in self.db.all("SELECT label, first_at FROM taint_ever WHERE family = ?", (family,))}

    def drop(self, family: str) -> None:
        self.db.execute("DELETE FROM taint_ever WHERE family = ?", (family,))

    def purge(self) -> int:
        """Drop the rows of families whose root grant expired or was revoked."""
        cutoff = self.clock() - EXPIRY_GRACE_S
        return self.db.execute(f"DELETE FROM taint_ever WHERE family IN ({_DEAD_FAMILIES})", (cutoff,)).rowcount


class ReceiptStore:
    """Context receipts (design 4.12): the decisions digest each family last loaded, per workspace."""

    def __init__(self, db: DB, *, clock: Callable[[], float]):
        self.db = db
        self.clock = clock
        ensure_schema(db)

    def record(self, family: str, workspace: str, digest: str) -> None:
        self.db.execute("INSERT INTO context_receipts (family, workspace, digest, at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(family, workspace) DO UPDATE SET digest = excluded.digest, at = excluded.at",
                        (family, workspace, digest, self.clock()))

    def get(self, family: str, workspace: str) -> dict | None:
        row = self.db.one("SELECT digest, at FROM context_receipts WHERE family = ? AND workspace = ?",
                          (family, workspace))
        return {"digest": row["digest"], "at": row["at"]} if row else None

    def state(self, family: str, workspace: str, digest: str) -> ReceiptState:
        got = self.get(family, workspace)
        if got is None:
            return "never"
        return "current" if got["digest"] == digest else "stale"

    def purge(self) -> int:
        cutoff = self.clock() - EXPIRY_GRACE_S
        return self.db.execute(f"DELETE FROM context_receipts WHERE family IN ({_DEAD_FAMILIES})",
                               (cutoff,)).rowcount
