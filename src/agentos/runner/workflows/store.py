"""Workflows' own tables, in runner.db beside the runs they make.

Versions are immutable. Lists, suppression, limits and the send ledger are the anti-spam rule's
state (`sends.py`), and the ledger's primary key is what makes a retry safe: one row per run, step
and address, written before a message leaves.

`site_watches` is what a `site_change` trigger last read (`sitechange.py`). Its `rows` never leave
the node: no view, report or event reads that column but the diff, and it goes with the workflow.
The same goes for the `pending` keys in its `doc`, which are never more than the rows it knows.

`leads` holds what an agent proposed and where it came from (`leads.py`), and `send_threads` and
`send_replies` what the node sent and who answered it (`replies.py`). Neither is in a view, report or
event that leaves the node.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from agentos.runner.store import RunnerStore
from agentos.runner.util import iso, parse_dt
from agentos.runner.workflows.addresses import base_address, base_sql
from agentos.runner.workflows.models import WorkflowDefinition, WorkflowState, WorkflowVersion

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_versions (
  id TEXT NOT NULL, version TEXT NOT NULL, revision INTEGER NOT NULL, definition TEXT NOT NULL,
  saved_by TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (id, version));
CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS send_lists (
  id TEXT PRIMARY KEY, workspace TEXT NOT NULL, name TEXT NOT NULL, supplied_by TEXT NOT NULL, supplied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS send_people (
  list_id TEXT NOT NULL, address TEXT NOT NULL, name TEXT, PRIMARY KEY (list_id, address));
CREATE TABLE IF NOT EXISTS send_suppressed (
  workspace TEXT NOT NULL, address TEXT NOT NULL, by TEXT NOT NULL, why TEXT, at TEXT NOT NULL,
  PRIMARY KEY (workspace, address));
CREATE TABLE IF NOT EXISTS send_limits (account TEXT PRIMARY KEY, daily_limit INTEGER NOT NULL, set_by TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS send_ledger (
  run_id TEXT NOT NULL, path TEXT NOT NULL, address TEXT NOT NULL, account TEXT NOT NULL, state TEXT NOT NULL,
  day TEXT NOT NULL, at TEXT NOT NULL, note TEXT, PRIMARY KEY (run_id, path, address));
CREATE INDEX IF NOT EXISTS send_ledger_day ON send_ledger(account, day, state);
CREATE TABLE IF NOT EXISTS mailboxes (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS site_watches (id TEXT PRIMARY KEY, doc TEXT NOT NULL, rows TEXT);
CREATE TABLE IF NOT EXISTS lead_batches (
  id TEXT PRIMARY KEY, list_id TEXT NOT NULL, workspace TEXT NOT NULL, family TEXT NOT NULL, day TEXT NOT NULL,
  received INTEGER NOT NULL, left_out INTEGER NOT NULL, state TEXT NOT NULL, approval_id TEXT, noted INTEGER NOT NULL DEFAULT 0,
  by TEXT NOT NULL, at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_by TEXT, decided_at TEXT);
CREATE INDEX IF NOT EXISTS lead_batches_state ON lead_batches(state);
CREATE INDEX IF NOT EXISTS lead_batches_day ON lead_batches(day, family);
CREATE TABLE IF NOT EXISTS leads (
  list_id TEXT NOT NULL, address TEXT NOT NULL, batch_id TEXT NOT NULL, name TEXT, company TEXT, source TEXT NOT NULL, why TEXT,
  added_at TEXT NOT NULL, state TEXT NOT NULL, taken_at TEXT, taken_by TEXT, PRIMARY KEY (list_id, address));
CREATE INDEX IF NOT EXISTS leads_batch ON leads(batch_id);
CREATE TABLE IF NOT EXISTS lead_drops (family TEXT NOT NULL, digest TEXT NOT NULL, until TEXT NOT NULL, PRIMARY KEY (family, digest));
CREATE TABLE IF NOT EXISTS send_threads (
  message_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, path TEXT NOT NULL, address TEXT NOT NULL, mailbox TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS send_threads_at ON send_threads(at);
CREATE TABLE IF NOT EXISTS send_replies (
  run_id TEXT NOT NULL, path TEXT NOT NULL, address TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (run_id, path, address));
"""


PEOPLE = ("SELECT p.address, p.name, l.address IS NOT NULL AS lead FROM send_people p LEFT JOIN leads l "
          "ON l.list_id = p.list_id AND l.address = p.address AND l.state = 'taken'")


