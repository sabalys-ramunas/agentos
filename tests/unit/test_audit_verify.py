"""`agentos audit verify`: one test per kind of tampering, each finding one sentence with seq
numbers, and distinct exit codes for ok, tampered and unverifiable."""

from __future__ import annotations

import json
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_policy_support import Clock
from typer.testing import CliRunner

from agentos.policy.auditlog import (
    EXIT_OK,
    EXIT_TAMPERED,
    SIG_CTX,
    canonical,
    mark_hash,
    pub_text,
    row_hash,
    sign,
)
from agentos.policy.tokens import key_id
from agentos.policy.traces import TraceEvent, TraceStore


def ev(i: int, lane: str = "biz-a/instagram") -> TraceEvent:
    return TraceEvent(kind="tool_end", tool="browser_act", lane=lane, status="ok", summary=f"step {i}", args={"i": i})


@pytest.fixture
def log(tmp_path):
    """Five rows covered by a checkpoint (mark 2), the triggers dropped so a test can tamper like
    someone with the file would."""
    s = TraceStore(tmp_path / "traces.db", clock=Clock(), checkpoint_rows=10_000)
    for i in range(1, 6):
        s.write(ev(i))
    s.checkpoint()
    raw = sqlite3.connect(tmp_path / "traces.db", isolation_level=None)
    for t in ("traces_append_only", "audit_marks_no_update", "audit_marks_no_delete"):
        raw.execute(f"DROP TRIGGER {t}")
    yield s, raw
    raw.close()
    s.close()


def findings(s: TraceStore, level: str = "tampered") -> list[str]:
    return [f.text for f in s.verify().findings if f.level == level]


def test_untouched_log_is_ok(log):
    s, _ = log
    report = s.verify()
    assert report.status == "ok" and report.exit_code == EXIT_OK and report.proven_through["seq"] == 5


def test_edited_row(log):
    s, raw = log
    raw.execute("UPDATE traces SET summary = 'step 3 (nothing happened)' WHERE seq = 3")
    assert findings(s) == ["row seq 3 was edited after it was written: its contents no longer match its hash"]
    assert s.verify().exit_code == EXIT_TAMPERED


def test_edited_row_with_its_hash_recomputed(log):
    s, raw = log
    row = dict(next(r for r in s.chain_rows() if r["seq"] == 3), summary="harmless")
    raw.execute("UPDATE traces SET summary = ?, hash = ? WHERE seq = 3", ("harmless", row_hash(row["prev"], row)))
    assert findings(s) == ["row seq 4 does not link to row seq 3: row 3 was rewritten or replaced"]


def test_deleted_middle_row(log):
    s, raw = log
    raw.execute("DELETE FROM traces WHERE seq = 3")
    assert findings(s) == [("row seq 3 is missing from the middle of the log (row seq 4 links to a row that is not "
                           "there)")]


def test_reordered_rows(log):
    s, raw = log
    raw.execute("UPDATE traces SET seq = -3 WHERE seq = 3")
    raw.execute("UPDATE traces SET seq = 3 WHERE seq = 4")
    raw.execute("UPDATE traces SET seq = 4 WHERE seq = -3")
    assert findings(s) == [("rows seq 3 and 4 are out of chain order: row 3 links to row 4, which is stored after it "
                           "(rows were reordered)")]


def test_truncated_tail_after_the_last_checkpoint_is_unprovable_not_fine(log):
    s, raw = log
    for i in range(6, 9):
        s.write(ev(i))
    raw.execute("DELETE FROM traces WHERE seq > 6")
    report = s.verify()
    texts = [f.text for f in report.findings]
    assert "1 newer row (row seq 6) waits for the next checkpoint after checkpoint 2" in texts, texts
    # SQLite's own counter still remembers seq 8
    assert "the log has used seq up to 8, but it now ends at seq 6: rows seq 7-8 were deleted from the end" in texts
    # a careful attacker resets the counter too: what is left is still not called fine
    raw.execute("UPDATE sqlite_sequence SET seq = 6 WHERE name = 'traces'")
    report = s.verify()
    assert report.status == "pending" and report.exit_code == EXIT_OK
    assert [f.level for f in report.findings] == ["pending"]


def test_rows_deleted_from_the_end_below_a_checkpoint(log):
    s, raw = log
    raw.execute("DELETE FROM traces WHERE seq > 3")
    raw.execute("UPDATE sqlite_sequence SET seq = 3 WHERE name = 'traces'")
    assert findings(s) == [("the log ends at seq 3, but checkpoint 2 covers rows through seq 5: rows seq 4-5 were "
                           "deleted from the end")]


def test_forged_checkpoint(log):
    s, raw = log
    body = json.loads(raw.execute("SELECT body FROM audit_marks WHERE n = 2").fetchone()[0])
    forged = {**body, "seq": 4, "hash": next(r for r in s.chain_rows() if r["seq"] == 4)["hash"], "count": 4}
    raw.execute("UPDATE audit_marks SET body = ?, hash = ? WHERE n = 2", (canonical(forged).decode(),
                                                                         mark_hash(forged)))
    assert findings(s) == ["mark 2 (checkpoint) has a signature that does not verify: it was forged or edited"]


