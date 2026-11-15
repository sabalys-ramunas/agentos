"""The hash chain over the action log: test vectors, the write path (spill, crash, two writers),
the genesis migration, checkpoints, retention anchors and key rotation."""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_policy_support import Clock

from agentos.policy import auditlog
from agentos.policy.auditlog import (
    SIG_CTX,
    canonical,
    lane_tag,
    mark_hash,
    pub_text,
    real,
    row_digest,
    row_hash,
    row_record,
    sig_ok,
)
from agentos.policy.traces import TraceEvent, TraceStore

VECTORS = json.loads((Path(__file__).parents[1] / "fixtures" / "audit" / "vectors.json").read_text())


def ev(i: int = 0, **kw) -> TraceEvent:
    return TraceEvent(**{"kind": "tool_end", "tool": "browser_act", "lane": "biz-a/instagram", "status": "ok",
                         "summary": f"step {i}", "args": {"i": i}, **kw})


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = TraceStore(tmp_path / "traces.db", clock=clock, checkpoint_rows=10_000)
    yield s
    s.close()


# ---------------------------------------------------------------- the format

@pytest.mark.parametrize("value,text", VECTORS["reals"])
def test_real_vectors(value, text):
    assert real(value) == text


def test_row_vectors():
    prev = VECTORS["marks"][0]["hash"]
    for v in VECTORS["rows"]:
        row = v["row"]
        assert canonical(row_record(row)).decode() == v["canonical"]
        assert row_digest(row) == v["digest"]
        assert lane_tag(row["lane"], row["id"]) == v["lane_tag"]
        assert v["prev"] == prev
        assert row_hash(prev, row) == v["hash"]
        prev = v["hash"]


def test_mark_vectors():
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(VECTORS["key"]["seed_hex"]))
    assert pub_text(key) == VECTORS["key"]["pub"]
    for m in VECTORS["marks"]:
        data = canonical(m["body"])
        assert data.decode() == m["canonical"] and mark_hash(m["body"]) == m["hash"]
        assert sig_ok(VECTORS["key"]["pub"], SIG_CTX, data, m["sig"])
    # the vector log verifies as a whole: two pre-chain rows, three chained, one checkpoint
    rows = [{**v["row"], "prev": v["prev"], "hash": v["hash"]} for v in VECTORS["rows"]]
    marks = [{"n": m["body"]["n"], "kind": m["body"]["kind"], "body": m["canonical"], "hash": m["hash"],
              "sig": m["sig"]} for m in VECTORS["marks"]]
    report = auditlog.verify(rows, marks, trusted={VECTORS["key"]["kid"]})
    assert report.status == "ok", report.public()


def test_hash_covers_the_redacted_row_only(store):
    """The secret never reaches a hashed field: what is hashed is what is stored."""
    store.write(ev(summary="Cookie: sid=s3cr3tv4lue", args={"password": "hunter2"}))
    row = next(store.chain_rows())
    assert "s3cr3tv4lue" not in json.dumps(row) and "hunter2" not in json.dumps(row)
    assert row["hash"] == row_hash(row["prev"], row)


# ---------------------------------------------------------------- the write path

def test_rows_chain_in_order_from_the_genesis(store):
    for i in range(5):
        store.write(ev(i))
    rows = list(store.chain_rows())
    genesis = store.marks()[0]
    assert genesis["kind"] == "genesis" and rows[0]["prev"] == genesis["hash"]
    assert [r["seq"] for r in rows] == [1, 2, 3, 4, 5]
    assert all(b["prev"] == a["hash"] for a, b in itertools.pairwise(rows))
    assert store.verify().status == "pending"  # nothing signed covers them yet
    store.checkpoint()
    assert store.verify().status == "ok"


def test_the_existing_api_shape_is_unchanged(store):
    e = store.write(ev(1))
    assert set(store.get(e.id).public()) == set(TraceEvent().public())
    assert store.query()[0][0].id == e.id


