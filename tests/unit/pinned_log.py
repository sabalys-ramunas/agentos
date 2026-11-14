"""A synthetic action log for the audit pins (test_pinned_verify, test_pinned_anchors): rows chained
with row_hash, marks signed with a fixed Ed25519 key, so a test can tamper with any part of it."""

from __future__ import annotations

import json
import sqlite3

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentos.policy.auditlog import (
    ROW_COLUMNS,
    SIG_CTX,
    canonical,
    lane_tag,
    mark_hash,
    pub_text,
    row_digest,
    row_hash,
    sign,
)
from agentos.policy.tokens import key_id

AT = "2026-11-14T00:00:00Z"

KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
KEY2 = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
KID, PUB, KID2, PUB2 = key_id(KEY), pub_text(KEY), key_id(KEY2), pub_text(KEY2)


def row(seq: int, lane: str = "biz-a/x") -> dict:
    r = {c: None for c in ROW_COLUMNS}
    r.update(seq=seq, id=f"tr_{seq:010d}", at=1000.0 + seq, kind="tool_end", tool="browser_act", lane=lane,
             workspace=lane.split("/")[0], status="ok", summary=f"step {seq}")
    return r


class Log:
    """A log as a node writes it: rows after `after` chained to the genesis, marks signed in order."""

    def __init__(self, n: int = 6, *, after: int = 0, unchained: int | None = None, lanes: tuple = ("biz-a/x",)):
        self.marks: list[dict] = []
        self.hashes: dict[int, str | None] = {}  # each seq's hash as written, kept after retention removes it
        self.key, self.kid = KEY, KID
        self.genesis = self.mark("genesis", pub=PUB, after_seq=after, unchained=after if unchained is None else unchained)
        self.rows = [row(i, lanes[i % len(lanes)]) for i in range(1, n + 1)]
        self.relink()

    def relink(self, start: int = 0) -> None:
        """Chain every row after the genesis's after_seq again, from row index `start`."""
        after = self.genesis["after_seq"]
        prev = self.rows[start - 1]["hash"] if start else self.marks[0]["hash"]
        for r in self.rows[start:]:
            if r["seq"] <= after:
                r["prev"], r["hash"] = None, None
                continue
            r["prev"], r["hash"] = prev, row_hash(prev, r)
            prev = r["hash"]
        self.hashes.update({r["seq"]: r["hash"] for r in self.rows})

    def body(self, kind: str, *, at: str = AT, **fields) -> dict:
        last = self.marks[-1] if self.marks else None
        return {"v": 1, "kind": kind, "chain": "ch_pinned", "n": len(self.marks) + 1,
                "prev": last["hash"] if last else None, "at": at, "kid": self.kid, **fields}

    def add(self, body: dict, *, key=None, signed: bool = True) -> dict:
        data = canonical(body)
        self.marks.append({"n": body["n"], "kind": body["kind"], "body": data.decode(), "hash": mark_hash(body),
                           "sig": sign(key or self.key, SIG_CTX, data) if signed else None})
        return body

    def mark(self, kind: str, *, key=None, signed: bool = True, at: str = AT, **fields) -> dict:
        return self.add(self.body(kind, at=at, **fields), key=key, signed=signed)

    def row_at(self, seq: int) -> dict:
        return next(r for r in self.rows if r["seq"] == seq)

    def checkpoint(self, seq: int | None = None, *, at: str = AT, **extra) -> dict:
        seq = seq if seq is not None else self.rows[-1]["seq"]
        after = self.genesis["after_seq"]
        return self.mark("checkpoint", at=at, seq=seq, hash=self.row_at(seq)["hash"], count=seq - after,
                         reason="manual", **extra)

    def rotate(self, key=KEY2, *, reset: bool = False, next_kid=None, next_pub=None, as_node: bool = False) -> dict:
        """A key mark; `as_node`: a reset as the node writes it, naming the new key that signs it."""
        if as_node:
            self.kid = key_id(key)
        body = self.body("key", next_kid=next_kid or key_id(key), next_pub=next_pub or pub_text(key), reset=reset)
        self.add(body, key=key if reset else None)
        self.key, self.kid = key, key_id(key)
        return body

    def prune(self, through: int, kept: list[int], *, signed: bool = True, kept_raw=None, at: str = AT) -> dict:
        kept_rows = kept_raw if kept_raw is not None else [[s, self.hashes[s]] for s in kept]
        body = self.mark("pruned", signed=signed, at=at, through_seq=through, through_hash=self.hashes[through],
                         deleted=through - len(kept), kept=kept_rows, cutoff="2026-11-01T00:00:00Z")
        self.rows = [r for r in self.rows if r["seq"] > through or r["seq"] in kept]
        return body

    def drop(self, *seqs: int) -> None:
        self.rows = [r for r in self.rows if r["seq"] not in seqs]

    def edit_mark(self, n: int, *, resign: bool = True, **changes) -> None:
        m = self.marks[n - 1]
        body = {**json.loads(m["body"]), **changes}
        data = canonical(body)
        m.update(body=data.decode(), kind=body.get("kind", m["kind"]))
        if resign:
            m.update(hash=mark_hash(body), sig=sign(self.key, SIG_CTX, data))

    def stub(self, seq: int) -> None:
        i = next(i for i, r in enumerate(self.rows) if r["seq"] == seq)
        r = self.rows[i]
        self.rows[i] = {"seq": seq, "id": r["id"], "stub": True, "prev": r["prev"], "hash": r["hash"],
                        "digest": row_digest(r), "lane_tag": lane_tag(r["lane"], r["id"])}

    def db(self, *, traces: bool = True) -> sqlite3.Connection:
        """The log as a node's traces.db holds it (the columns the anchor check reads)."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE audit_marks (n INTEGER, kind TEXT, body TEXT, hash TEXT, sig TEXT)")
        conn.executemany("INSERT INTO audit_marks VALUES (:n, :kind, :body, :hash, :sig)", self.marks)
        if traces:
            conn.execute("CREATE TABLE traces (seq INTEGER, hash TEXT)")
            conn.executemany("INSERT INTO traces VALUES (?, ?)", [(r["seq"], r["hash"]) for r in self.rows])
        return conn