def test_swapped_key(log, tmp_path):
    s, raw = log
    s.write(ev(6))
    mine = Ed25519PrivateKey.generate()
    last = raw.execute("SELECT n, hash FROM audit_marks ORDER BY n DESC LIMIT 1").fetchone()
    head = raw.execute("SELECT seq, hash FROM traces ORDER BY seq DESC LIMIT 1").fetchone()
    body = {"v": 1, "kind": "checkpoint", "chain": json.loads(s.marks()[0]["body"])["chain"], "n": last[0] + 1,
            "prev": last[1], "at": "2027-01-15T08:00:00Z", "kid": key_id(mine), "seq": head[0], "hash": head[1],
            "count": head[0], "reason": "rows"}
    data = canonical(body)
    raw.execute("INSERT INTO audit_marks (n, kind, body, hash, sig) VALUES (?, 'checkpoint', ?, ?, ?)",
                (body["n"], data.decode(), mark_hash(body), sign(mine, SIG_CTX, data)))
    genesis_kid = json.loads(s.marks()[0]["body"])["kid"]
    assert findings(s) == [(f"mark 3 (checkpoint) is signed by key {key_id(mine)}, but the audit key at that point was "
                           f"{genesis_kid}: the key was swapped")]


def test_whole_log_resigned_with_another_key(log):
    s, raw = log
    mine = Ed25519PrivateKey.generate()
    marks = s.marks()
    prev = None
    for m in marks:  # rewrite every mark with the attacker's key, as if it had been there from the start
        body = json.loads(m["body"])
        body.update(kid=key_id(mine), prev=prev, **({"pub": pub_text(mine)} if body["kind"] == "genesis" else {}))
        data = canonical(body)
        raw.execute("UPDATE audit_marks SET body = ?, hash = ?, sig = ? WHERE n = ?",
                    (data.decode(), mark_hash(body), sign(mine, SIG_CTX, data), m["n"]))
        prev = mark_hash(body)
    first = next(s.chain_rows())
    raw.execute("UPDATE traces SET prev = ? WHERE seq = 1", (mark_hash(json.loads(raw.execute(
        "SELECT body FROM audit_marks WHERE n = 1").fetchone()[0])),))
    assert first["seq"] == 1
    texts = findings(s)
    assert any(t.startswith(f"the log starts from audit key {key_id(mine)}, which this node never trusted") and
               "the key or the whole log was swapped" in t for t in texts), texts


def test_deleted_checkpoint_mark(log):
    s, raw = log
    s.write(ev(6))
    s.checkpoint()
    raw.execute("DELETE FROM audit_marks WHERE n = 2")
    assert "marks 2-2 are missing: checkpoints were deleted" in findings(s)


def test_lawful_retention_versus_a_deleted_middle(tmp_path):
    clock = Clock()
    s = TraceStore(tmp_path / "traces.db", clock=clock)
    old = [s.write(TraceEvent(kind="e", lane="biz-a/x", at=clock.t - 100 * 86400)) for _ in range(3)]
    s.pin(old[0].id, "mem")
    for i in range(3):
        s.write(ev(i))
    s.purge(older_than_days=90)
    s.checkpoint()
    assert s.verify().status == "ok"
    raw = sqlite3.connect(tmp_path / "traces.db", isolation_level=None)
    raw.execute("DROP TRIGGER traces_append_only")
    raw.execute("DELETE FROM traces WHERE seq = 1")  # the pinned row the retention mark kept
    texts = [f.text for f in s.verify().findings]
    assert texts == ["row seq 1 was kept through retention by mark 2 and is now missing"]
    raw.execute("DELETE FROM traces WHERE seq = 5")  # and now a real hole above the anchor
    assert "row seq 5 is missing from the middle of the log (row seq 6 links to a row that is not there)" in \
        [f.text for f in s.verify().findings]
    raw.close()
    s.close()


def test_since_and_lane_scope(tmp_path):
    clock = Clock()
    s = TraceStore(tmp_path / "traces.db", clock=clock)
    s.write(ev(1, lane="biz-a/x"))
    s.checkpoint()
    clock.advance(120)  # less than the 10 minutes after which a time checkpoint would cover it
    s.write(ev(2, lane="biz-a/y"))
    from agentos.policy.auditlog import Scope

    # the uncovered row is on another lane, or before the window: the scope is proven
    assert s.verify(scope=Scope(lane="biz-a/x")).status == "ok"
    report = s.verify(scope=Scope(since=clock.t - 30))
    assert report.status == "pending" and report.findings[0].first == 2
    s.close()


# ---------------------------------------------------------------- the CLI

