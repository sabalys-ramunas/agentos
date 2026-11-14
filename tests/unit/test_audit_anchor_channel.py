"""Audit anchors over the node channel and to S3: the controller keeps each checkpoint head once and
signs a receipt for it, the node keeps only receipts whose signature verifies, and S3 gets one
object-locked object per head. The Hub and AnchorService are the real ones, joined by a fake
channel that passes each message through the hub as a wire frame.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_policy_support import T0, Clock

from agentos.controller.grants import Keys
from agentos.controller.hub import Hub, NodeSession
from agentos.controller.store import ControllerStore
from agentos.core.s3 import S3Object
from agentos.core.signing import verify_controller
from agentos.node.anchors import AnchorService, AnchorStore, fetch_s3
from agentos.node.channel import wire
from agentos.policy import anchors as anchor_rules
from agentos.policy.auditlog import read_marks
from agentos.policy.traces import TraceEvent, TraceStore

ORG = "org_chan"
NODE = "node_01CHANNEL00000000000000A"
OTHER = "node_01CHANNEL00000000000000B"
CHAIN = "ch_chan"
RECEIPT = "audit_anchor_receipt"
HASH = "sha256:" + "a" * 64
OTHER_HASH = "sha256:" + "b" * 64


def anchor(n: int, h: str = HASH) -> dict:
    return {"v": 1, "chain": CHAIN, "n": n, "seq": n * 10, "hash": h, "kid": "k_chan",
            "at": f"2027-01-15T00:00:{n:02d}Z"}


class FakeWS:
    """The controller's side of one node socket: what it writes, decoded the way the node reads it."""

    def __init__(self):
        self.sent: list[wire.Envelope] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(wire.decode(text))


def node_session(node_id: str = NODE) -> NodeSession:
    return NodeSession(FakeWS(), {"id": node_id, "org_id": ORG, "name": "box"})


async def ask(hub: Hub, session: NodeSession, env: wire.Envelope) -> wire.Envelope:
    """A node message goes into the hub as a wire frame; returns the reply the hub wrote to that node."""
    before = len(session.ws.sent)
    await hub.handle(session, wire.decode(env.encode()))
    (reply,) = [e for e in session.ws.sent[before:] if e.re == env.id]
    return reply


class Link:
    """The node's channel: each message goes to `answer`, and the reply is handed back to the service."""

    def __init__(self, answer):
        self.connected, self.conn, self.service = True, self, None
        self.answer, self.sent = answer, []

    def envelope(self, kind: str, body: dict) -> wire.Envelope:  # as NodeChannel.envelope
        return wire.Envelope(kind, body)

    async def send(self, env: wire.Envelope) -> None:
        self.sent.append(env)
        assert self.service.on_reply(await self.answer(env)), "a reply matched no request"


class FakeBucket:
    """Stands in for S3Target: records each PUT with its headers, and serves back what was stored."""

    def __init__(self):
        self.puts: list[tuple[str, bytes, dict]] = []
        self.objects: dict[str, bytes] = {}

    def key(self, name: str) -> str:
        return name

    def _request(self, method: str, key: str = "", *, body: bytes = b"", headers: dict | None = None):
        assert method == "PUT"
        self.puts.append((key, body, dict(headers or {})))
        self.objects[key] = body
        return 200, {}, b""

    def list(self) -> list[S3Object]:
        return [S3Object(k, len(v), "") for k, v in sorted(self.objects.items())]

    def get(self, name: str, dest: Path) -> None:
        Path(dest).write_bytes(self.objects[name])


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def keys(tmp_path):
    return Keys(tmp_path / "controller-secrets")


@pytest.fixture
def hub(tmp_path, keys, clock):
    store = ControllerStore(tmp_path / "controller.db")
    yield Hub(store, keys, clock=clock)
    store.close()


@pytest.fixture
def traces(tmp_path, clock):
    """The node's real trace log: three checkpoints of five rows each."""
    store = TraceStore(tmp_path / "traces.db", clock=clock, checkpoint_rows=10_000)
    for i in range(15):
        store.write(TraceEvent(kind="tool_end", tool="browser_act", lane="biz-a/instagram", status="ok",
                               summary=f"step {i}", args={"i": i}))
        clock.advance(1)
        if i % 5 == 4:
            assert store.checkpoint() is not None
    yield store
    store.close()


@pytest.fixture
def make_service(keys, clock):
    made: list[AnchorService] = []

    def make(home: Path, *, traces=None, channel=None, s3_factory=None, **config) -> AnchorService:
        cfg = SimpleNamespace(**{"home": home, "audit_anchors": ["controller"], "enrolled": True,
                                 "controller_pubkey": keys.controller_pubkey, "node_id": NODE,
                                 "audit_anchor_retain_days": 400, **config})
        parts = {"policy": SimpleNamespace(traces=traces), "channel": channel}
        svc = AnchorService(cfg, SimpleNamespace(get=parts.get), clock=clock, s3_factory=s3_factory)
        made.append(svc)
        if channel is not None:
            channel.service = svc
        return svc

    yield make
    for svc in made:
        svc.store.close()


