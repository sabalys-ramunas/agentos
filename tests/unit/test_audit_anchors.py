"""Audit anchors: a log rewritten and signed again with the node's own key passes `verify`, and
fails `verify --anchors`; so does a truncated log and a reset one. Also the write path's health,
which the fail-closed rule reads."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3

import pytest
from test_policy_support import Clock

from openajar.core import signing
from openajar.policy import anchors, auditlog
from openajar.policy.auditlog import SIG_CTX, canonical, mark_hash, row_hash, sign
from openajar.policy.traces import TraceEvent, TraceStore


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


def held(store: TraceStore) -> list[dict]:
    """What the controller would hold: one anchor per checkpoint, collected through the hook."""
    out: list[dict] = []
    store.on_mark = lambda body: out.append(anchors.anchor_of(body))
    return out


def fill(store: TraceStore, clock: Clock, rows: int, start: int = 0) -> dict:
    for i in range(start, start + rows):
        store.write(ev(i))
        clock.advance(1)
    return store.checkpoint()


def check(store: TraceStore, held_anchors: list[dict]):
    store.flush()
    return anchors.check(store.db.conn, held_anchors, lock=store.db.lock)


def unlock(store: TraceStore) -> None:
    """The append-only triggers are only a guard against mistakes: the node user can drop them."""
    with store.db.lock:
        for t in ("traces_append_only", "audit_marks_no_update", "audit_marks_no_delete"):
            store.db.conn.execute(f"DROP TRIGGER IF EXISTS {t}")
        store.db.conn.commit()


def rewrite_and_resign(store: TraceStore, seq: int, summary: str) -> None:
    """What someone with the node user does: edit a row, chain every later row again, and sign
    every later checkpoint again with the node's own key. `verify` then has nothing to say."""
    unlock(store)
    conn = store.db.conn
    cols = ", ".join((*auditlog.ROW_COLUMNS, "prev", "hash"))
    with store.db.lock:
        conn.execute("UPDATE traces SET summary = ? WHERE seq = ?", (summary, seq))
        rows = [dict(zip((*auditlog.ROW_COLUMNS, "prev", "hash"), r, strict=True))
                for r in conn.execute(f"SELECT {cols} FROM traces WHERE seq >= ? ORDER BY seq", (seq,))]
        prev = rows[0]["prev"]
        hashes = {}
        for r in rows:
            h = row_hash(prev, r)
            conn.execute("UPDATE traces SET prev = ?, hash = ? WHERE seq = ?", (prev, h, r["seq"]))
            hashes[r["seq"]] = h
            prev = h
        marks = conn.execute("SELECT n, kind, body FROM audit_marks ORDER BY n").fetchall()
        mprev = None
        for n, kind, raw in marks:
            body = json.loads(raw)
            body["prev"] = mprev
            if kind == "checkpoint" and body["seq"] in hashes:
                body["hash"] = hashes[body["seq"]]
            key = store.keys.private(body.get("kid") if kind != "key" else None) or \
                store.keys.private(store._signing_key(conn)[0])
            data = canonical(body)
            conn.execute("UPDATE audit_marks SET body = ?, hash = ?, sig = ? WHERE n = ?",
                         (data.decode(), mark_hash(body), sign(key, SIG_CTX, data), n))
            mprev = mark_hash(body)
        conn.commit()
    store._head_cache = None


# ---------------------------------------------------------------- the check

def test_an_untouched_log_matches_every_anchor(store, clock):
    a = held(store)
    fill(store, clock, 5)
    fill(store, clock, 5, 5)
    findings, summary = check(store, a)
    assert findings == []
    assert summary["matched"] == 2 and summary["anchored_through"]["seq"] == 10


def test_a_log_rewritten_and_signed_again_passes_verify_and_fails_the_anchors(store, clock):
    a = held(store)
    fill(store, clock, 5)
    fill(store, clock, 5, 5)
    rewrite_and_resign(store, 3, "nothing happened here")
    assert store.verify().status == "ok"  # the node's own key vouches for the new history
    findings, _ = check(store, a)
    assert findings and all(f.level == "tampered" for f in findings)
    assert any("rewritten" in f.text for f in findings)


def test_a_log_cut_back_after_it_was_anchored_is_named(store, clock):
    a = held(store)
    fill(store, clock, 5)
    fill(store, clock, 5, 5)
    unlock(store)
    with store.db.lock:
        store.db.conn.execute("DELETE FROM traces WHERE seq > 5")
        store.db.conn.execute("DELETE FROM audit_marks WHERE n > 2")
        store.db.conn.execute("UPDATE sqlite_sequence SET seq = 5 WHERE name = 'traces'")
        store.db.conn.commit()
    store._head_cache = None
    assert store.verify().status == "ok"  # nothing on the node says the end is gone
    findings, _ = check(store, a)
    assert [f.level for f in findings] == ["tampered"]
    assert "cut back" in findings[0].text


