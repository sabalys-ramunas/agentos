"""The channel's SQLite file (openajar.node.channel.store) as other code sees it: the rows each call
writes, a file an older build wrote, and the file's mode. Pinned before the store moved onto
openajar.core.sqlite, so the move changes no table, no stored value and no file."""

from __future__ import annotations

import json
import sqlite3
import threading

from openajar.node.channel.store import DB_NAME, SCHEMA, ChannelStore


def rows(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_every_call_writes_the_rows_it_always_wrote(tmp_path):
    path = tmp_path / DB_NAME
    store = ChannelStore(path, clock=lambda: 1000.5)
    seq = store.enqueue("event", {"kind": "x"})
    store.set_meta("controller_config", {"heartbeat_seconds": 15})
    store.put_call("c1", "running", tool="whoami")
    store.put_call("c1", "done", result={"ok": True})
    store.put_waiting("c2", "browser_act", "d1g3st")
    store.set_approval_state("ap_1", "pending")
    store.close()

    assert seq == 1
    meta = dict(rows(path, "SELECT key, value FROM meta"))
    assert meta["next_seq"] == "2" and json.loads(meta["controller_config"]) == {"heartbeat_seconds": 15}
    assert json.loads(meta["epoch"]).startswith("ep")
    [(out_seq, kind, frame, created)] = rows(path, "SELECT seq, type, frame, created_at FROM outbox")
    assert (out_seq, kind, created) == (1, "event", 1000.5)
    sent = json.loads(frame)  # the exact frame it sends, so a redelivery keeps its id and seq
    assert (sent["type"], sent["seq"], sent["body"]) == ("event", 1, {"kind": "x"}) and sent["id"].startswith("m_")
    assert rows(path, "SELECT call_id, state, tool, result, approval_id, updated_at FROM calls") == [
        ("c1", "done", "whoami", '{"ok": true}', None, 1000.5)]
    assert rows(path, "SELECT call_id, tool, args_digest, at FROM waiting") == [("c2", "browser_act", "d1g3st", 1000.5)]
    assert rows(path, "SELECT id, state, at FROM approvals") == [("ap_1", "pending", 1000.5)]
    assert rows(path, "PRAGMA journal_mode") == [("wal",)]
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_file_an_older_build_wrote_opens_and_keeps_its_counter(tmp_path):
    path = tmp_path / DB_NAME
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO meta (key, value) VALUES ('next_seq', '41'), ('epoch', '\"ep_old\"')")
    conn.execute("INSERT INTO outbox (seq, type, frame, created_at) VALUES (40, 'traces', '{}', 1.0)")
    conn.execute("INSERT INTO calls (call_id, state, tool, result, approval_id, updated_at) "
                 "VALUES ('c9', 'done', 't', '{\"a\": 1}', 'ap', 2.0)")
    conn.commit()
    conn.close()
    path.chmod(0o644)

    store = ChannelStore(path)
    assert store.epoch == "ep_old" and store.high_seq == 40 and store.oldest_seq() == 40
    assert store.outbox_after(0) == [(40, "{}")]
    call = store.get_call("c9")
    assert (call.state, call.tool, call.result, call.approval_id, call.updated_at) == ("done", "t", {"a": 1}, "ap", 2.0)
    assert store.enqueue("event", {}) == 41
    store.close()
    assert path.stat().st_mode & 0o777 == 0o600  # every open makes it the owner's alone


def test_seq_stays_one_counter_across_threads(tmp_path):
    store = ChannelStore(tmp_path / DB_NAME)
    got: list[int] = []
    lock = threading.Lock()

    def put() -> None:
        for _ in range(25):
            seq = store.enqueue("event", {})
            with lock:
                got.append(seq)

    threads = [threading.Thread(target=put) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == list(range(1, 101)) and store.depth() == 100
    store.close()
