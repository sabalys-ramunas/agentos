"""QA bug hunt on the signed audit log (policy/AUDIT-LOG.md). Each xfail asserts what the spec
promises and fails today; the reason names the bug (QA-audit-<n>)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_policy_support import Clock

from agentos.audit.service import AuditService, verify_bundle
from agentos.core.errors import InvalidRequest
from agentos.policy.auditlog import Scope, canonical, row_digest
from agentos.policy.traces import Cost, TraceEvent, TraceStore

VECTORS = json.loads((Path(__file__).parents[1] / "fixtures" / "audit" / "vectors.json").read_text())
LANE = "biz-a/instagram"
DAY = "2027-01-15"  # T0 is 2027-01-15T08:00Z
OLD = 100 * 86400   # older than the 90 days of retention


def ev(i: int = 0, **kw) -> TraceEvent:
    return TraceEvent(**{"kind": "tool_end", "tool": "browser_act", "lane": LANE, "status": "ok",
                         "summary": f"step {i}", "args": {"i": i}, **kw})


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = TraceStore(tmp_path / "traces.db", clock=clock, checkpoint_rows=10_000)
    yield s
    s.close()


def raw_conn(path: Path) -> sqlite3.Connection:
    """The file as someone with write access to it sees it: the append-only triggers dropped."""
    raw = sqlite3.connect(path, isolation_level=None)
    for t in ("traces_append_only", "audit_marks_no_update", "audit_marks_no_delete"):
        raw.execute(f"DROP TRIGGER IF EXISTS {t}")
    return raw


def tampered(report) -> list[str]:
    return [f.text for f in report.findings if f.level == "tampered"]


def verdict(fn) -> str:
    """The report status, or the exception verify raised instead of giving one."""
    try:
        return fn().status
    except Exception as e:  # noqa: BLE001
        return f"raised {type(e).__name__}: {e}"


# ---------------------------------------------------------------- verify --since

def test_qa_audit_1_verify_since_a_quiet_window_is_not_tampered(store, clock):
    for i in range(3):
        store.write(ev(i))
    store.checkpoint()
    assert store.verify().status == "ok"
    # `agentos audit verify --since 1h` (or GET /api/audit/verify?day=...) on a quiet hour or day
    report = store.verify(scope=Scope(since=clock.t + 3600))
    assert tampered(report) == []


def test_qa_audit_2_rows_deleted_at_the_start_of_a_since_window_are_reported(tmp_path, store, clock):
    for i in range(1, 6):
        store.write(ev(i))
    clock.advance(86400)
    since = clock.t
    for i in range(6, 11):
        store.write(ev(i))
    store.checkpoint()
    raw = raw_conn(tmp_path / "traces.db")
    raw.execute("DELETE FROM traces WHERE seq IN (6, 7)")  # the first two rows of the second day
    raw.close()
    assert tampered(store.verify())  # the full walk sees the hole
    # "tampering anywhere is always reported", and these rows are inside the window
    assert store.verify(scope=Scope(since=since)).status == "tampered"


# ---------------------------------------------------------------- export bundles

def test_qa_audit_3_an_honest_export_of_a_past_day_verifies(tmp_path, store, clock):
    svc = AuditService(store, home=tmp_path, clock=clock)
    store.write(TraceEvent(kind="tool_end", tool="browser_act", lane=LANE, summary="on the day"))
    clock.advance(86400)  # the next day, same lane, no checkpoint in between
    store.write(TraceEvent(kind="tool_end", tool="browser_act", lane=LANE, summary="the next day"))
    out = svc.export(tmp_path / "day.zip", day=DAY, lane=LANE, tz="UTC")
    # the next day's row of the lane is shipped in full, outside the bundle's scope: as a stub it could
    # not be told from a row of the day the export left out (QA-audit-3, AUDIT-LOG.md "Export bundles")
    assert out["rows"] == 1 and out["context"] == 1 and out["stubs"] == 0
    assert tampered(verify_bundle(Path(out["path"]))) == []


# ---------------------------------------------------------------- retention

def test_qa_audit_4_retention_after_an_unpin_stays_lawful(store, clock):
    old = [store.write(ev(i, at=clock.t - OLD)) for i in range(5)]
    store.pin(old[2].id, "mem_a")
    store.write(ev(10))
    store.write(ev(11))
    store.checkpoint()
    assert store.purge(older_than_days=90) == 4  # pruned through seq 5, kept seq 3
    assert store.verify().status == "ok"
    store.unpin("mem_a")  # the memory entry that needed it was deleted
    assert store.purge(older_than_days=90) == 1  # the next daily run removes seq 3
    assert tampered(store.verify()) == []


def test_qa_audit_5_a_seq_is_never_reused_after_retention(tmp_path, clock):
    s = TraceStore(tmp_path / "traces.db", clock=clock)
    old = [s.write(ev(i, at=clock.t - OLD)) for i in range(5)]
    s.pin(old[2].id, "mem_a")
    s.checkpoint()
    s.purge(older_than_days=90)  # pruned through seq 5, kept seq 3
    s.unpin("mem_a")
    s.purge(older_than_days=90)  # the table is empty now
    s.close()
    s = TraceStore(tmp_path / "traces.db", clock=clock)  # the daemon restarts, or the CLI opens the file
    try:
        e = s.write(ev(99))
        seq = s.row(e.id)["seq"]
        s.checkpoint()
        status = s.verify().status
    finally:
        s.close()
    assert (seq, status) == (6, "ok")  # seq 1-5 were used (SQLite's counter says 5)


def test_qa_audit_7_an_unsigned_retention_mark_does_not_make_kept_rows_tampered(store, clock):
    old = [store.write(ev(i, at=clock.t - OLD)) for i in range(3)]
    store.pin(old[0].id, "mem")
    store.write(ev(9))
    store.checkpoint()
    store.keys.forget(json.loads(store.marks()[0]["body"])["kid"])  # the audit key file is lost
    store.purge(older_than_days=90)  # writes the pruned mark unsigned
    report = store.verify()
    assert any("cannot be trusted, so rows before seq 3 are not checked" in f.text for f in report.findings)
    assert tampered(report) == []


def test_qa_audit_8_an_unchained_row_added_below_a_retention_mark_is_tampered(tmp_path, clock):
    path = tmp_path / "traces.db"
    old = sqlite3.connect(path)
    old.executescript("""
    CREATE TABLE traces (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, at REAL NOT NULL,
      kind TEXT NOT NULL, principal TEXT, principal_kind TEXT, grant_id TEXT, grant_path TEXT, tool TEXT, lane TEXT,
      workspace TEXT, effect TEXT, status TEXT NOT NULL, summary TEXT, args TEXT, error TEXT,
      cost_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, duration_ms INTEGER, run_id TEXT,
      parent_id TEXT);
    """)
    old.executemany("INSERT INTO traces (id, at, kind, status, lane) VALUES (?, ?, 'e', 'ok', ?)",
                    [("tr_old1", clock.t - OLD, LANE), ("tr_old2", clock.t - OLD, LANE)])
    old.commit()
    old.close()
    s = TraceStore(path, clock=clock)  # a log made before format 1: genesis after seq 2
    s.write(ev(1))
    s.purge(older_than_days=90)  # removes both pre-chain rows: pruned through seq 2, kept nothing
    s.checkpoint()
    assert s.verify().status == "ok"
    raw = raw_conn(path)
    raw.execute("INSERT INTO traces (seq, id, at, kind, status, lane, summary) VALUES "
                "(1, 'tr_forged', ?, 'tool_end', 'ok', ?, 'approved the transfer')", (clock.t - OLD, LANE))
    raw.close()
    try:
        status = s.verify().status
    finally:
        s.close()
    # AUDIT-LOG.md: "a row below a retention mark it did not keep" is tampered
    assert status == "tampered"


# ---------------------------------------------------------------- the write path

@pytest.mark.parametrize("poison", [{"lane": "biz-a/x\ud800"}, {"cost": Cost(tokens=2**63)}], ids=["surrogate", "bigint"])
def test_qa_audit_6_a_bad_value_never_fails_or_wedges_the_log(store, poison):
    store.write(ev(0))
    raised = []
    for e in (ev(1, **poison), ev(2)):  # the bad row, then an ordinary one
        try:
            store.write(e)
        except Exception as exc:  # noqa: BLE001
            raised.append(type(exc).__name__)
    # traces.py: "Writing never fails a tool call"; write(): "Redact and append. Never raises."
    assert raised == [] and store.pending_count == 0
    store._pending.clear()  # let teardown close cleanly today


def test_qa_audit_10_negative_zero_cost_verifies(store):
    store.write(ev(1, cost=Cost(usd=-0.0)))
    store.checkpoint()
    assert tampered(store.verify()) == []


# ---------------------------------------------------------------- marks

@pytest.mark.parametrize("pub", ["!!not-base64!!", "AAAA"], ids=["not-base64", "short"])
def test_qa_audit_9_an_edited_genesis_key_is_reported_not_a_crash(tmp_path, store, pub):
    store.write(ev(1))
    store.checkpoint()
    raw = raw_conn(tmp_path / "traces.db")
    body = json.loads(raw.execute("SELECT body FROM audit_marks WHERE n = 1").fetchone()[0])
    raw.execute("UPDATE audit_marks SET body = ? WHERE n = 1", (canonical({**body, "pub": pub}).decode(),))
    raw.close()
    assert verdict(store.verify) == "tampered"


# ---------------------------------------------------------------- canonical form

@pytest.mark.parametrize("col,honest,other", [
    ("cost_usd", float("inf"), "Infinity"),   # REAL inf and TEXT 'Infinity': SQLite stores both in a REAL column
    ("cost_usd", 1e-05, "0.00001"),           # a REAL and a JSON string in a bundle row
    ("summary", "\x00blob:41", b"A"),          # honest text (redaction keeps NUL) and a BLOB
    ("cost_tokens", 1, True),                 # an integer and a JSON boolean in a bundle row
], ids=["inf-text", "real-text", "nul-text-blob", "int-bool"])
def test_qa_audit_11_two_different_records_never_share_a_digest(col, honest, other):
    base = dict(VECTORS["rows"][1]["row"])
    assert row_digest({**base, col: honest}) != row_digest({**base, col: other})


def test_qa_audit_11_precondition_sqlite_keeps_both_values_apart():
    """Not a bug by itself: shows the two values of the first case are two different stored rows."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (r REAL, s TEXT)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(float("inf"), "\x00blob:41"), ("Infinity", b"A")])
    assert db.execute("SELECT typeof(r), typeof(s) FROM t ORDER BY rowid").fetchall() == \
        [("real", "text"), ("text", "blob")]


# ---------------------------------------------------------------- share links

def test_qa_audit_12_a_share_for_a_lane_the_replay_refuses_is_refused_when_made(tmp_path, store, clock):
    svc = AuditService(store, home=tmp_path, clock=clock)
    store.write(ev(1))
    try:
        svc.share_create(lane="biz-a", day=DAY, tz="UTC")  # replay(): "lane must look like workspace/name"
    except InvalidRequest:
        return
    raise AssertionError("share_create signed a link for a lane that replay refuses")