# ---- the controller: Hub.handle on a node session ----

async def test_a_batch_of_heads_gets_one_receipt_each_that_the_controller_signed(hub, keys):
    session = node_session()
    sent = [anchor(1), anchor(2)]
    reply = await ask(hub, session, wire.Envelope("audit_anchor", {"anchors": sent}))
    assert reply.type == RECEIPT
    assert [r["anchor"]["n"] for r in reply.body["receipts"]] == [1, 2]
    assert reply.body["conflicts"] == [] and reply.body["refused"] == 0
    for receipt, head in zip(reply.body["receipts"], sent):
        assert receipt["anchor"] == anchor_rules.core(head)
        assert verify_controller(keys.controller_pubkey, RECEIPT, NODE, receipt)
    assert hub.store.anchors(ORG, NODE) == reply.body["receipts"]


async def test_the_same_head_sent_again_gets_the_same_receipt_and_no_conflict(hub, clock):
    session = node_session()
    first = await ask(hub, session, wire.Envelope("audit_anchor", {"anchors": [anchor(1)]}))
    clock.advance(60)  # a new receipt would carry this later time, so the same one proves it was kept
    again = await ask(hub, session, wire.Envelope("audit_anchor", {"anchors": [anchor(1)]}))
    assert again.body["receipts"] == first.body["receipts"]
    assert again.body["conflicts"] == []
    assert len(hub.store.anchors(ORG, NODE)) == 1 and hub.store.anchor_conflicts(ORG, NODE) == []


async def test_another_head_for_a_checkpoint_gets_the_original_receipt_and_a_conflict(hub, clock):
    session = node_session()
    original = (await ask(hub, session, wire.Envelope("audit_anchor", {"anchors": [anchor(4)]}))).body
    clock.advance(60)
    rewritten = await ask(hub, session, wire.Envelope("audit_anchor", {"anchors": [anchor(4, OTHER_HASH)]}))
    assert rewritten.body["receipts"] == original["receipts"]  # the stored receipt, not one for the new head
    assert rewritten.body["conflicts"] == [4]
    assert hub.store.anchors(ORG, NODE) == original["receipts"]
    (row,) = hub.store.anchor_conflicts(ORG, NODE)
    assert row["n"] == 4 and row["hash"] == OTHER_HASH


async def test_malformed_heads_are_refused_and_stored_nowhere(hub):
    bad = [{**anchor(1), "hash": "a" * 64},  # no sha256: prefix
           {**anchor(2), "n": 0}, "not an anchor"]
    body = (await ask(hub, node_session(), wire.Envelope("audit_anchor", {"anchors": bad}))).body
    assert body["refused"] == 3 and body["receipts"] == [] and body["conflicts"] == []
    assert hub.store.anchors(ORG, NODE) == []


async def test_a_node_gets_back_its_own_receipts_and_no_other_nodes(hub, keys):
    mine, theirs = node_session(NODE), node_session(OTHER)
    await ask(hub, mine, wire.Envelope("audit_anchor", {"anchors": [anchor(1), anchor(2)]}))
    await ask(hub, theirs, wire.Envelope("audit_anchor", {"anchors": [anchor(1, OTHER_HASH)]}))
    reply = await ask(hub, mine, wire.Envelope("audit_anchors_get", {"limit": 5000}))
    assert reply.type == "audit_anchors_result"
    assert [(r["anchor"]["n"], r["anchor"]["hash"]) for r in reply.body["receipts"]] == [(1, HASH), (2, HASH)]
    assert reply.body["receipts"] == hub.store.anchors(ORG, NODE)
    assert all(verify_controller(keys.controller_pubkey, RECEIPT, NODE, r) for r in reply.body["receipts"])


async def test_a_store_failure_is_answered_unavailable_and_the_hub_goes_on(hub, monkeypatch):
    monkeypatch.setattr(hub.store, "put_anchor", Mock(side_effect=RuntimeError("disk on fire")))
    reply = await ask(hub, node_session(), wire.Envelope("audit_anchor", {"anchors": [anchor(1)]}))
    assert reply.type == "error" and reply.body["code"] == "unavailable"
    assert "disk on fire" not in reply.body["message"]


# ---- the node: AnchorService through a channel to the hub ----