def test_rows_deleted_from_the_end_with_the_marks_kept_are_named(store, clock):
    a = held(store)
    fill(store, clock, 5)
    unlock(store)
    with store.db.lock:
        store.db.conn.execute("DELETE FROM traces WHERE seq > 3")
        store.db.conn.commit()
    store._head_cache = None
    findings, _ = check(store, a)
    assert any("deleted from the end" in f.text for f in findings)


def test_a_log_reset_and_started_again_is_named(tmp_path, clock):
    s = TraceStore(tmp_path / "traces.db", clock=clock, checkpoint_rows=10_000)
    a = held(s)
    fill(s, clock, 5)
    s.close()
    for p in tmp_path.glob("traces.db*"):
        p.unlink()
    shutil.rmtree(tmp_path / "secrets")  # the key and the trust file go too: nothing on the node remembers
    s2 = TraceStore(tmp_path / "traces.db", clock=clock, checkpoint_rows=10_000)
    fill(s2, clock, 3)
    assert s2.verify().status == "ok"
    findings, summary = check(s2, a)
    assert any(f.level == "tampered" and "reset" in f.text for f in findings)
    assert summary["matched"] == 0
    s2.close()


def test_checkpoints_not_anchored_yet_are_unprovable_not_tampered(store, clock):
    a = held(store)
    fill(store, clock, 5)
    store.on_mark = None  # the controller was away for this one
    fill(store, clock, 5, 5)
    findings, summary = check(store, a)
    assert [f.level for f in findings] == ["unprovable"]
    assert "not anchored" in findings[0].text and summary["matched"] == 1


def test_lawful_retention_still_matches_the_anchors(store, clock):
    a = held(store)
    fill(store, clock, 5)
    clock.advance(100 * 86400)
    fill(store, clock, 5, 5)
    assert store.purge(older_than_days=90) == 5
    findings, summary = check(store, a)
    assert findings == [] and summary["matched"] == len(a) >= 2


def test_rows_pruned_hours_after_they_were_anchored_are_flagged(store, clock):
    a = held(store)
    fill(store, clock, 5)
    clock.advance(3600)
    fill(store, clock, 5, 5)
    assert store.purge(older_than_days=0) > 0  # what someone with the node's key would sign
    findings, _ = check(store, a)
    assert findings and findings[0].level == "unprovable" and "removed to hide them" in findings[0].text


def test_malformed_anchors_are_skipped():
    good = "sha256:" + "a" * 64
    assert anchors.valid({"chain": "ch_x", "n": 2, "seq": 5, "hash": good, "kid": "k"}) is None
    assert anchors.valid({"chain": "ch_x", "n": 2, "seq": 5, "hash": "a" * 64})
    assert anchors.valid({"chain": "ch_x", "n": True, "seq": 5, "hash": good})
    assert anchors.valid("nope")


# ---------------------------------------------------------------- the write path's health

def test_a_short_lock_is_a_blip_and_a_long_one_goes_to_the_spill_file(tmp_path, clock):
    s = TraceStore(tmp_path / "traces.db", clock=clock, busy_timeout_ms=1)
    other = sqlite3.connect(tmp_path / "traces.db", timeout=0)
    other.execute("BEGIN IMMEDIATE")
    s.write(ev(0))
    assert s.db_failing_since == clock.t and s.unwritten_since == clock.t  # in memory only, for now
    clock.advance(3)
    s.write(ev(1))
    assert s.spill_path.exists() and s.unwritten_since is None  # on disk: the trail is written
    assert s.db_failing_since is not None
    other.execute("ROLLBACK")
    other.close()
    s.flush()
    assert s.db_failing_since is None and s.unwritten_since is None
    assert [json.loads(r["args"])["i"] for r in s.chain_rows()] == [0, 1]
    s.close()


def test_when_the_spill_file_fails_too_the_clock_keeps_running(tmp_path, clock, monkeypatch):
    s = TraceStore(tmp_path / "traces.db", clock=clock, busy_timeout_ms=1)
    other = sqlite3.connect(tmp_path / "traces.db", timeout=0)
    other.execute("BEGIN IMMEDIATE")

    def full(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "open", full)
    s.write(ev(0))
    started = s.unwritten_since
    clock.advance(30)
    s.write(ev(1))  # never raises
    assert s.unwritten_since == started and s.pending_count == 2
    assert "spill" in (s.last_write_error or "")
    monkeypatch.undo()
    other.execute("ROLLBACK")
    other.close()
    assert s.flush() == 0 and s.unwritten_since is None
    s.close()


# ---------------------------------------------------------------- the CLI

NODE = "node_01ANCHORCLI0000000000000000"