def test_cli_exit_codes(tmp_path, monkeypatch):
    from agentos.audit.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    s = TraceStore(tmp_path / "traces.db", secrets_dir=tmp_path / "secrets")
    for i in range(3):
        s.write(ev(i))
    s.checkpoint()
    s.close()
    runner = CliRunner()
    ok = runner.invoke(app, ["verify"])
    assert ok.exit_code == EXIT_OK and "status: ok" in ok.output
    s = TraceStore(tmp_path / "traces.db", secrets_dir=tmp_path / "secrets", checkpoint_rows=10_000)
    s.write(ev(9))
    s.db.close()  # no shutdown checkpoint: the last row stays uncovered
    unv = runner.invoke(app, ["verify", "--json"])
    assert unv.exit_code == EXIT_OK and json.loads(unv.output)["status"] == "pending"
    assert json.loads(unv.output)["findings"][0]["seq"] == [4, 4]
    shown = runner.invoke(app, ["verify"])
    assert shown.exit_code == EXIT_OK and "status: pending" in shown.output
    assert "verified through checkpoint" in shown.output and "1 newer row (row seq 4) waits for the next" in shown.output
    raw = sqlite3.connect(tmp_path / "traces.db", isolation_level=None)
    raw.execute("DROP TRIGGER traces_append_only")
    raw.execute("UPDATE traces SET status = 'denied' WHERE seq = 2")
    raw.close()
    bad = runner.invoke(app, ["verify"])
    assert bad.exit_code == EXIT_TAMPERED and "row seq 2 was edited" in bad.output
    assert runner.invoke(app, ["checkpoint"]).exit_code == 0


# ---------------------------------------------------------------- retention marks (2026-11-04)

OLD = 100 * 86400


def _old_log(tmp_path):
    clock = Clock()
    s = TraceStore(tmp_path / "traces.db", clock=clock, checkpoint_rows=10_000)
    old = [s.write(TraceEvent(kind="tool_end", tool="t", lane="biz-a/x", status="ok", summary=f"old {i}",
                              at=clock.t - OLD)) for i in range(5)]
    return s, clock, old


def test_a_backwards_retention_mark_from_an_older_node_still_verifies(tmp_path):
    """Logs written before the fix: a purge after an unpin wrote "pruned through" below the mark
    before it. The verifier folds marks cumulatively, so that log is still lawful."""
    s, _, old = _old_log(tmp_path)
    try:
        s.pin(old[2].id, "mem")
        s.write(ev(10))
        s.checkpoint()
        s.purge(older_than_days=90)  # pruned through seq 5, kept seq 3
        third = s.row(old[2].id)
        with s._tx() as c:  # what the old purge wrote after the unpin
            c.execute("DELETE FROM traces WHERE seq = 3")
            s._add_mark(c, "pruned", {"through_seq": 3, "through_hash": third["hash"], "deleted": 1, "kept": [],
                                      "cutoff": "2027-01-01T00:00:00Z"})
        s.unpin("mem")
        assert s.verify().status == "ok"
        s.write(ev(11))  # the next row still follows seq 6
        assert s.row(next(r["id"] for r in s.chain_rows() if r["summary"] == "step 11"))["seq"] == 7
        s.checkpoint()
        assert s.verify().status == "ok"
    finally:
        s.close()


def test_an_unsigned_retention_mark_never_hides_a_row_a_signed_one_removed(tmp_path):
    """A signed mark said seq 1-5 are gone. A later unsigned mark (the key was lost) cannot make a
    row put back below seq 5 look lawful."""
    s, _, _ = _old_log(tmp_path)
    try:
        assert s.purge(older_than_days=90) == 5  # signed: pruned through seq 5, kept nothing
        s.write(TraceEvent(kind="tool_end", tool="t", lane="biz-a/x", status="ok", summary="old again",
                           at=s.clock() - OLD))
        s.write(ev(12))
        s.checkpoint()
        s.keys.forget(json.loads(s.marks()[0]["body"])["kid"])
        assert s.purge(older_than_days=90) == 1  # unsigned: pruned through seq 6
        assert s.verify().status == "unverifiable"
        raw = sqlite3.connect(tmp_path / "traces.db", isolation_level=None)
        raw.execute("DROP TRIGGER traces_append_only")
        raw.execute("INSERT INTO traces (seq, id, at, kind, status, summary) VALUES (2, 'tr_forged', 0, 'e', 'ok', "
                    "'approved the transfer')")
        raw.close()
        report = s.verify()
        assert report.status == "tampered"
        assert any("retention mark 4 cannot be trusted" in f.text for f in report.findings)
        assert any("row seq 2 is older than retention mark 2" in f.text for f in report.findings)
    finally:
        s.close()


def test_verify_since_a_window_that_starts_the_log(tmp_path):
    s = TraceStore(tmp_path / "traces.db", clock=Clock(), checkpoint_rows=10_000)
    try:
        for i in range(1, 4):
            s.write(ev(i))
        s.checkpoint()
        raw = sqlite3.connect(tmp_path / "traces.db", isolation_level=None)
        raw.execute("DROP TRIGGER traces_append_only")
        raw.execute("DELETE FROM traces WHERE seq = 1")
        raw.close()
        from agentos.policy.auditlog import Scope

        # no row before the window: the walk starts at the genesis, so the missing first row is seen
        assert s.verify(scope=Scope(since=0)).status == "tampered"
    finally:
        s.close()
