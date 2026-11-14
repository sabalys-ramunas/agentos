"""Runtime service "anchors": every signed checkpoint and retention head, sent somewhere the node
cannot rewrite (agentos.policy.anchors has the why and the check).

Targets come from `NodeConfig.audit_anchors`:

- "controller" (the default): the controller this node joined. Each head goes over the node
  channel as `audit_anchor`; the controller stores it per node and answers with a receipt it
  signs with its own key (`audit_anchor_receipt`). A node with no controller has nothing to
  send to, so anchoring is off there until the owner adds a target.
- "s3://bucket/prefix": one small JSON object per head, written with S3 Object Lock
  (COMPLIANCE, `audit_anchor_retain_days`), using the backup credentials (agentos.ops.s3).

Heads wait in home/anchors.db until a target took them, so a controller that is away, an old
controller that does not know the message, or an S3 outage only delays anchoring. Nothing here
can stop the node: every failure is logged, shown in `status()`, and retried.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.policy import anchors as anchor_rules
from agentos.policy.auditlog import read_marks
from agentos.policy.store import DB

log = logging.getLogger("agentos.anchors")

SERVICE_NAME = "anchors"
SCHEMA = """
CREATE TABLE IF NOT EXISTS anchors (
    chain TEXT NOT NULL,
    n INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    hash TEXT NOT NULL,
    kid TEXT,
    at TEXT,
    receipt TEXT,          -- the controller's signed receipt (JSON), once it has the head
    s3_at REAL,            -- when the S3 target took it
    PRIMARY KEY (chain, n)
);
"""
# added later, so an anchors.db from before them is migrated in place (agentos.policy.store.DB)
COLUMNS = {"kind": "TEXT",          # NULL for a checkpoint, "pruned" for a retention mark
           "queued_at": "REAL"}     # when this node queued it (status: how long heads wait)
BATCH = 50                  # heads per audit_anchor message
ROUND_S = 60.0              # a round every this often, and right after each checkpoint
REPLY_WAIT_S = 15.0
UNSUPPORTED_BACKOFF_S = 3600.0  # an older controller that does not know audit_anchor
FETCH_LIMIT = 2000          # newest heads an older controller sends back (one frame stays under 1 MiB)
PAGE_LIMIT = 1000           # heads per audit_anchors_get page (well under 1 MiB a frame)
MAX_PAGES = 10_000          # a controller whose cursor never ends is cut off here
S3_PREFIX = "anchors"


def s3_name(chain: str, n: int) -> str:
    return f"{S3_PREFIX}/{chain}/{n:012d}.json"


class AnchorStore:
    def __init__(self, path: Path, *, clock: Any = time.time):
        self.db = DB(path, SCHEMA)
        self.clock = clock
        have = {r["name"] for r in self.db.all("PRAGMA table_info(anchors)")}
        for col, typ in COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE anchors ADD COLUMN {col} {typ}")

    def add(self, a: dict) -> bool:
        kind = anchor_rules.kind_of(a)
        cur = self.db.execute("INSERT OR IGNORE INTO anchors (chain, n, seq, hash, kid, at, kind, queued_at) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (a["chain"], a["n"], a["seq"], a["hash"], a.get("kid"), a.get("at"),
                               None if kind == "checkpoint" else kind, self.clock()))
        return cur.rowcount == 1

    def known(self) -> set[tuple[str, int]]:
        return {(r["chain"], int(r["n"])) for r in self.db.all("SELECT chain, n FROM anchors")}

    def top(self, chain: str) -> int:
        r = self.db.one("SELECT MAX(n) AS n FROM anchors WHERE chain = ?", (chain,))
        return int(r["n"]) if r and r["n"] is not None else 0

    @staticmethod
    def _anchor(r: Any) -> dict:
        a = {"v": anchor_rules.ANCHOR_V, "chain": r["chain"], "n": r["n"], "seq": r["seq"], "hash": r["hash"],
             "kid": r["kid"], "at": r["at"]}
        if r["kind"]:
            a["kind"] = r["kind"]
        return a

    def pending(self, target: str, limit: int = BATCH) -> list[dict]:
        col = "receipt" if target == "controller" else "s3_at"
        rows = self.db.all(f"SELECT * FROM anchors WHERE {col} IS NULL ORDER BY chain, n LIMIT ?", (limit,))
        return [self._anchor(r) for r in rows]

    def get(self, chain: str, n: int) -> dict | None:
        r = self.db.one("SELECT * FROM anchors WHERE chain = ? AND n = ?", (chain, n))
        return self._anchor(r) if r else None

    def set_receipt(self, chain: str, n: int, receipt: dict) -> None:
        self.db.execute("UPDATE anchors SET receipt = ? WHERE chain = ? AND n = ?", (json.dumps(receipt), chain, n))

    def set_s3(self, chain: str, n: int, at: float) -> None:
        self.db.execute("UPDATE anchors SET s3_at = ? WHERE chain = ? AND n = ?", (at, chain, n))

    def counts(self) -> dict:
        r = self.db.one("SELECT COUNT(*) AS total, SUM(receipt IS NOT NULL) AS controller, "
                        "SUM(s3_at IS NOT NULL) AS s3 FROM anchors")
        return {"total": int(r["total"] or 0), "controller": int(r["controller"] or 0), "s3": int(r["s3"] or 0)}

    def target_status(self, target: str) -> dict:
        """For one target: the newest checkpoint and retention mark it took, and how many wait."""
        col = "receipt" if target == "controller" else "s3_at"
        cp = self.db.one(f"SELECT n, seq, at FROM anchors WHERE {col} IS NOT NULL AND kind IS NULL "
                         "ORDER BY seq DESC, n DESC LIMIT 1")
        pr = self.db.one(f"SELECT n, seq FROM anchors WHERE {col} IS NOT NULL AND kind = 'pruned' "
                         "ORDER BY n DESC LIMIT 1")
        w = self.db.one(f"SELECT COUNT(*) AS c, MIN(queued_at) AS oldest FROM anchors WHERE {col} IS NULL")
        return {"last_seq": int(cp["seq"]) if cp else None, "last_mark": int(cp["n"]) if cp else None,
                "last_at": cp["at"] if cp else None, "last_retention_mark": int(pr["n"]) if pr else None,
                "pending": int(w["c"] or 0), "oldest_queued_at": w["oldest"]}

    def close(self) -> None:
        self.db.close()


class AnchorService:
    def __init__(self, config: Any, runtime: Any, *, clock: Any = time.time, s3_factory: Any = None):
        self.config = config
        self.runtime = runtime
        self.clock = clock
        targets = [str(t).strip() for t in (getattr(config, "audit_anchors", None) or []) if str(t).strip()]
        self.to_controller = "controller" in targets and bool(getattr(config, "enrolled", False))
        self.s3_url = next((t for t in targets if t.startswith("s3://")), None)
        self.retain_days = int(getattr(config, "audit_anchor_retain_days", 400) or 400)
        self._store: AnchorStore | None = None
        self._s3_factory = s3_factory
        self._waiting: dict[str, asyncio.Future] = {}
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._backoff_until = 0.0
        self.last_error: str | None = None
        self.last_receipt_at: float | None = None
        self.conflicts = 0
        self.old_controller = False  # its receipts drop a retention head's kind: it predates them

    @property
    def store(self) -> AnchorStore:
        """home/anchors.db, made the first time a head is queued (a node that anchors nowhere has none)."""
        if self._store is None:
            self._store = AnchorStore(Path(self.config.home) / "anchors.db", clock=self.clock)
        return self._store

    @property
    def active(self) -> bool:
        return self.to_controller or self.s3_url is not None

    def targets(self) -> list[str]:
        return [*(["controller"] if self.to_controller else []), *([self.s3_url] if self.s3_url else [])]

    def status(self) -> dict:
        """What the panel and `agentos health status` show: per target, the newest checkpoint seq
        anchored there, how many heads wait, and the last error."""
        if not self.active:
            return {"on": False, "targets": []}
        per = {t: self.store.target_status(t) for t in (["controller"] if self.to_controller else [])
               + (["s3"] if self.s3_url else [])}
        queued = [p["oldest_queued_at"] for p in per.values() if p["pending"] and p["oldest_queued_at"] is not None]
        return {"on": self.active, "targets": self.targets(), **self.store.counts(), "per_target": per,
                "pending": sum(p["pending"] for p in per.values()),
                "pending_controller": per["controller"]["pending"] if "controller" in per else 0,
                "oldest_pending_s": max(0.0, self.clock() - min(queued)) if queued else None,
                "last_receipt_at": self.last_receipt_at, "conflicts": self.conflicts,
                "old_controller": self.old_controller, "last_error": self.last_error}

    # ---- collecting heads ----
    def _traces(self) -> Any:
        policy = self.runtime.get("policy") if self.runtime is not None else None
        return getattr(policy, "traces", None)

    def collect(self) -> int:
        """Queue every checkpoint and retention mark of the log not queued yet: the hook is only a
        wake-up, so a mark written while this was off or busy (or before this node anchored
        retention marks at all) is still anchored."""
        traces = self._traces()
        if traces is None:
            return 0
        with traces.db.lock:
            marks = read_marks(traces.db.conn)
        added = 0
        known = self.store.known()
        for m in marks:
            if m["kind"] not in anchor_rules.KINDS:
                continue
            try:
                body = json.loads(m["body"])
                a = anchor_rules.anchor_of(body)
            except (ValueError, KeyError, TypeError):
                continue
            if anchor_rules.valid(a) is not None or (a["chain"], a["n"]) in known:
                continue
            if self.store.add(a):
                added += 1
        return added

    def _on_checkpoint(self, _body: dict) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._wake.set)

    # ---- lifecycle ----
    async def start(self) -> None:
        if not self.active:
            log.info("audit anchors are off (no target: set audit_anchors in node.toml)")
            return
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        traces = self._traces()
        if traces is not None:
            traces.on_mark = self._on_checkpoint
        self._task = asyncio.create_task(self._run(), name="audit-anchors")
        log.info("audit anchors go to %s", ", ".join(self.targets()))

    async def stop(self) -> None:
        traces = self._traces()
        if traces is not None and getattr(traces, "on_mark", None) == self._on_checkpoint:
            traces.on_mark = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self._store is not None:
            self._store.close()
            self._store = None

    async def _run(self) -> None:
        while True:
            await self.round()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(ROUND_S):
                    await self._wake.wait()

    async def round(self) -> None:
        """Queue new heads and hand them to every target. Never raises."""
        try:
            await asyncio.to_thread(self.collect)
            if self.to_controller:
                await self.send_controller()
            if self.s3_url:
                await asyncio.to_thread(self.send_s3)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            log.exception("audit anchor round failed")

    # ---- the controller ----
    def _channel(self) -> Any:
        channel = self.runtime.get("channel") if self.runtime is not None else None
        if channel is None or not getattr(channel, "connected", False) or getattr(channel, "conn", None) is None:
            return None
        return channel

    async def _ask(self, kind: str, body: dict) -> Any:
        from agentos.node.channel import wire

        channel = self._channel()
        if channel is None:
            return None
        env = wire.Envelope(kind, body)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiting[env.id] = future
        try:
            await channel.conn.send(env)
            return await asyncio.wait_for(future, REPLY_WAIT_S)
        except TimeoutError:
            self.last_error = f"the controller did not answer {kind} in time"
            return None
        finally:
            self._waiting.pop(env.id, None)

    async def send_controller(self) -> int:
        if self.clock() < self._backoff_until:
            return 0
        sent = 0
        while True:
            batch = self.store.pending("controller")
            if not batch:
                return sent
            reply = await self._ask("audit_anchor", {"anchors": batch})
            if reply is None or reply.type == "error":
                self._refused(reply)
                return sent
            stored = self._take_receipts(reply.body.get("receipts"))
            sent += stored
            if not stored:
                return sent  # nothing it answered matched what was asked: try again next round

    def _refused(self, reply: Any) -> None:
        if reply is None:
            return
        code = str(reply.body.get("code") or "")[:40]
        if code == "unsupported":
            self._backoff_until = self.clock() + UNSUPPORTED_BACKOFF_S
            self.last_error = "the controller is older than audit anchors; upgrade it to anchor this node's log there"
            log.warning("audit anchors: %s", self.last_error)
        else:
            self.last_error = f"the controller refused audit anchors: {code}: {str(reply.body.get('message'))[:200]}"

    def receipt_ok(self, receipt: Any) -> bool:
        from agentos.node.channel.identity import verify_controller

        return isinstance(receipt, dict) and isinstance(receipt.get("anchor"), dict) and verify_controller(
            getattr(self.config, "controller_pubkey", ""), anchor_rules.RECEIPT_KIND,
            getattr(self.config, "node_id", ""), receipt)

    def _take_receipts(self, receipts: Any) -> int:
        n = 0
        for r in receipts if isinstance(receipts, list) else []:
            if not self.receipt_ok(r):
                self.last_error = "a receipt from the controller does not carry its signature; it was not kept"
                continue
            a = r["anchor"]
            mine = self.store.get(str(a.get("chain")), int(a.get("n") or 0))
            if mine is None:
                continue
            if "kind" in mine and "kind" not in a and anchor_rules.core({**mine, "kind": None}) == anchor_rules.core(a):
                # a controller older than retention anchors kept the head and its time but not the kind:
                # the receipt still dates the mark, so it is kept; verify says what it cannot show
                self.old_controller = True
                self.last_error = ("the controller is older than retention anchors: upgrade it so it also records "
                                   "which marks are retention marks")
            elif anchor_rules.core(mine) != anchor_rules.core(a):
                # the controller already held another head for this checkpoint: the log changed
                # after it was first anchored. Keep the receipt (it is the proof) and say so loudly.
                self.conflicts += 1
                self.last_error = (f"the controller holds another head for checkpoint {mine['n']} of chain "
                                   f"{mine['chain']}: this log changed after it was anchored")
                log.error("audit anchors: %s", self.last_error)
            elif "kind" in mine and self.old_controller:  # it keeps the kind now: it was upgraded
                self.old_controller = False
                if self.last_error and "older than retention anchors" in self.last_error:
                    self.last_error = None
            self.store.set_receipt(mine["chain"], mine["n"], r)
            self.last_receipt_at = self.clock()
            n += 1
        return n

    def on_reply(self, message: Any) -> bool:
        """The channel hands over audit_anchor_receipt, audit_anchors_result and errors that answer
        one of ours. True when it was ours."""
        future = self._waiting.get(message.reply_to or "")
        if future is None or future.done():
            return False
        future.set_result(message)
        return True

    async def fetch_controller(self) -> dict:
        """Every head the controller holds for this node, page by page, each with the controller's
        signed receipt; heads whose receipt does not verify are left out and counted. A controller
        from before paging answers once, with no `next`: that one reply is all it has to give.
        `truncated` says the walk was cut off (a cursor that repeats, or MAX_PAGES)."""
        if not self.to_controller:
            return {"ok": False, "error": "this node does not anchor to a controller"}
        good, bad, after, pages = [], 0, "", 0
        truncated = False
        while True:
            reply = await self._ask("audit_anchors_get", {"limit": FETCH_LIMIT, "page": PAGE_LIMIT, "after": after})
            if reply is None:
                return {"ok": False, "error": self.last_error or "the controller is not connected"}
            if reply.type == "error":
                return {"ok": False, "error": f"{reply.body.get('code')}: {str(reply.body.get('message'))[:200]}"}
            receipts = reply.body.get("receipts") or []
            for r in receipts if isinstance(receipts, list) else []:
                if self.receipt_ok(r):
                    good.append({**r["anchor"], "source": "controller", "received_at": r.get("received_at")})
                else:
                    bad += 1
            pages += 1
            nxt = reply.body.get("next")  # missing (an older controller) or null: that was the last page
            if not nxt or not isinstance(nxt, str):
                break
            if nxt == after or pages >= MAX_PAGES:
                truncated = True
                break
            after = nxt
        return {"ok": True, "anchors": good, "unsigned": bad, "pages": pages, "truncated": truncated}

    # ---- S3 with object lock ----
    def _s3(self) -> Any:
        if self._s3_factory is not None:
            return self._s3_factory(self.s3_url)
        from agentos.ops.s3 import S3Target

        return S3Target(self.s3_url, home=Path(self.config.home))

    def send_s3(self) -> int:
        from agentos.ops.s3 import S3Error

        try:
            target = self._s3()
        except S3Error as e:
            self.last_error = str(e)
            return 0
        sent = 0
        for a in self.store.pending("s3", 500):
            body = json.dumps(a, sort_keys=True, separators=(",", ":")).encode()
            until = datetime.fromtimestamp(self.clock() + self.retain_days * 86400, UTC)
            headers = {"content-type": "application/json",
                       "content-md5": base64.b64encode(hashlib.md5(body).digest()).decode(),
                       "x-amz-object-lock-mode": "COMPLIANCE",
                       "x-amz-object-lock-retain-until-date": until.strftime("%Y-%m-%dT%H:%M:%SZ")}
            try:
                status, _, resp = target._request("PUT", target.key(s3_name(a["chain"], a["n"])), body=body,
                                                  headers=headers)
            except S3Error as e:
                self.last_error = str(e)
                return sent
            if status != 200:
                self.last_error = str(target._error(status, resp, f"anchor upload to {self.s3_url}"))
                return sent
            self.store.set_s3(a["chain"], a["n"], self.clock())
            sent += 1
        return sent


def fetch_s3(url: str, *, home: Path | None = None, target: Any = None) -> list[dict]:
    """Every head an S3 anchor target holds (for `agentos audit verify --anchors`)."""
    import tempfile

    from agentos.ops.s3 import S3Target

    t = target or S3Target(url, home=home)
    out: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        for obj in t.list():
            if not obj.key.startswith(S3_PREFIX + "/") or not obj.key.endswith(".json"):
                continue
            dest = Path(tmp) / "a.json"
            t.get(obj.key, dest)
            try:
                a = json.loads(dest.read_text())
            except ValueError:
                continue
            if isinstance(a, dict):
                # the bucket's own time for the object: the node cannot set it, so it dates the mark
                out.append({**a, "source": url, "received_at": obj.last_modified or None})
    return out


def build(runtime: Any) -> AnchorService:
    service = AnchorService(runtime.require("config"), runtime)
    runtime.register(SERVICE_NAME, service)
    return service