async def test_collect_queues_each_checkpoint_once_and_the_controller_holds_each_with_a_receipt(
        hub, traces, tmp_path, make_service):
    session = node_session()
    link = Link(lambda env: ask(hub, session, env))
    svc = make_service(tmp_path, traces=traces, channel=link)
    assert len([m for m in read_marks(traces.db.conn) if m["kind"] == "checkpoint"]) == 3
    assert svc.collect() == 3 and svc.collect() == 0
    assert len(svc.store.pending("controller")) == 3
    assert await svc.send_controller() == 3
    assert svc.store.pending("controller") == []
    assert len(hub.store.anchors(ORG, NODE)) == 3
    got = await svc.fetch_controller()
    assert got["ok"] and got["unsigned"] == 0 and len(got["anchors"]) == 3
    assert {a["source"] for a in got["anchors"]} == {"controller"}
    traces.flush()
    findings, summary = anchor_rules.check(traces.db.conn, got["anchors"], lock=traces.db.lock)
    assert findings == [] and summary["matched"] == 3


async def test_a_receipt_whose_signature_does_not_verify_is_not_kept(hub, traces, tmp_path, make_service):
    session = node_session()

    async def forged(env: wire.Envelope) -> wire.Envelope:
        reply = await ask(hub, session, env)
        for receipt in reply.body["receipts"]:
            receipt["sig"] = ("A" if receipt["sig"][0] != "A" else "B") + receipt["sig"][1:]
        return reply

    svc = make_service(tmp_path, traces=traces, channel=Link(forged))
    svc.collect()
    assert await svc.send_controller() == 0
    assert len(svc.store.pending("controller")) == 3
    assert "signature" in svc.last_error


async def test_an_unsupported_reply_backs_off_for_an_hour(traces, tmp_path, make_service, clock):
    async def old_controller(env: wire.Envelope) -> wire.Envelope:
        return wire.error_envelope("unsupported", "unknown message type 'audit_anchor'", re=env.id)

    link = Link(old_controller)
    svc = make_service(tmp_path, traces=traces, channel=link)
    svc.collect()
    assert await svc.send_controller() == 0 and len(link.sent) == 1
    assert await svc.send_controller() == 0 and len(link.sent) == 1  # nothing is sent inside the backoff
    assert "older than audit anchors" in svc.last_error
    clock.advance(3601)
    await svc.send_controller()
    assert len(link.sent) == 2


@pytest.mark.parametrize("config", [{"audit_anchors": []}, {"enrolled": False}])
def test_with_no_target_anchoring_is_off(tmp_path, make_service, config):
    off = make_service(tmp_path, **config)
    assert off.active is False and off.targets() == []
    (tmp_path / "on").mkdir()
    assert make_service(tmp_path / "on").active is True  # the same node with a target is on


# ---- S3 with object lock ----

def test_each_head_goes_to_s3_as_a_locked_object_and_reads_back(tmp_path, clock, make_service):
    bucket = FakeBucket()
    svc = make_service(tmp_path, audit_anchors=["s3://bkt/agentos"], s3_factory=lambda url: bucket)
    heads = [anchor(1), anchor(2)]
    for head in heads:
        svc.store.add(head)
    assert svc.send_s3() == 2
    assert svc.store.pending("s3") == []
    assert [key for key, _, _ in bucket.puts] == [f"anchors/{CHAIN}/{n:012d}.json" for n in (1, 2)]
    until = T0 + 400 * 86400
    for (_, body, headers), head in zip(bucket.puts, heads):
        assert headers["x-amz-object-lock-mode"] == "COMPLIANCE"
        retain = datetime.strptime(headers["x-amz-object-lock-retain-until-date"], "%Y-%m-%dT%H:%M:%SZ")
        assert abs(retain.replace(tzinfo=UTC).timestamp() - until) < 60
        assert headers["content-md5"] == base64.b64encode(hashlib.md5(body).digest()).decode()
        assert anchor_rules.core(json.loads(body)) == anchor_rules.core(head)
    bucket.objects["other/readme.json"] = b"{}"  # outside the anchors prefix: not an anchor
    fetched = fetch_s3("s3://bkt/agentos", target=bucket)
    assert [anchor_rules.core(a) for a in fetched] == [anchor_rules.core(h) for h in heads]
    assert {a["source"] for a in fetched} == {"s3://bkt/agentos"}


# ---- paging, retention heads, databases from before them, and status ----

