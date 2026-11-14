"""The action ledger: what each account (lane) did, by kind, whoever made it do it.

Caps such as "10 likes a day" mean the account's likes, not one engine's. The engagement engine
counted only its own operations, so likes an agent made over MCP, a pilot run, a flow or a workflow
on the same account went past its caps unseen. Every one of those paths leaves through the lane's
egress guard, which already names what a request does (agentos.node.egress EFFECT_RULES, by host,
path and GraphQL or LinkedIn operation name). So the guard is the one place that sees them all: when
a request an effect rule names finishes (the site answered), the lane manager hands it here and one
row goes in. A request the guard refused, or one whose failure proves it never reached the site (no
name, no connection), is not counted. One the site answered with an error is, and so is one that
failed after it may have left (the page went away, the connection dropped): the ledger stays on the
side of more. Every action of a batch counts, and a redirect chain counts once. The actor is who
drove the lane when the request left, not when it was answered.

    ledger = runtime.get("ledger")
    ledger.debit("biz-a/instagram", "like", actor="agent", rule="instagram.like")
    ledger.count("biz-a/instagram", "like", since=t0, until=t1)               # one kind, a window
    ledger.day_count("biz-a/instagram", "like", day, ZoneInfo("Europe/Vilnius"))
    ledger.hour_count("biz-a/instagram", "like", now)                         # the rolling hour
    ledger.counts("biz-a/instagram", since=t0)                                # every kind at once

Kinds are the effect kinds: post, comment, like, follow, join, profile (an unlike counts as a like:
the site sees an action either way). `actor` is who drove the lane: agent (an MCP agent or a pilot
run through the node's tools), flow (a recorded flow, a connector run, a workflow step, an
engagement operation) or person (the owner in control). Counts leave out `person` unless asked:
a person's own clicks are theirs to make, and no cap of the node's stops them.

The ledger only counts: it refuses nothing, so a deploy changes no program by itself. A reader (the
engagement engine's caps) decides what a count means. Rows older than KEEP_DAYS are pruned.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("agentos.policy.ledger")

SERVICE_NAME = "ledger"
KINDS = frozenset({"post", "comment", "like", "follow", "join", "profile"})
ACTORS = frozenset({"agent", "flow", "person"})
AUTOMATED: tuple[str, ...] = ("agent", "flow")   # what a count covers unless the caller asks otherwise
KEEP_DAYS = 8
PRUNE_EVERY_S = 3600.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    kind TEXT NOT NULL,
    at REAL NOT NULL,
    actor TEXT NOT NULL,
    rule TEXT
);
CREATE INDEX IF NOT EXISTS actions_account_kind_at ON actions (account, kind, at);
"""


def _ts(at: datetime | float) -> float:
    return at.timestamp() if isinstance(at, datetime) else float(at)


class ActionLedger:
    """Per account, per kind: one row per action that left. Thread safe; sqlite at `path`."""

    def __init__(self, path: Path | str | None, *, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._lock = threading.Lock()
        target = ":memory:" if path is None else str(path)
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        self._db.executescript(SCHEMA)
        self._pruned_at = 0.0

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- writing -------------------------------------------------------------------------

    def debit(self, account: str, kind: str, *, actor: str, rule: str | None = None,
              at: datetime | float | None = None) -> int | None:
        """One action of `kind` on `account`. None (and nothing stored) for a kind that is no action."""
        if kind not in KINDS:
            return None
        if actor not in ACTORS:
            raise ValueError(f"actor is one of {sorted(ACTORS)}, not {actor!r}")
        t = self.clock() if at is None else _ts(at)
        with self._lock:
            cur = self._db.execute("INSERT INTO actions (account, kind, at, actor, rule) VALUES (?, ?, ?, ?, ?)",
                                   (account, kind, t, actor, rule))
            if t - self._pruned_at > PRUNE_EVERY_S:
                self._pruned_at = t
                self._db.execute("DELETE FROM actions WHERE at < ?", (t - KEEP_DAYS * 86400,))
            return int(cur.lastrowid or 0)

    def on_effect(self, lane_id: str, kind: str, rule: str | None, actor: str) -> None:
        """The lane manager's effect listener (LaneManager.add_effect_listener)."""
        try:
            self.debit(lane_id, kind, actor=actor, rule=rule)
        except Exception:
            log.exception("%s: could not record a %s on the action ledger", lane_id, kind)

    # ---- reading -------------------------------------------------------------------------

    def count(self, account: str, kind: str, *, since: datetime | float, until: datetime | float | None = None,
              actors: Iterable[str] = AUTOMATED) -> int:
        """Actions of `kind` on `account` in [since, until)."""
        who = sorted(set(actors))
        if not who:
            return 0
        sql = (f"SELECT COUNT(*) FROM actions WHERE account = ? AND kind = ? AND at >= ? "
               f"AND actor IN ({', '.join('?' * len(who))})")
        args: list[Any] = [account, kind, _ts(since), *who]
        if until is not None:
            sql, args = sql + " AND at < ?", [*args, _ts(until)]
        with self._lock:
            return int(self._db.execute(sql, args).fetchone()[0])

    def counts(self, account: str, *, since: datetime | float, until: datetime | float | None = None,
               actors: Iterable[str] = AUTOMATED) -> dict[str, int]:
        """Every kind on `account` in [since, until), kinds with none left out."""
        return {k: n for k in sorted(KINDS) if (n := self.count(account, k, since=since, until=until, actors=actors))}

    def day_count(self, account: str, kind: str, day: date, tz: ZoneInfo, *,
                  actors: Iterable[str] = AUTOMATED) -> int:
        """Actions on the local calendar `day` in `tz`."""
        start = datetime.combine(day, dtime(0), tz)
        end = datetime.combine(day + timedelta(days=1), dtime(0), tz)
        return self.count(account, kind, since=start, until=end, actors=actors)

    def hour_count(self, account: str, kind: str, now: datetime | float | None = None, *,
                   actors: Iterable[str] = AUTOMATED) -> int:
        """Actions in the hour before `now` (the rolling hour)."""
        t = self.clock() if now is None else _ts(now)
        return self.count(account, kind, since=t - 3600.0, until=t + 1e-6, actors=actors)

    def recent(self, account: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT kind, at, actor, rule FROM actions WHERE account = ? "
                                    "ORDER BY at DESC LIMIT ?", (account, limit)).fetchall()
        return [{"kind": k, "at": datetime.fromtimestamp(at, UTC).isoformat(), "actor": a, "rule": r}
                for k, at, a, r in rows]

    # ---- wiring --------------------------------------------------------------------------

    def attach_lanes(self, runtime: Any) -> bool:
        """Composition hook: every effect any lane's guard sees goes on this ledger."""
        lanes = runtime.get("lanes") if runtime is not None else None
        add = getattr(lanes, "add_effect_listener", None)
        if not callable(add):
            return False
        add(self.on_effect)
        return True


def ledger_path(home: Path | str) -> Path:
    return Path(home) / "policy" / "ledger.sqlite"


def build(runtime: Any, home: Path | str | None = None) -> ActionLedger:
    """Register the ledger as "ledger" and hear every lane's effects. In memory with no home."""
    ledger = ActionLedger(ledger_path(home) if home is not None else None)
    if runtime is not None:
        runtime.register(SERVICE_NAME, ledger)
        if not ledger.attach_lanes(runtime):  # nothing would ever reach it: the daily caps would count none
            log.warning("the action ledger cannot hear this node's lanes (its lane manager has no "
                        "add_effect_listener): likes, follows and posts are not counted")
    return ledger