def export_doc(tmp_path, held_anchors: list[dict]) -> dict:
    """What `openajar controller anchors NODE -o` writes: each head with a controller receipt."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from openajar.core.config import NodeConfig, save_config

    key = Ed25519PrivateKey.generate()
    save_config(NodeConfig(home=tmp_path, controller_url="ws://localhost:1/v1/node/connect", node_id=NODE,
                           controller_pubkey=signing.public_b64(key), audit_anchors=[]))
    receipts = [signing.sign_controller(key, anchors.RECEIPT_KIND, NODE,
                                     {"anchor": anchors.core(a), "received_at": "2026-11-07T00:00:00Z"})
                for a in held_anchors]
    return {"node": NODE, "receipts": receipts}


def test_cli_verify_with_an_anchors_file(tmp_path, monkeypatch, clock):
    from typer.testing import CliRunner

    from openajar.audit.cli import app
    from openajar.policy.auditlog import EXIT_OK, EXIT_TAMPERED, EXIT_UNVERIFIABLE

    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))
    s = TraceStore(tmp_path / "traces.db", secrets_dir=tmp_path / "secrets", clock=clock, checkpoint_rows=10_000)
    a = held(s)
    fill(s, clock, 4)
    fill(s, clock, 4, 4)
    doc = export_doc(tmp_path, a)
    path = tmp_path / "anchors.json"
    path.write_text(json.dumps(doc))
    runner = CliRunner()
    ok = runner.invoke(app, ["verify", "--anchors-file", str(path)])
    assert ok.exit_code == EXIT_OK, ok.output
    assert "anchors: 2 of 2 match" in ok.output

    forged = {**doc, "receipts": [{**r, "sig": doc["receipts"][0]["sig"]} for r in doc["receipts"]]}
    path.write_text(json.dumps(forged))  # the second receipt now carries the first one's signature
    out = runner.invoke(app, ["verify", "--anchors-file", str(path), "--json"])
    assert out.exit_code == EXIT_UNVERIFIABLE, out.output
    assert json.loads(out.output)["anchors"]["matched"] == 1

    path.write_text(json.dumps(doc))
    rewrite_and_resign(s, 2, "a quieter afternoon")
    s.close()
    assert runner.invoke(app, ["verify"]).exit_code == EXIT_OK  # the node's own key vouches for it
    bad = runner.invoke(app, ["verify", "--anchors-file", str(path)])
    assert bad.exit_code == EXIT_TAMPERED and "status: tampered" in bad.output and "rewritten" in bad.output


def test_cli_verify_anchors_on_a_node_that_anchors_nowhere(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from openajar.audit.cli import app
    from openajar.core.config import NodeConfig, save_config

    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))
    save_config(NodeConfig(home=tmp_path, audit_anchors=[]))
    TraceStore(tmp_path / "traces.db", secrets_dir=tmp_path / "secrets").close()
    out = CliRunner().invoke(app, ["verify", "--anchors"])
    assert out.exit_code == 1 and "audit_anchors" in out.output


def test_cli_verify_anchors_refuses_since_and_until(tmp_path, monkeypatch, clock):
    from typer.testing import CliRunner

    from openajar.audit.cli import app

    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))
    s = TraceStore(tmp_path / "traces.db", secrets_dir=tmp_path / "secrets", clock=clock, checkpoint_rows=10_000)
    a = held(s)
    fill(s, clock, 4)
    path = tmp_path / "anchors.json"
    path.write_text(json.dumps(export_doc(tmp_path, a)))
    s.close()
    for opt in ("--since", "--until"):
        out = CliRunner().invoke(app, ["verify", "--anchors-file", str(path), opt, "2026-01-01"])
        assert out.exit_code == 1 and "whole log" in out.output


def test_cli_receipts_need_a_controller_key_and_bare_anchors_never_make_ok(tmp_path, monkeypatch, clock):
    from typer.testing import CliRunner

    from openajar.audit.cli import app
    from openajar.core.config import load_config, save_config
    from openajar.policy.auditlog import EXIT_OK, EXIT_UNVERIFIABLE

    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))
    s = TraceStore(tmp_path / "traces.db", secrets_dir=tmp_path / "secrets", clock=clock, checkpoint_rows=10_000)
    a = held(s)
    fill(s, clock, 4)
    fill(s, clock, 4, 4)
    s.close()
    doc = export_doc(tmp_path, a)
    path = tmp_path / "anchors.json"
    path.write_text(json.dumps(doc))
    cfg = load_config(tmp_path)
    pub = cfg.controller_pubkey
    runner = CliRunner()
    assert runner.invoke(app, ["verify", "--anchors-file", str(path)]).exit_code == EXIT_OK

    save_config(cfg.model_copy(update={"controller_pubkey": ""}))  # the node user blanks the pin
    blank = runner.invoke(app, ["verify", "--anchors-file", str(path)])
    assert blank.exit_code == EXIT_UNVERIFIABLE and "--controller-key" in blank.output
    # the auditor brings the controller's key: the receipts verify again
    assert runner.invoke(app, ["verify", "--anchors-file", str(path), "--controller-key", pub]).exit_code == EXIT_OK

    path.write_text(json.dumps({"anchors": [anchors.core(x) for x in a]}))  # a bare list: no signatures
    bare = runner.invoke(app, ["verify", "--anchors-file", str(path), "--controller-key", pub])
    assert bare.exit_code == EXIT_UNVERIFIABLE and "no controller signature" in bare.output