def test_locked_database_defers_then_chains_in_write_order(tmp_path, clock):
    s = TraceStore(tmp_path / "traces.db", clock=clock, busy_timeout_ms=1)
    s.write(ev(0))
    other = sqlite3.connect(tmp_path / "traces.db", timeout=0)
    other.execute("BEGIN IMMEDIATE")  # another process holds the write lock
    for i in range(1, 4):
        s.write(ev(i))  # never raises
    assert s.pending_count == 3
    other.execute("ROLLBACK")
    other.close()
    assert s.flush() == 0
    rows = list(s.chain_rows())
    assert [json.loads(r["args"])["i"] for r in rows] == [0, 1, 2, 3]
    s.checkpoint()
    assert s.verify().status == "ok"
    s.close()


def test_spill_keeps_order_and_a_crash_before_the_flush_does_not_fork(tmp_path, clock):
    s = TraceStore(tmp_path / "traces.db", clock=clock, busy_timeout_ms=1)
    s.MAX_PENDING = 2
    s.write(ev(0))
    other = sqlite3.connect(tmp_path / "traces.db", timeout=0)
    other.execute("BEGIN IMMEDIATE")
    for i in range(1, 5):
        s.write(ev(i))
    assert s.spill_path.exists()  # spilled past the bound: unchained, in write order
    spilled = [json.loads(line) for line in s.spill_path.read_text().splitlines()]
    # once a spill file exists, later rows join it at once: nothing waits only in memory
    assert [json.loads(r["args"])["i"] for r in spilled] == [1, 2, 3, 4]
    assert "hash" not in spilled[0] and "seq" not in spilled[0]
    other.execute("ROLLBACK")
    other.close()
    # the process dies here: nothing in memory survives, the spill file does
    s.db.close()
    s2 = TraceStore(tmp_path / "traces.db", clock=clock)
    s2.write(ev(9))
    order = [json.loads(r["args"])["i"] for r in s2.chain_rows()]
    assert order[:1] == [0] and order[-1] == 9 and sorted(order[1:-1]) == order[1:-1]
    s2.checkpoint()
    assert s2.verify().status == "ok"
    s2.close()


def test_a_spill_replayed_after_a_crash_after_commit_is_not_written_twice(tmp_path, clock):
    s = TraceStore(tmp_path / "traces.db", clock=clock)
    rows = [s._row(s._redacted(ev(i), ())) for i in range(3)]
    with s._tx() as c:
        s._insert(c, rows)  # committed, then the crash: the renamed spill file was never deleted
    leftover = s.spill_path.with_name(f"{s.spill_path.name}.{1:020d}-1.restoring")
    leftover.write_text("".join(json.dumps(r) + "\n" for r in rows))
    s._spilled = True
    s.write(ev(3))
    assert not leftover.exists()
    assert [json.loads(r["args"])["i"] for r in s.chain_rows()] == [0, 1, 2, 3]
    s.checkpoint()
    assert s.verify().status == "ok"
    s.close()


def test_two_writers_never_fork_the_chain(tmp_path, clock):
    a = TraceStore(tmp_path / "traces.db", clock=clock)
    b = TraceStore(tmp_path / "traces.db", clock=clock)  # the CLI next to the daemon
    for i in range(20):
        (a if i % 3 else b).write(ev(i))
    assert len(a.marks()) == 1  # one genesis, however many stores opened the file
    a.checkpoint()
    report = a.verify()
    assert report.status == "ok" and report.checked == 20, report.public()
    a.close()
    b.close()


# ---------------------------------------------------------------- migration