# The tables as the versions before retention anchors created them (cc813b6b). The migration tests open
# files built from these, the same files an installed node or controller already has.
OLD_NODE_ANCHORS = """
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
OLD_CONTROLLER_ANCHORS = """
CREATE TABLE IF NOT EXISTS audit_anchors (
  org_id TEXT NOT NULL, node_id TEXT NOT NULL, chain TEXT NOT NULL, n INTEGER NOT NULL, seq INTEGER NOT NULL,
  hash TEXT NOT NULL, kid TEXT, at TEXT, received_at TEXT NOT NULL, sig TEXT NOT NULL,
  PRIMARY KEY (org_id, node_id, chain, n)
);
CREATE TABLE IF NOT EXISTS audit_anchor_conflicts (
  org_id TEXT NOT NULL, node_id TEXT NOT NULL, chain TEXT NOT NULL, n INTEGER NOT NULL, seq INTEGER NOT NULL,
  hash TEXT NOT NULL, kid TEXT, at TEXT, received_at TEXT NOT NULL
);
"""
RECEIVED = "2027-01-15T00:01:00Z"


def mark_head(n: int, chain: str = CHAIN, *, kid: str = "k_chan") -> dict:
    """A checkpoint head for mark n (seq n * 10). The time is fixed, so long runs stay valid anchors."""
    return {"v": 1, "chain": chain, "n": n, "seq": n * 10, "hash": HASH, "kid": kid, "at": "2027-01-15T00:00:00Z"}


def take_all(hub: Hub, session: NodeSession, heads: list[dict], clock: Clock) -> None:
    """Hand heads to the controller the way a node does: batches of 50, one second apart."""
    for i in range(0, len(heads), 50):
        reply = hub.take_anchors(session, heads[i:i + 50])
        assert reply["conflicts"] == [] and reply["refused"] == 0
        clock.advance(1)


def signed(keys: Keys, anchor_body: dict, received_at: str = RECEIVED) -> dict:
    """A receipt the controller signed over `anchor_body` as given, the way take_anchors signs one."""
    return keys.sign(RECEIPT, NODE, {"anchor": anchor_body, "received_at": received_at})


def without_kind(a: dict) -> dict:
    return {k: v for k, v in a.items() if k != "kind"}


# ---- the controller pages the heads it holds for a node ----

async def test_paged_requests_hand_back_every_head_once_in_chain_and_mark_order(hub, keys, clock):
    session = node_session()
    take_all(hub, session, [mark_head(n) for n in range(1, 2501)], clock)
    pages, cursor = [], ""
    for _ in range(5):  # three pages are expected; the bound only stops a cursor that never ends
        reply = await ask(hub, session, wire.Envelope("audit_anchors_get", {"after": cursor, "page": 1000}))
        assert reply.type == "audit_anchors_result" and set(reply.body) == {"receipts", "next"}
        pages.append(reply.body["receipts"])
        if reply.body["next"] is None:
            break
        cursor = reply.body["next"]
    assert [len(page) for page in pages] == [1000, 1000, 500]
    got = [r for page in pages for r in page]
    assert [(r["anchor"]["chain"], r["anchor"]["n"]) for r in got] == [(CHAIN, n) for n in range(1, 2501)]
    assert all(verify_controller(keys.controller_pubkey, RECEIPT, NODE, r) for r in got)


async def test_pages_follow_the_cursor_across_a_chain_boundary_and_end_on_a_full_last_page(hub, clock):
    session = node_session()
    other = "ch:b"  # a colon in a chain id: the cursor is split at its first colon only
    take_all(hub, session, [mark_head(n, c) for c in (CHAIN, other) for n in range(1, 8)], clock)
    first = await ask(hub, session, wire.Envelope("audit_anchors_get", {"after": "", "page": 7}))
    assert [(r["anchor"]["chain"], r["anchor"]["n"]) for r in first.body["receipts"]] == [(other, n) for n in range(1, 8)]
    assert first.body["next"] is not None
    second = await ask(hub, session, wire.Envelope("audit_anchors_get", {"after": first.body["next"], "page": 7}))
    assert [(r["anchor"]["chain"], r["anchor"]["n"]) for r in second.body["receipts"]] == [(CHAIN, n) for n in range(1, 8)]
    assert second.body["next"] is None  # a full last page is the last one: no empty page follows it


async def test_a_request_without_a_cursor_gets_the_newest_heads_and_no_next(hub, clock):
    session = node_session()
    take_all(hub, session, [mark_head(n) for n in range(1, 2101)], clock)
    reply = await ask(hub, session, wire.Envelope("audit_anchors_get", {"limit": 5}))
    assert "next" not in reply.body
    assert [r["anchor"]["n"] for r in reply.body["receipts"]] == [2096, 2097, 2098, 2099, 2100]
    capped = await ask(hub, session, wire.Envelope("audit_anchors_get", {"limit": 5000}))
    assert "next" not in capped.body and len(capped.body["receipts"]) == 2000  # the limit tops out at 2000
    assert capped.body["receipts"][0]["anchor"]["n"] == 101


async def test_a_full_page_of_the_longest_heads_encodes_under_a_mebibyte(hub, clock):
    session = node_session()
    take_all(hub, session, [mark_head(n, chain="c" * 128, kid="k" * 80) for n in range(1, 1001)], clock)
    reply = await ask(hub, session, wire.Envelope("audit_anchors_get", {"after": "", "page": 1000}))
    assert len(reply.body["receipts"]) == 1000 and reply.body["next"] is None
    assert len(json.dumps(reply.body).encode()) < 1024 * 1024


async def test_a_page_size_left_out_or_not_taken_gets_the_default_page_of_1000(hub, clock):
    session = node_session()
    take_all(hub, session, [mark_head(n) for n in range(1, 1002)], clock)
    for page in (None, 0, -5, 5000, "7", True):
        body = {"after": ""} if page is None else {"after": "", "page": page}
        reply = await ask(hub, session, wire.Envelope("audit_anchors_get", body))
        assert len(reply.body["receipts"]) == 1000 and reply.body["next"] is not None, page


# ---- the node asks page after page ----

async def test_fetch_controller_walks_every_page_and_keeps_each_head_once(hub, keys, clock, tmp_path, make_service):
    session = node_session()
    take_all(hub, session, [mark_head(n) for n in range(1, 2501)], clock)
    link = Link(lambda env: ask(hub, session, env))
    svc = make_service(tmp_path, channel=link)
    got = await svc.fetch_controller()
    assert got["ok"] and got["pages"] == 3 and got["truncated"] is False and got["unsigned"] == 0
    assert [(a["chain"], a["n"]) for a in got["anchors"]] == [(CHAIN, n) for n in range(1, 2501)]
    assert link.sent[0].body == {"limit": 2000, "page": 1000, "after": ""}


async def test_an_older_controller_answers_once_and_a_reply_with_no_next_is_complete(hub, clock, tmp_path,
                                                                                     make_service):
    session = node_session()
    take_all(hub, session, [mark_head(n) for n in range(1, 4)], clock)

    async def older(env: wire.Envelope) -> wire.Envelope:
        # before paging, the controller read only `limit`: it answers its newest heads once, with no `next`
        reply = await ask(hub, session, wire.Envelope("audit_anchors_get", {"limit": env.body["limit"]}))
        return wire.Envelope(reply.type, reply.body, re=env.id)

    link = Link(older)
    svc = make_service(tmp_path, channel=link)
    got = await svc.fetch_controller()
    assert got["ok"] and got["pages"] == 1 and got["truncated"] is False and got["unsigned"] == 0
    assert [a["n"] for a in got["anchors"]] == [1, 2, 3] and len(link.sent) == 1


async def test_an_older_controller_full_reply_is_taken_as_complete(keys, tmp_path, make_service):
    receipts = [signed(keys, mark_head(n)) for n in range(1, 2001)]

    async def older(env: wire.Envelope) -> wire.Envelope:
        return wire.Envelope("audit_anchors_result", {"receipts": receipts}, re=env.id)

    svc = make_service(tmp_path, channel=Link(older))
    got = await svc.fetch_controller()
    assert got["ok"] and got["pages"] == 1 and len(got["anchors"]) == 2000
    assert got["truncated"] is False  # no `next`: an older controller, and its one reply is complete


async def test_a_cursor_that_repeats_ends_the_walk(tmp_path, make_service):
    async def stuck(env: wire.Envelope) -> wire.Envelope:
        return wire.Envelope("audit_anchors_result", {"receipts": [], "next": f"1:{CHAIN}"}, re=env.id)

    link = Link(stuck)
    svc = make_service(tmp_path, channel=link)
    got = await svc.fetch_controller()
    assert got["ok"] and got["pages"] == 2 and len(link.sent) == 2  # the second reply repeats the cursor: stop
    assert got["truncated"] is True


# ---- retention marks: the head they pruned through, kind kept end to end ----

def test_anchor_of_a_retention_mark_body_is_its_through_head_and_a_checkpoint_has_no_kind():
    pruned = {"v": 1, "kind": "pruned", "chain": CHAIN, "n": 4, "prev": "sha256:" + "c" * 64,
              "at": "2027-01-15T00:00:20Z", "kid": "k_chan", "through_seq": 15, "through_hash": OTHER_HASH,
              "deleted": 15, "kept": [], "cutoff": "2027-01-14T00:00:00Z"}
    a = anchor_rules.anchor_of(pruned)
    assert a == {"v": 1, "kind": "pruned", "chain": CHAIN, "n": 4, "seq": 15, "hash": OTHER_HASH,
                 "kid": "k_chan", "at": "2027-01-15T00:00:20Z"}
    assert anchor_rules.valid(a) is None and anchor_rules.kind_of(a) == "pruned"
    checkpoint = {"v": 1, "kind": "checkpoint", "chain": CHAIN, "n": 2, "seq": 10, "hash": HASH,
                  "kid": "k_chan", "at": "2027-01-15T00:00:10Z", "count": 10, "reason": "rows"}
    cp = anchor_rules.anchor_of(checkpoint)
    assert "kind" not in cp
    assert cp == {"v": 1, "chain": CHAIN, "n": 2, "seq": 10, "hash": HASH, "kid": "k_chan", "at": "2027-01-15T00:00:10Z"}


def test_collect_queues_a_retention_mark_from_a_real_purge_by_its_through_head(traces, tmp_path, make_service):
    svc = make_service(tmp_path, traces=traces)
    assert svc.collect() == 3
    assert traces.purge(older_than_days=0) > 0
    (pruned,) = [m for m in read_marks(traces.db.conn) if m["kind"] == "pruned"]
    body = json.loads(pruned["body"])
    assert svc.collect() == 1 and svc.collect() == 0
    row = svc.store.get(body["chain"], body["n"])
    assert row == anchor_rules.anchor_of(body) and row["kind"] == "pruned"
    assert row["seq"] == body["through_seq"] and row["hash"] == body["through_hash"]
    assert body["n"] in [a["n"] for a in svc.store.pending("controller")]


async def test_the_controller_keeps_a_retention_marks_kind_and_a_checkpoints_receipt_is_unchanged(hub, keys):
    session = node_session()
    heads = [mark_head(1), {**mark_head(2), "kind": "pruned"}]
    reply = await ask(hub, session, wire.Envelope("audit_anchor", {"anchors": heads}))
    checkpoint, pruned = reply.body["receipts"]
    assert "kind" not in checkpoint["anchor"] and pruned["anchor"]["kind"] == "pruned"
    # a checkpoint's receipt is the same bytes the controller signed before retention anchors existed
    assert checkpoint == signed(keys, mark_head(1), received_at=checkpoint["received_at"])
    assert all(verify_controller(keys.controller_pubkey, RECEIPT, NODE, r) for r in (checkpoint, pruned))
    kept = {r["anchor"]["n"]: r["anchor"] for r in hub.store.anchors(ORG, NODE)}
    assert kept[2]["kind"] == "pruned" and "kind" not in kept[1]


async def test_a_retention_mark_goes_to_the_controller_and_comes_back_with_its_kind(hub, traces, tmp_path,
                                                                                    make_service):
    session = node_session()
    svc = make_service(tmp_path, traces=traces, channel=Link(lambda env: ask(hub, session, env)))
    svc.collect()
    traces.purge(older_than_days=0)
    svc.collect()
    (pruned,) = [m for m in read_marks(traces.db.conn) if m["kind"] == "pruned"]
    assert await svc.send_controller() == 4
    got = await svc.fetch_controller()
    assert got["ok"] and got["unsigned"] == 0
    assert [a["n"] for a in got["anchors"] if a.get("kind") == "pruned"] == [pruned["n"]]
    assert all("kind" not in a for a in got["anchors"] if a["n"] != pruned["n"])


# ---- databases from before retention anchors ----

def test_an_anchors_db_from_before_retention_anchors_opens_and_its_receipts_still_verify(tmp_path, keys, clock,
                                                                                         make_service):
    old = sqlite3.connect(tmp_path / "anchors.db")
    old.executescript(OLD_NODE_ANCHORS)
    kept = anchor_rules.core(anchor(1))
    receipt = signed(keys, kept)
    old.execute("INSERT INTO anchors (chain, n, seq, hash, kid, at, receipt, s3_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (CHAIN, 1, kept["seq"], HASH, "k_chan", kept["at"], json.dumps(receipt)))
    old.execute("INSERT INTO anchors (chain, n, seq, hash, kid, at) VALUES (?, ?, ?, ?, ?, ?)",
                (CHAIN, 2, 20, HASH, "k_chan", "2027-01-15T00:00:02Z"))
    old.commit()
    old.close()

    store = AnchorStore(tmp_path / "anchors.db", clock=clock)
    try:
        assert {"kind", "queued_at"} <= {r["name"] for r in store.db.all("PRAGMA table_info(anchors)")}
        assert store.get(CHAIN, 1) == kept and "kind" not in store.get(CHAIN, 1)  # an old row is a checkpoint
        assert [a["n"] for a in store.pending("controller")] == [2]
        assert [a["n"] for a in store.pending("s3")] == [1, 2]
        status = store.target_status("controller")
        assert status["last_seq"] == 10 and status["last_mark"] == 1 and status["last_retention_mark"] is None
        assert status["pending"] == 1 and status["oldest_queued_at"] is None  # old rows were never queued_at
        pruned = {**anchor(3), "kind": "pruned"}
        assert store.add(pruned) is True
        assert store.get(CHAIN, 3)["kind"] == "pruned"
        assert store.target_status("controller")["oldest_queued_at"] == clock.t
        stored = json.loads(store.db.one("SELECT receipt FROM anchors WHERE n = 1")["receipt"])
        assert stored == receipt
        assert anchor_rules.receipt_ok(stored, controller_pubkey=keys.controller_pubkey, node_id=NODE)
    finally:
        store.close()
    again = AnchorStore(tmp_path / "anchors.db", clock=clock)  # opening it a second time changes nothing
    try:
        assert again.get(CHAIN, 3)["kind"] == "pruned" and again.get(CHAIN, 1) == kept
    finally:
        again.close()
    svc = make_service(tmp_path)
    assert svc.receipt_ok(receipt)


async def test_a_controller_db_from_before_retention_anchors_opens_and_its_heads_read_back(tmp_path, keys, clock):
    path = tmp_path / "old-controller" / "controller.db"
    path.parent.mkdir()
    old = sqlite3.connect(path)
    old.executescript(OLD_CONTROLLER_ANCHORS)
    kept = anchor_rules.core(anchor(1))
    receipt = signed(keys, kept)
    old.execute("INSERT INTO audit_anchors (org_id, node_id, chain, n, seq, hash, kid, at, received_at, sig) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ORG, NODE, CHAIN, 1, kept["seq"], HASH, "k_chan", kept["at"], RECEIVED, receipt["sig"]))
    old.commit()
    old.close()

    store = ControllerStore(path, clock=clock)
    hub = Hub(store, keys, clock=clock)
    try:
        assert hub.store.anchors(ORG, NODE) == [receipt]  # an old row reads back as a checkpoint
        assert verify_controller(keys.controller_pubkey, RECEIPT, NODE, receipt)
        reply = hub.take_anchors(node_session(), [{**anchor(2), "kind": "pruned"}, anchor(1, OTHER_HASH)])
        assert reply["conflicts"] == [1]
        (pruned,) = [r for r in hub.store.anchors(ORG, NODE) if r["anchor"]["n"] == 2]
        assert pruned["anchor"]["kind"] == "pruned"
        (conflict,) = store.anchor_conflicts(ORG, NODE)  # the conflicts table has the kind column too
        assert conflict["n"] == 1 and conflict["hash"] == OTHER_HASH and conflict["kind"] is None
    finally:
        store.close()


# ---- status ----

async def test_status_names_each_target_and_what_waits_for_it(hub, traces, tmp_path, make_service, clock):
    session = node_session()
    bucket = FakeBucket()
    svc = make_service(tmp_path, traces=traces, channel=Link(lambda env: ask(hub, session, env)),
                       audit_anchors=["controller", "s3://bkt/agentos"], s3_factory=lambda url: bucket)
    queued_at = clock.t
    assert svc.collect() == 3
    st = svc.status()
    assert st["on"] is True and st["targets"] == ["controller", "s3://bkt/agentos"]
    assert set(st["per_target"]) == {"controller", "s3"}
    for per in st["per_target"].values():
        assert per["pending"] == 3 and per["oldest_queued_at"] == queued_at
        assert per["last_seq"] is None and per["last_mark"] is None and per["last_retention_mark"] is None
    assert st["pending"] == 6 and st["pending_controller"] == 3 and st["oldest_pending_s"] == 0.0
    assert st["old_controller"] is False and st["last_error"] is None
    clock.advance(120)
    assert svc.status()["oldest_pending_s"] == 120.0
    assert await svc.send_controller() == 3
    st = svc.status()
    assert st["per_target"]["controller"]["pending"] == 0
    assert st["per_target"]["controller"]["oldest_queued_at"] is None
    assert st["per_target"]["s3"]["pending"] == 3
    assert st["pending"] == 3 and st["pending_controller"] == 0 and st["oldest_pending_s"] == 120.0


async def test_status_shows_the_newest_checkpoint_and_the_newest_retention_mark_each_target_took(
        hub, traces, tmp_path, make_service):
    session = node_session()
    svc = make_service(tmp_path, traces=traces, channel=Link(lambda env: ask(hub, session, env)))
    svc.collect()
    checkpoints = [json.loads(m["body"]) for m in read_marks(traces.db.conn) if m["kind"] == "checkpoint"]
    assert await svc.send_controller() == 3
    per = svc.status()["per_target"]["controller"]
    assert per["last_mark"] == max(c["n"] for c in checkpoints) and per["last_seq"] == max(c["seq"] for c in checkpoints)
    assert per["last_retention_mark"] is None
    traces.purge(older_than_days=0)
    svc.collect()
    assert await svc.send_controller() == 1
    (pruned,) = [m for m in read_marks(traces.db.conn) if m["kind"] == "pruned"]
    per = svc.status()["per_target"]["controller"]
    assert per["last_retention_mark"] == pruned["n"]
    assert per["last_mark"] == max(c["n"] for c in checkpoints)  # a retention mark is not the newest checkpoint
    assert per["last_seq"] == max(c["seq"] for c in checkpoints) and per["pending"] == 0


def test_status_is_off_with_no_target(tmp_path, make_service):
    assert make_service(tmp_path, audit_anchors=[]).status() == {"on": False, "targets": []}


# ---- an older controller that drops the kind of a retention head ----

async def test_an_older_controller_that_drops_the_kind_keeps_the_receipt_and_is_no_conflict(keys, traces, tmp_path,
                                                                                          make_service):
    async def older(env: wire.Envelope) -> wire.Envelope:
        # stores and signs each head without its kind: its receipt still dates the mark
        receipts = [signed(keys, without_kind(a)) for a in env.body["anchors"]]
        return wire.Envelope("audit_anchor_receipt", {"receipts": receipts, "conflicts": [], "refused": 0}, re=env.id)

    svc = make_service(tmp_path, traces=traces, channel=Link(older))
    svc.collect()
    traces.purge(older_than_days=0)
    svc.collect()
    (pruned,) = [m for m in read_marks(traces.db.conn) if m["kind"] == "pruned"]
    assert await svc.send_controller() == 4
    assert svc.conflicts == 0 and svc.old_controller is True
    assert "older than retention anchors" in svc.last_error
    assert svc.store.pending("controller") == []
    body = json.loads(pruned["body"])
    assert svc.store.get(body["chain"], body["n"])["kind"] == "pruned"  # our own copy still says what it is
    st = svc.status()
    assert st["old_controller"] is True and "older than retention anchors" in st["last_error"]
    stored = json.loads(svc.store.db.one("SELECT receipt FROM anchors WHERE n = ?", (pruned["n"],))["receipt"])
    assert "kind" not in stored["anchor"] and svc.receipt_ok(stored)


async def test_an_older_controller_that_changed_the_head_is_still_a_conflict(keys, traces, tmp_path, make_service):
    async def older(env: wire.Envelope) -> wire.Envelope:
        def stored_form(a: dict) -> dict:
            kept = without_kind(a)
            return {**kept, "hash": OTHER_HASH} if a.get("kind") == "pruned" else kept

        receipts = [signed(keys, stored_form(a)) for a in env.body["anchors"]]
        return wire.Envelope("audit_anchor_receipt", {"receipts": receipts, "conflicts": [], "refused": 0}, re=env.id)

    svc = make_service(tmp_path, traces=traces, channel=Link(older))
    svc.collect()
    traces.purge(older_than_days=0)
    svc.collect()
    await svc.send_controller()
    assert svc.conflicts == 1 and svc.old_controller is False
    assert "another head" in svc.last_error


@pytest.mark.parametrize("cursor", ["²:x", "1" * 26 + ":x", "-1:x", ":x", "abc", 5, None])
def test_a_cursor_that_is_not_a_mark_number_starts_from_the_first_page(hub, cursor):
    session = node_session()
    body = hub.anchors_page(session, {"after": cursor, "page": 10})
    assert body["receipts"] == [] and body["next"] is None  # no error, nothing skipped


def test_the_health_monitor_shows_anchor_status_and_warns_when_heads_wait(tmp_path):
    import types

    from agentos.core.config import NodeConfig
    from agentos.ops.health import ANCHOR_WARN_S, HealthMonitor

    monitor = HealthMonitor(NodeConfig(home=tmp_path))
    st = {"on": True, "per_target": {"controller": {"pending": 0, "last_seq": 10}}, "pending": 0,
          "oldest_pending_s": None, "conflicts": 0, "last_error": None}
    monitor.anchors = types.SimpleNamespace(status=lambda: st)
    (f,) = monitor.check_anchors()
    assert (f.key, f.level) == ("audit.anchors", "ok") and f.data["per_target"]["controller"]["last_seq"] == 10
    st.update(per_target={"controller": {"pending": 3}}, pending=3, oldest_pending_s=ANCHOR_WARN_S + 1,
              last_error="the controller is not connected")
    (f,) = monitor.check_anchors()
    assert f.level == "warn" and "3 head(s)" in f.detail and "not connected" in f.detail
    st.update(conflicts=1)
    assert monitor.check_anchors()[0].level == "critical"
    monitor.anchors = None
    assert monitor.check_anchors() == []


def test_the_node_channel_builds_the_messages_the_anchors_service_sends():
    """The service sits a layer below the channel, so it asks the channel object for its envelopes."""
    from agentos.node.channel.node import NodeChannel

    env = NodeChannel.envelope(SimpleNamespace(), "audit_anchor", {"anchor": {"n": 1}})
    assert isinstance(env, wire.Envelope) and env.type == "audit_anchor" and env.body == {"anchor": {"n": 1}}
    assert env.id.startswith("m_") and env.re is None and env.seq is None