class WorkflowStore:
    def __init__(self, store: RunnerStore):
        self.runner_store = store
        with store._lock:
            store._db.executescript(SCHEMA)
        self._exec, self._rows = store._exec, store._rows

    # ---- versions and state ----
    def put_version(self, wv: WorkflowVersion) -> None:
        self._exec("INSERT OR IGNORE INTO workflow_versions (id, version, revision, definition, saved_by, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   (wv.id, wv.version, wv.revision, json.dumps(wv.definition.canonical()), json.dumps(wv.saved_by),
                    iso(wv.created_at)))

    def set_saved_by(self, flow_id: str, version: str, saved_by: dict[str, Any]) -> None:
        self._exec("UPDATE workflow_versions SET saved_by = ? WHERE id = ? AND version = ?",
                   (json.dumps(saved_by), flow_id, version))

    @staticmethod
    def _version(row: Any) -> WorkflowVersion:
        return WorkflowVersion(definition=WorkflowDefinition.model_validate(json.loads(row["definition"])),
                               version=row["version"], revision=row["revision"], saved_by=json.loads(row["saved_by"]),
                               created_at=parse_dt(row["created_at"]))

    def version(self, flow_id: str, version: str) -> WorkflowVersion | None:
        rows = self._rows("SELECT * FROM workflow_versions WHERE id = ? AND version = ?", (flow_id, version))
        return self._version(rows[0]) if rows else None

    def put_state(self, state: WorkflowState) -> None:
        self._exec("INSERT OR REPLACE INTO workflows (id, doc) VALUES (?, ?)", (state.id, state.model_dump_json()))

    def state(self, flow_id: str) -> WorkflowState | None:
        rows = self._rows("SELECT doc FROM workflows WHERE id = ?", (flow_id,))
        return WorkflowState.model_validate_json(rows[0]["doc"]) if rows else None

    def states(self) -> list[WorkflowState]:
        return [WorkflowState.model_validate_json(r["doc"]) for r in self._rows("SELECT doc FROM workflows ORDER BY id")]

    def delete(self, flow_id: str) -> None:
        self._exec("DELETE FROM workflows WHERE id = ?", (flow_id,))  # versions stay: old runs still read them
        self.drop_watch(flow_id)

    # ---- what a site_change trigger last read ----
    def watch(self, flow_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT doc FROM site_watches WHERE id = ?", (flow_id,))
        return json.loads(rows[0]["doc"]) if rows else None

    def watch_rows(self, flow_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT rows FROM site_watches WHERE id = ?", (flow_id,))
        return json.loads(rows[0]["rows"]) if rows and rows[0]["rows"] is not None else None

    def put_watch(self, flow_id: str, doc: dict[str, Any], rows: str | None = None) -> None:
        """`rows`, as JSON, replaces the baseline. Without them the baseline stays as it is."""
        self._exec("INSERT INTO site_watches (id, doc, rows) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                   "doc = excluded.doc, rows = COALESCE(excluded.rows, site_watches.rows)",
                   (flow_id, json.dumps(doc), rows))

    def drop_watch(self, flow_id: str) -> None:
        self._exec("DELETE FROM site_watches WHERE id = ?", (flow_id,))

    # ---- lists ----
    def put_list(self, list_id: str, workspace: str, name: str, people: list[tuple[str, str | None]], by: str,
                 at: datetime) -> None:
        store = self.runner_store
        with store._lock:
            store._db.execute("BEGIN IMMEDIATE")
            try:
                store._db.execute("INSERT OR REPLACE INTO send_lists (id, workspace, name, supplied_by, supplied_at) "
                                  "VALUES (?, ?, ?, ?, ?)", (list_id, workspace, name, by, iso(at)))
                store._db.execute("DELETE FROM send_people WHERE list_id = ?", (list_id,))
                store._db.executemany("INSERT OR REPLACE INTO send_people (list_id, address, name) VALUES (?, ?, ?)",
                                      [(list_id, a, n) for a, n in people])
                store._db.execute("COMMIT")
            except Exception:
                store._db.execute("ROLLBACK")
                raise

    def list_info(self, list_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM send_lists WHERE id = ?", (list_id,))
        return dict(rows[0]) if rows else None

    def lists(self, workspace: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM send_lists", ()
        if workspace:
            sql, args = sql + " WHERE workspace = ?", (workspace,)
        return [dict(r) for r in self._rows(sql + " ORDER BY id", args)]

    def people(self, list_id: str) -> list[dict[str, Any]]:
        """Everyone on a list. `lead` is true for one who came as a lead: their name is a stranger's text."""
        return [dict(r) for r in self._rows(f"{PEOPLE} WHERE p.list_id = ? ORDER BY p.address", (list_id,))]

    def person(self, list_id: str, address: str) -> dict[str, Any] | None:
        rows = self._rows(f"{PEOPLE} WHERE p.list_id = ? AND {base_sql('p.address')} = ?", (list_id, base_address(address)))
        return dict(rows[0]) if rows else None

    def list_size(self, list_id: str) -> int:
        return self._rows("SELECT COUNT(*) AS n FROM send_people WHERE list_id = ?", (list_id,))[0]["n"]

    def suppressed_on_list(self, list_id: str, workspace: str) -> int:
        return self._rows(f"SELECT COUNT(*) AS n FROM send_people p JOIN send_suppressed s ON s.address = {base_sql('p.address')} "
                          "WHERE p.list_id = ? AND s.workspace = ?", (list_id, workspace))[0]["n"]

    # ---- suppression ----
    def suppress(self, workspace: str, address: str, by: str, why: str | None, at: datetime) -> bool:
        cur = self._exec("INSERT OR IGNORE INTO send_suppressed (workspace, address, by, why, at) VALUES (?, ?, ?, ?, ?)",
                         (workspace, base_address(address), by, why, iso(at)))
        return cur.rowcount > 0

    def unsuppress(self, workspace: str, address: str) -> bool:
        return self._exec("DELETE FROM send_suppressed WHERE workspace = ? AND address = ?",
                          (workspace, base_address(address))).rowcount > 0

    def is_suppressed(self, workspace: str, address: str) -> bool:
        return bool(self._rows("SELECT 1 FROM send_suppressed WHERE workspace = ? AND address = ?", (workspace, base_address(address))))

    # ---- limits ----
    def daily_limit(self, account: str) -> int | None:
        rows = self._rows("SELECT daily_limit FROM send_limits WHERE account = ?", (account,))
        return rows[0]["daily_limit"] if rows else None

    def set_daily_limit(self, account: str, limit: int, by: str, at: datetime) -> None:
        self._exec("INSERT OR REPLACE INTO send_limits (account, daily_limit, set_by, at) VALUES (?, ?, ?, ?)",
                   (account, limit, by, iso(at)))

    # ---- the ledger ----
    def ledger_get(self, run_id: str, path: str, address: str) -> str | None:
        rows = self._rows("SELECT state FROM send_ledger WHERE run_id = ? AND path = ? AND address = ?",
                          (run_id, path, address))
        return rows[0]["state"] if rows else None

    def ledger_rows(self, run_id: str, path: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows("SELECT address, state, note FROM send_ledger WHERE run_id = ? AND path = ?",
                                            (run_id, path))]

    def ledger_claim(self, run_id: str, path: str, address: str, account: str, day: str, at: datetime,
                     limit: int) -> str:
        """Take the next slot for this address in one transaction: `claimed` (a `sending` row now
        exists and the day's count is one higher), `limit` (the day is used up), or the state the
        address already has. Two runs cannot both pass the limit, and one address is claimed once."""
        store = self.runner_store
        with store._lock:
            store._db.execute("BEGIN IMMEDIATE")
            try:
                row = store._db.execute("SELECT state FROM send_ledger WHERE run_id = ? AND path = ? AND address = ?",
                                        (run_id, path, address)).fetchone()
                if row is not None:
                    store._db.execute("COMMIT")
                    return row["state"]
                used = store._db.execute("SELECT COUNT(*) AS n FROM send_ledger WHERE account = ? AND day = ? AND state IN "
                                         "('sending', 'sent', 'unknown')", (account, day)).fetchone()["n"]
                if used >= limit:
                    store._db.execute("COMMIT")
                    return "limit"
                store._db.execute("INSERT INTO send_ledger (run_id, path, address, account, state, day, at) "
                                  "VALUES (?, ?, ?, ?, 'sending', ?, ?)", (run_id, path, address, account, day, iso(at)))
                store._db.execute("COMMIT")
                return "claimed"
            except Exception:
                store._db.execute("ROLLBACK")
                raise

    def ledger_claim_many(self, run_id: str, path: str, count: int, account: str, day: str, at: datetime,
                          limit: int) -> str:
        """`count` slots for one step whose addresses nobody can see, all or none, in one transaction:
        `claimed`, or `limit` when the day cannot hold them. Claiming again for the same path takes
        no more, so a step that comes back after a stop is not charged twice."""
        store = self.runner_store
        with store._lock:
            store._db.execute("BEGIN IMMEDIATE")
            try:
                held = store._db.execute("SELECT COUNT(*) AS n FROM send_ledger WHERE run_id = ? AND path = ?",
                                         (run_id, path)).fetchone()["n"]
                need = count - held
                if need <= 0:
                    store._db.execute("COMMIT")
                    return "claimed"
                used = store._db.execute("SELECT COUNT(*) AS n FROM send_ledger WHERE account = ? AND day = ? AND state IN "
                                         "('sending', 'sent', 'unknown')", (account, day)).fetchone()["n"]
                if used + need > limit:
                    store._db.execute("COMMIT")
                    return "limit"
                store._db.executemany("INSERT INTO send_ledger (run_id, path, address, account, state, day, at) "
                                      "VALUES (?, ?, ?, ?, 'sending', ?, ?)",
                                      [(run_id, path, f"item:{i}", account, day, iso(at)) for i in range(held, count)])
                store._db.execute("COMMIT")
                return "claimed"
            except Exception:
                store._db.execute("ROLLBACK")
                raise

    def ledger_settle(self, run_id: str, path: str, state: str) -> int:
        """What became of a counted step: `sent`, `failed` (it gives its slots back), or `unknown`."""
        return self._exec("UPDATE send_ledger SET state = ? WHERE run_id = ? AND path = ? AND state = 'sending'",
                          (state, run_id, path)).rowcount

    def ledger_mark(self, run_id: str, path: str, address: str, account: str, state: str, day: str, at: datetime,
                    note: str | None = None) -> None:
        self._exec("INSERT INTO send_ledger (run_id, path, address, account, state, day, at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                   "ON CONFLICT(run_id, path, address) DO UPDATE SET state = excluded.state, note = excluded.note, at = excluded.at",
                   (run_id, path, address, account, state, day, iso(at), note))

    def ledger_clear_failed(self, run_id: str, path: str) -> int:
        """Forget what certainly never left, so a retry can try those addresses again."""
        return self._exec("DELETE FROM send_ledger WHERE run_id = ? AND path = ? AND state = 'failed'",
                          (run_id, path)).rowcount

    def ledger_mark_lost(self, run_id: str, path: str) -> int:
        """A `sending` row nobody finished (the node stopped): it may have left, so it is in doubt."""
        return self._exec("UPDATE send_ledger SET state = 'unknown' WHERE run_id = ? AND path = ? AND state = 'sending'",
                          (run_id, path)).rowcount

    def sent_on(self, account: str, day: str) -> int:
        return self._rows("SELECT COUNT(*) AS n FROM send_ledger WHERE account = ? AND day = ? AND state IN "
                          "('sending', 'sent', 'unknown')", (account, day))[0]["n"]

    # ---- leads ----
    def lead_batch_put(self, batch: dict[str, Any], leads: list[dict[str, Any]], *, per_family: int, per_list: int) -> str:
        """A batch and its leads in one transaction: `ok`, or the bound it would pass (`family`, `list`).
        What counts is what was received, so the answer says nothing of what the queue left out."""
        store = self.runner_store
        with store._lock:
            store._db.execute("BEGIN IMMEDIATE")
            try:
                sql = "SELECT COALESCE(SUM(received), 0) AS n FROM lead_batches WHERE day = ? AND "
                family = store._db.execute(f"{sql}family = ?", (batch["day"], batch["family"])).fetchone()["n"]
                listed = store._db.execute(f"{sql}list_id = ?", (batch["day"], batch["list_id"])).fetchone()["n"]
                over = "family" if family + batch["received"] > per_family else \
                    "list" if listed + batch["received"] > per_list else None
                if over is not None:
                    store._db.execute("COMMIT")
                    return over
                store._db.execute("INSERT INTO lead_batches (id, list_id, workspace, family, day, received, left_out, state, "
                                  "by, at, expires_at) VALUES (:id, :list_id, :workspace, :family, :day, :received, :left_out, "
                                  ":state, :by, :at, :expires_at)", batch)
                store._db.executemany("INSERT OR IGNORE INTO leads (list_id, address, batch_id, name, company, source, why, "
                                      "added_at, state) VALUES (:list_id, :address, :batch_id, :name, :company, :source, :why, "
                                      ":added_at, 'waiting')", leads)
                store._db.execute("COMMIT")
                return "ok"
            except Exception:
                store._db.execute("ROLLBACK")
                raise

    def lead_batch_card(self, batch_id: str, approval_id: str | None, *, noted: bool | None = None) -> None:
        self._exec("UPDATE lead_batches SET approval_id = ?, noted = COALESCE(?, noted) WHERE id = ?",
                   (approval_id, noted, batch_id))

    def lead_batch(self, batch_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM lead_batches WHERE id = ?", (batch_id,))
        return dict(rows[0]) if rows else None

    def lead_batches(self, state: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM lead_batches", ()
        if state:
            sql, args = sql + " WHERE state = ?", (state,)
        return [dict(r) for r in self._rows(sql + " ORDER BY at, id", args)]

    def lead_batches_with_a_card(self) -> list[dict[str, Any]]:
        """Batches a person decided elsewhere, whose card still waits."""
        return [dict(r) for r in self._rows("SELECT * FROM lead_batches WHERE approval_id IS NOT NULL AND state != 'waiting'")]

    def leads_of(self, batch_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows("SELECT * FROM leads WHERE batch_id = ? AND state = 'waiting' ORDER BY address",
                                            (batch_id,))]

    def lead_known(self, list_id: str, address: str) -> bool:
        return bool(self._rows(f"SELECT 1 FROM leads WHERE list_id = ? AND {base_sql('address')} = ?",
                               (list_id, base_address(address))))

    def lead_dropped(self, family: str, digest: str, now: datetime) -> bool:
        return bool(self._rows("SELECT 1 FROM lead_drops WHERE family = ? AND digest = ? AND until > ?",
                               (family, digest, iso(now))))

    def lead_drops_prune(self, now: datetime) -> None:
        self._exec("DELETE FROM lead_drops WHERE until <= ?", (iso(now),))

    def lead_batch_close(self, batch_id: str, state: str, take: list[dict[str, Any]], drops: list[str], until: datetime,
                         by: str, at: datetime, max_size: int) -> str:
        """What a person decided, in one transaction: `take` joins the list, every other waiting lead
        of the batch goes, source and all, and `drops` are remembered. `closed` if it was decided
        before, `full` if the list cannot hold them, else `ok`."""
        store = self.runner_store
        with store._lock:
            store._db.execute("BEGIN IMMEDIATE")
            try:
                row = store._db.execute("SELECT list_id, family, state FROM lead_batches WHERE id = ?", (batch_id,)).fetchone()
                if row is None or row["state"] != "waiting":
                    store._db.execute("COMMIT")
                    return "closed"
                list_id = row["list_id"]
                size = store._db.execute("SELECT COUNT(*) AS n FROM send_people WHERE list_id = ?", (list_id,)).fetchone()["n"]
                if size + len(take) > max_size:
                    store._db.execute("COMMIT")
                    return "full"
                store._db.executemany("INSERT OR IGNORE INTO send_people (list_id, address, name) VALUES (?, ?, ?)",
                                      [(list_id, x["address"], x["name"]) for x in take])
                store._db.executemany("UPDATE leads SET state = 'taken', taken_at = ?, taken_by = ? WHERE list_id = ? AND address = ?",
                                      [(iso(at), by, list_id, x["address"]) for x in take])
                store._db.execute("DELETE FROM leads WHERE batch_id = ? AND state = 'waiting'", (batch_id,))
                store._db.executemany("INSERT OR REPLACE INTO lead_drops (family, digest, until) VALUES (?, ?, ?)",
                                      [(row["family"], d, iso(until)) for d in drops])
                store._db.execute("UPDATE lead_batches SET state = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                                  (state, by, iso(at), batch_id))
                store._db.execute("COMMIT")
                return "ok"
            except Exception:
                store._db.execute("ROLLBACK")
                raise

    def leads_on(self, list_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows("SELECT address, name, company, source, why, added_at, state, taken_at, taken_by "
                                            "FROM leads WHERE list_id = ? ORDER BY added_at, address", (list_id,))]

    def lead_erase(self, workspace: str, address: str, digest: str, until: datetime) -> dict[str, int]:
        """A lead and its source, everywhere in a workspace, with who answered and what was sent to
        them. Suppression, the ledger and the unsubscribe token stay: those are what keep mail from
        going to them, and a day's count. The family that proposed them stays out for as long as a
        dropped lead does, so erasing is not a way back on, and only a digest remembers that."""
        store = self.runner_store
        with store._lock:
            store._db.execute("BEGIN IMMEDIATE")
            try:
                found = store._db.execute(
                    "SELECT l.list_id, b.family FROM leads l JOIN send_lists s ON s.id = l.list_id "
                    "LEFT JOIN lead_batches b ON b.id = l.batch_id WHERE s.workspace = ? AND l.address = ?",
                    (workspace, address)).fetchall()
                lists = [r["list_id"] for r in found]
                on_list = sum(store._db.execute("DELETE FROM send_people WHERE list_id = ? AND address = ?", (x, address)).rowcount
                              for x in lists)
                for x in lists:
                    store._db.execute("DELETE FROM leads WHERE list_id = ? AND address = ?", (x, address))
                store._db.executemany("INSERT OR REPLACE INTO lead_drops (family, digest, until) VALUES (?, ?, ?)",
                                      [(r["family"], digest, iso(until)) for r in found if r["family"]])
                if lists:  # another business's mail to the same address is not this one's to forget
                    here = (address, f"{workspace}/%")
                    store._db.execute("DELETE FROM send_replies WHERE address = ? AND run_id IN (SELECT run_id FROM send_threads "
                                      "WHERE address = ? AND mailbox LIKE ?)", (address, *here))
                    store._db.execute("DELETE FROM send_threads WHERE address = ? AND mailbox LIKE ?", here)
                store._db.execute("COMMIT")
                return {"leads": len(lists), "on_lists": on_list}
            except Exception:
                store._db.execute("ROLLBACK")
                raise

    # ---- what was sent, and who answered ----
    def thread_put(self, message_id: str, run_id: str, path: str, address: str, mailbox: str, at: datetime) -> None:
        self._exec("INSERT OR REPLACE INTO send_threads (message_id, run_id, path, address, mailbox, at) VALUES (?, ?, ?, ?, ?, ?)",
                   (message_id, run_id, path, address, mailbox, iso(at)))

    def threads_prune(self, before: datetime) -> None:
        self._exec("DELETE FROM send_threads WHERE at < ?", (iso(before),))

    def threads(self, message_ids: list[str], mailbox: str) -> dict[str, dict[str, Any]]:
        """What this mailbox sent under these ids, by id."""
        marks = ", ".join("?" * len(message_ids))
        rows = self._rows(f"SELECT * FROM send_threads WHERE mailbox = ? AND message_id IN ({marks})", (mailbox, *message_ids))
        return {r["message_id"]: dict(r) for r in rows}

    def reply_put(self, run_id: str, path: str, address: str, at: datetime) -> None:
        self._exec("INSERT OR IGNORE INTO send_replies (run_id, path, address, at) VALUES (?, ?, ?, ?)",
                   (run_id, path, address, iso(at)))

    def replied(self, run_id: str, path: str) -> set[str]:
        return {r["address"] for r in self._rows("SELECT address FROM send_replies WHERE run_id = ? AND path = ?", (run_id, path))}

    # ---- mailboxes ----
    def put_mailbox(self, mailbox_id: str, doc: dict[str, Any]) -> None:
        self._exec("INSERT OR REPLACE INTO mailboxes (id, doc) VALUES (?, ?)", (mailbox_id, json.dumps(doc)))

    def mailbox(self, mailbox_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT doc FROM mailboxes WHERE id = ?", (mailbox_id,))
        return json.loads(rows[0]["doc"]) if rows else None

    def mailboxes(self) -> list[dict[str, Any]]:
        return [json.loads(r["doc"]) for r in self._rows("SELECT doc FROM mailboxes ORDER BY id")]

    def delete_mailbox(self, mailbox_id: str) -> bool:
        return self._exec("DELETE FROM mailboxes WHERE id = ?", (mailbox_id,)).rowcount > 0


def today(at: datetime, tz: Any = UTC) -> str:
    return at.astimezone(tz).date().isoformat()