def test_an_old_log_migrates_with_a_genesis_and_its_rows_stay_unchained(tmp_path, clock):
    path = tmp_path / "traces.db"
    old = sqlite3.connect(path)
    old.executescript("""
    CREATE TABLE traces (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, at REAL NOT NULL,
      kind TEXT NOT NULL, principal TEXT, principal_kind TEXT, grant_id TEXT, grant_path TEXT, tool TEXT, lane TEXT,
      workspace TEXT, effect TEXT, status TEXT NOT NULL, summary TEXT, args TEXT, error TEXT,
      cost_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, duration_ms INTEGER, run_id TEXT,
      parent_id TEXT);
    INSERT INTO traces (id, at, kind, status, lane) VALUES ('tr_old1', 1, 'e', 'ok', 'biz-a/x'),
      ('tr_old2', 2, 'e', 'ok', 'biz-a/x');
    """)
    old.commit()
    old.close()
    s = TraceStore(path, clock=clock)
    genesis = json.loads(s.marks()[0]["body"])
    assert genesis["after_seq"] == 2 and genesis["unchained"] == 2
    s.write(ev(1))
    rows = list(s.chain_rows())
    assert [r["hash"] is None for r in rows] == [True, True, False]
    s.checkpoint()
    report = s.verify()
    assert report.status == "ok" and report.unchained == 2 and report.exit_code == 0  # QA 2026-11-15 F14
    assert any("written before the chain began" in f.text and f.level == "note" and f.first == 1 and f.last == 2
               for f in report.findings)
    assert s.verify(scope=auditlog.Scope(since=clock.t - 10)).status == "ok"  # today's rows are proven
    s.close()


# ---------------------------------------------------------------- checkpoints

def test_checkpoints_every_n_rows_every_m_minutes_and_at_shutdown(tmp_path, clock):
    s = TraceStore(tmp_path / "traces.db", clock=clock, checkpoint_rows=5, checkpoint_s=600)
    for i in range(12):
        s.write(ev(i))
    reasons = [json.loads(m["body"]).get("reason") for m in s.marks() if m["kind"] == "checkpoint"]
    assert reasons == ["rows", "rows"]
    s.write(ev(12))
    clock.advance(601)
    s.flush()  # the policy tick: a quiet node still gets its time checkpoint
    s.write(ev(13))
    s.close()
    s2 = TraceStore(tmp_path / "traces.db", clock=clock)
    cps = [json.loads(m["body"]) for m in s2.marks() if m["kind"] == "checkpoint"]
    assert [c["reason"] for c in cps] == ["rows", "rows", "time", "shutdown"]
    assert cps[-1]["seq"] == 14 and cps[-1]["count"] == 14
    assert s2.verify().status == "ok"
    s2.close()


# ---------------------------------------------------------------- retention

def test_retention_writes_a_signed_anchor_and_keeps_pinned_rows(store, clock):
    old = [store.write(ev(i, at=clock.t - 100 * 86400)) for i in range(4)]
    store.pin(old[1].id, "mem_a")
    store.write(ev(9))
    store.checkpoint()
    assert store.purge(older_than_days=90) == 3
    pruned = json.loads(store.marks()[-1]["body"])
    assert pruned["kind"] == "pruned" and pruned["through_seq"] == 4 and pruned["deleted"] == 3
    assert pruned["kept"] == [[2, next(store.chain_rows())["hash"]]]
    store.write(ev(10))
    store.checkpoint()
    report = store.verify()
    assert report.status == "ok", report.public()


def test_retention_can_remove_every_row_and_the_chain_goes_on(store, clock):
    for i in range(3):
        store.write(ev(i, at=clock.t - 100 * 86400))
    assert store.purge(older_than_days=90) == 3
    store.write(ev(3))
    rows = list(store.chain_rows())
    assert rows[0]["seq"] == 4 and rows[0]["prev"] == json.loads(store.marks()[-1]["body"])["through_hash"]
    store.checkpoint()
    assert store.verify().status == "ok"


# ---------------------------------------------------------------- keys

def test_the_audit_key_is_its_own_file_and_rotates(tmp_path, clock):
    s = TraceStore(tmp_path / "traces.db", clock=clock)
    secrets = tmp_path / "secrets"
    first = json.loads(s.marks()[0]["body"])["kid"]
    keyfile = secrets / f"audit-{first.removeprefix('ed25519:')}.key"
    assert keyfile.exists() and oct(os.stat(keyfile).st_mode & 0o777) == "0o600"
    assert not (secrets / "policy.key").exists()  # never the grant signing key
    s.write(ev(1))
    s.checkpoint()
    out = s.rotate_audit_key()
    assert out["previous"] == first and not keyfile.exists()  # the old private key is gone
    s.write(ev(2))
    s.checkpoint()
    cps = [json.loads(m["body"]) for m in s.marks() if m["kind"] == "checkpoint"]
    assert [c["kid"] for c in cps] == [first, out["kid"]]
    assert s.verify().status == "ok" and s.keys.trusted_kids() == {first, out["kid"]}
    s.close()


