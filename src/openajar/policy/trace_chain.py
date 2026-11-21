"""The action log's hash chain and signed marks: the part of TraceStore that links each row to the
one before it as it is inserted, writes the genesis, checkpoints, retention anchors and key
rotations into `audit_marks`, and verifies it all (openajar.policy.auditlog). A mixin, so the
store stays one object with one database; AUDIT-LOG.md has the format.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from typing import Any

from openajar.core.errors import InvalidRequest
from openajar.policy import auditlog
from openajar.policy.auditlog import SIG_CTX, Report, Scope, canonical, mark_hash, pub_text, row_hash, sign
from openajar.policy.util import iso, new_id

log = logging.getLogger("openajar.traces")  # the store's own logger: one log for the whole action log
_INSERT = ("INSERT OR IGNORE INTO traces (seq, id, at, kind, principal, principal_kind, grant_id, grant_path, tool,"
           " lane, workspace, effect, status, summary, args, error, cost_tokens, cost_usd, duration_ms, run_id,"
           " parent_id, prev, hash) VALUES (:seq, :id, :at, :kind, :principal, :principal_kind, :grant_id,"
           " :grant_path, :tool, :lane, :workspace, :effect, :status, :summary, :args, :error, :cost_tokens,"
           " :cost_usd, :duration_ms, :run_id, :parent_id, :prev, :hash)")
# a row (not the database) is at fault: it becomes a stub instead of waiting forever. When the stub
# fails too (a closed or broken database), the error reaches the caller and the rows wait.
_ROW_ERRORS = (sqlite3.IntegrityError, sqlite3.InterfaceError, sqlite3.DataError, sqlite3.ProgrammingError,
               OverflowError, ValueError, TypeError)


class TraceChainMixin:
    def _init_chain(self) -> None:
        """Add the chain columns to a log made before them and write the genesis mark once:
        rows already there stay unchained (the genesis says up to which seq), never chained after
        the fact."""
        trust: tuple | None = None
        try:
            with self._tx() as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(traces)").fetchall()}
                for col in ("prev", "hash"):
                    if col not in cols:
                        c.execute(f"ALTER TABLE traces ADD COLUMN {col} TEXT")
                if c.execute("SELECT 1 FROM audit_marks LIMIT 1").fetchone() is None:
                    trust = self._write_genesis(c)
                self._genesis = self._load_genesis(c)
                last = c.execute("SELECT body FROM audit_marks WHERE kind = 'checkpoint' ORDER BY n DESC LIMIT 1"
                                 ).fetchone()
                if last is not None:
                    body = json.loads(last[0])
                    head = self._head(c)[0]
                    self._since_cp = max(0, head - int(body.get("seq") or 0))
        except (sqlite3.Error, ValueError):
            log.exception("the audit chain could not start; rows are kept and chained once it does")
            return
        if trust is not None:
            try:
                self.keys.add_trust(*trust)
            except OSError:
                log.exception("audit trust file not written")

    def _write_genesis(self, c: sqlite3.Connection) -> tuple | None:
        used = auditlog.max_seq_used(c) or 0
        after = max(int(c.execute("SELECT COALESCE(MAX(seq), 0) FROM traces").fetchone()[0]), used)
        unchained = int(c.execute("SELECT COUNT(*) FROM traces").fetchone()[0])
        chain = new_id("ch")
        try:
            kid, key = self.keys.create()
            pub: str | None = pub_text(key)
        except OSError:
            log.exception("audit key not created; the genesis mark is unsigned")
            kid, key, pub = None, None, None
        now = self.clock()
        body = {"v": auditlog.FORMAT, "kind": "genesis", "chain": chain, "n": 1, "prev": None, "at": iso(now),
                "kid": kid, "pub": pub, "after_seq": after, "unchained": unchained}
        data = canonical(body)
        c.execute("INSERT INTO audit_marks (n, kind, body, hash, sig) VALUES (1, 'genesis', ?, ?, ?)",
                  (data.decode(), mark_hash(body), sign(key, SIG_CTX, data) if key else None))
        if unchained:
            log.warning("audit chain started after seq %s: %s earlier rows stay unchained", after, unchained)
        return (chain, kid, pub, now) if kid and pub else None

    @staticmethod
    def _load_genesis(c: sqlite3.Connection) -> dict | None:
        row = c.execute("SELECT body, hash FROM audit_marks WHERE n = 1 AND kind = 'genesis'").fetchone()
        if row is None:
            return None
        return {**json.loads(row[0]), "hash": row[1]}

    def _head(self, c: sqlite3.Connection) -> tuple[int, str]:
        """(the highest seq used, the hash the next row links to). After retention removed every
        row up to the newest, the head is the retention mark's "pruned through seq N, hash H".
        Cached between this store's own transactions: `PRAGMA data_version` changes when any
        other connection commits, and only then is the head read again."""
        # data_version is per connection, and each thread has its own: the key names both
        dv = c.execute("PRAGMA data_version").fetchone()[0]
        cache = self._head_cache
        if cache is not None and cache[0][0] is c and cache[0][1] == dv:
            return cache[1], cache[2]
        seq, h = self._read_head(c)
        self._head_cache = ((c, dv), seq, h)
        return seq, h

    def _read_head(self, c: sqlite3.Connection) -> tuple[int, str]:
        """The head, never below a seq the log already used (SQLite's counter): a seq is never
        given twice, even after retention emptied the table and the store was opened again."""
        seq, h = self._chain_head(c)
        return max(seq, auditlog.max_seq_used(c) or 0), h

    def _chain_head(self, c: sqlite3.Connection) -> tuple[int, str]:
        g = self._genesis or {}
        base = (int(g.get("after_seq") or 0), str(g.get("hash") or ""))
        r = c.execute("SELECT seq, hash FROM traces ORDER BY seq DESC LIMIT 1").fetchone()
        floor = self._floor(c)
        if floor is not None and (r is None or int(r[0]) <= floor[0]):
            return (floor[0], floor[1]) if floor[1] else (max(floor[0], base[0]), base[1])
        if r is None:
            return base
        if r[1] is not None:
            return int(r[0]), str(r[1])
        chained = c.execute("SELECT hash FROM traces WHERE hash IS NOT NULL ORDER BY seq DESC LIMIT 1").fetchone()
        return max(int(r[0]), base[0]), str(chained[0]) if chained else base[1]

    def _floor(self, c: sqlite3.Connection) -> tuple[int, str | None] | None:
        """How far retention has removed the log: the highest (through_seq, through_hash) of every
        retention mark (an older node could write one below the one before), cached until a mark is
        added."""
        n = c.execute("SELECT MAX(n) FROM audit_marks").fetchone()[0]
        if n != self._floor_n:
            best: tuple[int, str | None] | None = None
            for (text,) in c.execute("SELECT body FROM audit_marks WHERE kind = 'pruned' ORDER BY n").fetchall():
                try:
                    body = json.loads(text)
                    mark = (int(body["through_seq"]), body.get("through_hash"))
                except (ValueError, KeyError, TypeError):
                    continue
                if best is None or mark[0] >= best[0]:
                    best = mark
            self._floor_v = best
            self._floor_n = n
        return self._floor_v

    def _insert(self, c: sqlite3.Connection, rows: Iterable[dict]) -> int:
        """Chain and insert rows in order; a row whose id is already there (a spill file
        replayed after a crash) is skipped and takes no seq."""
        seq, prev = self._head(c)
        chained = self._genesis is not None
        n = 0
        for row in rows:
            try:
                rec, done = self._insert_one(c, row, seq, prev, chained)
            except _ROW_ERRORS:
                # this one row cannot be stored (a value SQLite refuses): a stub keeps its place, so
                # it never blocks the rows behind it
                log.exception("trace %s could not be stored; a stub was written instead", str(row.get("id"))[:40])
                rec, done = self._insert_one(c, self._stub_row(row), seq, prev, chained)
            if done:
                seq += 1
                prev = rec["hash"] or prev
                n += 1
        if self._head_cache is not None:
            self._head_cache = (self._head_cache[0], seq, prev)
        return n

    @staticmethod
    def _insert_one(c: sqlite3.Connection, row: dict, seq: int, prev: str, chained: bool) -> tuple[dict, bool]:
        if row.get("id") is not None and c.execute("SELECT 1 FROM traces WHERE id = ?", (row["id"],)).fetchone():
            # already in the log (a spill file read twice): an ignored INSERT would still move SQLite's
            # seq counter past the head, and verify would read that as rows deleted from the end
            return {**row}, False
        rec = {**row, "seq": seq + 1}
        h = row_hash(prev, rec) if chained else None
        rec["prev"], rec["hash"] = (prev, h) if chained else (None, None)
        return rec, bool(c.execute(_INSERT, rec).rowcount)

    def _signing_key(self, c: sqlite3.Connection) -> tuple[str | None, Any]:
        """The audit key the log's own marks make active (the latest genesis or key mark)."""
        row = c.execute("SELECT kind, body FROM audit_marks WHERE kind IN ('genesis', 'key') ORDER BY n DESC LIMIT 1"
                        ).fetchone()
        if row is None:
            return None, None
        body = json.loads(row[1])
        kid = body.get("next_kid") if row[0] == "key" else body.get("kid")
        return kid, self.keys.private(kid)

    def _add_mark(self, c: sqlite3.Connection, kind: str, fields: dict, *, signer: tuple | None = None) -> dict:
        last = c.execute("SELECT n, hash FROM audit_marks ORDER BY n DESC LIMIT 1").fetchone()
        n, prev = (int(last[0]) + 1, last[1]) if last else (1, None)
        kid, key = signer or self._signing_key(c)
        if key is None:
            log.error("audit key %s is not on this node: mark %s (%s) is written unsigned", kid, n, kind)
        body = {"v": auditlog.FORMAT, "kind": kind, "chain": (self._genesis or {}).get("chain"), "n": n,
                "prev": prev, "at": iso(self.clock()), "kid": kid, **fields}
        data = canonical(body)
        c.execute("INSERT INTO audit_marks (n, kind, body, hash, sig) VALUES (?, ?, ?, ?, ?)",
                  (n, kind, data.decode(), mark_hash(body), sign(key, SIG_CTX, data) if key is not None else None))
        return body

    def checkpoint(self, reason: str = "manual") -> dict | None:
        """Sign the chain head (seq, hash, time, count). None when nothing new was written since
        the last checkpoint, or the log is not chained yet. Never raises on a locked database."""
        with self._lock:
            if self._genesis is None:
                return None
            try:
                with self._tx() as c:
                    seq, h = self._head(c)
                    after = int(self._genesis.get("after_seq") or 0)
                    last = c.execute("SELECT body FROM audit_marks WHERE kind = 'checkpoint' ORDER BY n DESC LIMIT 1"
                                     ).fetchone()
                    if seq <= after or (last is not None and int(json.loads(last[0]).get("seq") or 0) >= seq):
                        body = None
                    else:
                        body = self._add_mark(c, "checkpoint", {"seq": seq, "hash": h, "count": seq - after,
                                                                "reason": reason})
            except sqlite3.Error as e:
                log.warning("audit checkpoint deferred: %s", e)
                return None
            self._since_cp, self._cp_at = 0, self.clock()
        if body is not None and self.on_checkpoint is not None:
            try:
                self.on_checkpoint(body)
            except Exception:
                log.exception("audit checkpoint hook failed")
        self._mark_written(body)
        return body

    def _mark_written(self, body: dict | None) -> None:
        if body is not None and self.on_mark is not None:
            try:
                self.on_mark(body)
            except Exception:
                log.exception("audit mark hook failed")

    def _maybe_checkpoint(self) -> None:
        if not self._since_cp:
            return
        if self._since_cp >= self.checkpoint_rows:
            self.checkpoint("rows")
        elif self.clock() - self._cp_at >= self.checkpoint_s:
            self.checkpoint("time")

    def rotate_audit_key(self, *, reset: bool = False) -> dict:
        """Make a new audit key active with a key mark signed by the old one (`reset`: signed by
        the new key only, for a node that lost its key; verify reports it). The old private key
        is deleted; its public half stays in the log."""
        with self._lock:
            if self._genesis is None:
                raise InvalidRequest("the audit chain has not started on this node yet")
            with self._tx() as c:
                old_kid, old_key = self._signing_key(c)
                if old_key is None and not reset:
                    raise InvalidRequest(f"the active audit key {old_kid} is not on this node; rotate with --reset")
                new_kid, new_key = self.keys.create()
                fields = {"next_kid": new_kid, "next_pub": pub_text(new_key), "reset": bool(reset)}
                body = self._add_mark(c, "key", fields, signer=(new_kid, new_key) if reset else None)
            try:
                self.keys.add_trust(str(self._genesis.get("chain")), new_kid, fields["next_pub"], self.clock())
            except OSError:
                log.exception("audit trust file not updated")
            if old_kid and old_kid != new_kid:
                self.keys.forget(old_kid)
            return {"kid": new_kid, "previous": old_kid, "mark": body["n"], "reset": bool(reset)}

    def marks(self) -> list[dict]:
        with self.db.lock:
            return auditlog.read_marks(self.db.conn)

    def chain_rows(self, *, from_seq: int = 0, to_seq: int | None = None) -> Iterator[dict]:
        return auditlog.iter_rows(self.db.conn, from_seq=from_seq, to_seq=to_seq, lock=self.db.lock)

    def audit_sign(self, ctx: bytes, data: bytes) -> tuple[str | None, str | None]:
        """Sign `data` with the active audit key (export manifests, share links): (kid, sig)."""
        with self._lock, self.db.lock:
            kid, key = self._signing_key(self.db.conn)
        return kid, (sign(key, ctx, data) if key is not None else None)

    def audit_keys(self) -> dict[str, str]:
        """kid -> public key (base64url) of every audit key the log's marks introduced."""
        out: dict[str, str] = {}
        for m in self.marks():
            try:
                body = json.loads(m["body"])
            except ValueError:
                continue
            if m["kind"] == "genesis" and body.get("kid") and body.get("pub"):
                out[body["kid"]] = body["pub"]
            elif m["kind"] == "key" and body.get("next_kid") and body.get("next_pub"):
                out[body["next_kid"]] = body["next_pub"]
        return out

    def verify(self, *, scope: Scope | None = None, from_seq: int | None = None, to_seq: int | None = None,
               trust_note: bool = True) -> Report:
        """Check the chain, the marks and their signatures (openajar.policy.auditlog.verify_db)."""
        self.flush()
        return auditlog.verify_db(self.db.conn, trusted=self.keys.trusted_kids(), scope=scope, lock=self.db.lock,
                                  from_seq=from_seq, to_seq=to_seq, trust_note=trust_note)