def test_a_key_reset_is_reported_not_hidden(tmp_path, clock):
    s = TraceStore(tmp_path / "traces.db", clock=clock)
    s.write(ev(1))
    kid = json.loads(s.marks()[0]["body"])["kid"]
    s.keys.forget(kid)  # the key file is lost
    with pytest.raises(Exception, match="--reset"):
        s.rotate_audit_key()
    s.rotate_audit_key(reset=True)
    s.write(ev(2))
    s.checkpoint()
    # this node recorded the reset key in its trust file, so its own verify accepts it
    assert s.verify().status == "ok"
    # an auditor who only trusts the original key is told
    report = auditlog.verify_db(s.db.conn, trusted={kid}, lock=s.db.lock)
    assert report.status == "unverifiable"
    assert any("reset the audit key" in f.text for f in report.findings)
    s.close()


# ---------------------------------------------------------------- values agentos never stores (2026-11-04)

@pytest.mark.parametrize("v", VECTORS["typed"], ids=lambda v: f"{v['column']}-{v['type']}")
def test_typed_vectors(v):
    value = bytes.fromhex(v["value"]) if v["type"] == "blob" else v["value"]
    row = {**VECTORS["rows"][1]["row"], v["column"]: value}
    assert row_record(row)[v["column"]] == v["record_value"]
    assert row_digest(row) == v["digest"]
    assert row_digest(row) != VECTORS["rows"][1]["digest"]


@pytest.mark.parametrize("v", VECTORS["stored"], ids=lambda v: v["given"][:30])
def test_stored_vectors(v, tmp_path):
    """The write path gives every value a form SQLite binds and UTF-8 encodes (AUDIT-LOG.md "Write path")."""
    s = TraceStore(tmp_path / "traces.db", clock=lambda: 1793750400.0)
    try:
        e = s.write(TraceEvent(**{"id": v["row"]["id"], "at": v["row"]["at"], "kind": "tool_end", "tool": "browser_act",
                                  "status": "ok", **json.loads(v["given"])}))
        row = s.row(e.id)
        assert {k: row[k] for k in v["stored"]} == v["stored"]
        assert {k: row[k] for k in v["row"]} == v["row"]
        assert row_digest(row) == v["digest"]
        s.checkpoint()
        assert s.verify().status == "ok"
    finally:
        s.close()


def test_a_row_sqlite_refuses_becomes_a_stub_and_never_blocks_the_next(store, monkeypatch):
    real_row = store._row

    def bad_row(ev):  # a value the normalisation does not know about (a future column, a new type)
        row = real_row(ev)
        if ev.summary == "poison":
            row["summary"] = object()
        return row

    monkeypatch.setattr(store, "_row", bad_row)
    store.write(ev(summary="poison"))
    store.write(ev(2))
    assert store.pending_count == 0
    rows = list(store.chain_rows())
    assert [r["summary"] for r in rows] == ["[trace could not be stored]", "step 2"]
    store.checkpoint()
    assert store.verify().status == "ok"


def test_the_head_never_goes_below_the_seq_counter(tmp_path, clock):
    path = tmp_path / "traces.db"
    s = TraceStore(path, clock=clock)
    for i in range(3):
        s.write(ev(i))
    s.close()
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("UPDATE sqlite_sequence SET seq = 9 WHERE name = 'traces'")
    raw.close()
    s = TraceStore(path, clock=clock)
    try:
        e = s.write(ev(4))
        assert s.row(e.id)["seq"] == 10  # a used seq is never given again; verify names the gap
        assert s.verify().status == "tampered"
    finally:
        s.close()
