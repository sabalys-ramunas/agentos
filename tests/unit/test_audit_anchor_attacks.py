"""Adversarial tests for the audit anchors. Each test is one attack by someone with the node user: the
audit private key, the power to drop the append-only triggers, to edit rows and marks, and to delete or
recreate traces.db and secrets/. What they cannot change is what the controller holds: the anchors
collected through store.on_mark before the attack (held()).

Each test asserts what anchors.check() should say. A test that fails is a gap: the log no longer tells
the truth about anchored history and check() says nothing. Those stay failing on purpose. A test that
passes while the log is silent documents a limit. Every attack also prints what the node's own
store.verify() says for the same log (run pytest with -s to see the lines).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from test_policy_support import Clock

from openajar.policy import anchors, auditlog
from openajar.policy.auditlog import SIG_CTX, canonical, mark_hash, row_hash, sign
from openajar.policy.traces import TraceEvent, TraceStore
from openajar.policy.util import iso

COLS = (*auditlog.ROW_COLUMNS, "prev", "hash")
HOUR = 3600
DAY = 86400


# ---------------------------------------------------------------- the node and what its controller holds

def ev(i: int = 0, **kw) -> TraceEvent:
    return TraceEvent(**{"kind": "tool_end", "tool": "browser_act", "lane": "biz-a/instagram", "status": "ok",
                         "summary": f"step {i}", "args": {"i": i}, **kw})


@pytest.fixture
def clock():
    return Clock()


def new_store(path: Path, clock: Clock) -> TraceStore:
    # checkpoints only when a test asks for one: no timed checkpoint while the test moves the clock
    return TraceStore(path / "traces.db", clock=clock, checkpoint_rows=10_000, checkpoint_s=1e12)


@pytest.fixture
def store(tmp_path, clock):
    s = new_store(tmp_path, clock)
    yield s
    s.close()


def held(store: TraceStore) -> list[dict]:
    """What the controller holds: one anchor per checkpoint, collected through the hook."""
    out: list[dict] = []
    store.on_mark = lambda body: out.append(anchors.anchor_of(body))
    return out


def fill(store: TraceStore, clock: Clock, rows: int, start: int = 0):
    for i in range(start, start + rows):
        store.write(ev(i))
        clock.advance(1)
    return store.checkpoint()


def check(store: TraceStore, held_anchors: list[dict]):
    store.flush()
    return anchors.check(store.db.conn, held_anchors, lock=store.db.lock)


def verify_status(store: TraceStore) -> str:
    return store.verify().status


def report(name: str, alone: str, findings) -> None:
    shown = [(f.level, f.text[:110]) for f in findings] or "nothing"
    print(f"\n[{name}] verify alone: {alone}. check(): {shown}")


# ---------------------------------------------------------------- what the node user can do to the log

def unlock(store: TraceStore) -> None:
    """The append-only triggers are a guard against mistakes; the node user can drop them."""
    with store.db.lock:
        for t in ("traces_append_only", "audit_marks_no_update", "audit_marks_no_delete"):
            store.db.conn.execute(f"DROP TRIGGER IF EXISTS {t}")


def row(store: TraceStore, seq: int) -> dict:
    r = store.db.conn.execute(f"SELECT {', '.join(COLS)} FROM traces WHERE seq = ?", (seq,)).fetchone()
    return dict(zip(COLS, tuple(r), strict=True))


def rows(store: TraceStore, from_seq: int = 0) -> list[dict]:
    cur = store.db.conn.execute(f"SELECT {', '.join(COLS)} FROM traces WHERE seq >= ? ORDER BY seq", (from_seq,))
    return [dict(zip(COLS, tuple(r), strict=True)) for r in cur.fetchall()]


def hash_at(store: TraceStore, seq: int) -> str:
    return row(store, seq)["hash"]


def set_row(store: TraceStore, seq: int, **cols) -> None:
    """Edit a row in place. Its prev and hash stay as they were."""
    unlock(store)
    sets = ", ".join(f"{k} = ?" for k in cols)
    with store.db.lock:
        store.db.conn.execute(f"UPDATE traces SET {sets} WHERE seq = ?", (*cols.values(), seq))


def insert_row(store: TraceStore, **cols) -> None:
    unlock(store)
    names = ", ".join(cols)
    marks = ",".join(["?"] * len(cols))
    with store.db.lock:
        store.db.conn.execute(f"INSERT INTO traces ({names}) VALUES ({marks})", tuple(cols.values()))


def delete_rows(store: TraceStore, seqs) -> None:
    unlock(store)
    with store.db.lock:
        store.db.conn.executemany("DELETE FROM traces WHERE seq = ?", [(s,) for s in seqs])
    store._head_cache = None


def rechain(store: TraceStore, from_seq: int, prev: str | None = None) -> None:
    """Chain every row from from_seq on again, the way row_hash does. `prev` is what the first of them
    links to (default: the prev it already has)."""
    unlock(store)
    todo = rows(store, from_seq)
    link = prev if prev is not None else todo[0]["prev"]
    with store.db.lock:
        for r in todo:
            h = row_hash(link, r)
            store.db.conn.execute("UPDATE traces SET prev = ?, hash = ? WHERE seq = ?", (link, h, r["seq"]))
            link = h
    store._head_cache = None


def point_checkpoints(store: TraceStore) -> None:
    """Every checkpoint names the row at its seq again, so the marks agree with the rows."""
    unlock(store)
    with store.db.lock:
        conn = store.db.conn
        for n, kind, raw in conn.execute("SELECT n, kind, body FROM audit_marks").fetchall():
            body = json.loads(raw)
            head = conn.execute("SELECT hash FROM traces WHERE seq = ?", (body.get("seq"),)).fetchone()
            if kind == "checkpoint" and head is not None:
                conn.execute("UPDATE audit_marks SET body = ? WHERE n = ?", (json.dumps({**body, "hash": head[0]}), n))


def reseal(store: TraceStore, edits: dict | None = None, keys: dict | None = None) -> None:
    """Apply edits to marks (by n), then chain every mark again and sign each with the key its body names."""
    unlock(store)
    with store.db.lock:
        conn = store.db.conn
        prev = None
        for n, raw in conn.execute("SELECT n, body FROM audit_marks ORDER BY n").fetchall():
            body = {**json.loads(raw), **(edits or {}).get(n, {}), "prev": prev}
            data = canonical(body)
            key = (keys or {}).get(body["kid"]) or store.keys.private(body["kid"])
            conn.execute("UPDATE audit_marks SET body = ?, hash = ?, sig = ? WHERE n = ?",
                         (data.decode(), mark_hash(body), sign(key, SIG_CTX, data), n))
            prev = mark_hash(body)
    store._head_cache = None


def rewrite(store: TraceStore, seq: int, **cols) -> None:
    """Edit one row, chain the rows after it again, point the checkpoints at their new heads and sign every
    mark again. This is what verify() accepts once the node user holds the audit key."""
    set_row(store, seq, **cols)
    rechain(store, seq)
    point_checkpoints(store)
    reseal(store)


def rebuild(store: TraceStore, mark_edits: dict | None = None) -> None:
    """A whole log written again from its genesis, signed with the key the attacker holds."""
    reseal(store, mark_edits)
    genesis = store.db.conn.execute("SELECT hash FROM audit_marks WHERE n = 1").fetchone()[0]
    rechain(store, 0, prev=genesis)
    point_checkpoints(store)
    reseal(store)


def truncate(store: TraceStore, after_seq: int, after_mark: int, counter: int) -> None:
    """Delete the rows after after_seq and the marks after after_mark, and set SQLite's counter back."""
    unlock(store)
    with store.db.lock:
        c = store.db.conn
        c.execute("DELETE FROM traces WHERE seq > ?", (after_seq,))
        c.execute("DELETE FROM audit_marks WHERE n > ?", (after_mark,))
        c.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'traces'", (counter,))
    store._head_cache = None


def forge_prune(store: TraceStore, clock: Clock, through: int, through_hash: str, *, at="now") -> None:
    """A retention mark signed by hand, shaped the way purge() writes one. `at` is "now", a time string,
    None, or "absent" for a mark with no at field."""
    unlock(store)
    with store.db.lock:
        conn = store.db.conn
        n, prev = tuple(conn.execute("SELECT n, hash FROM audit_marks ORDER BY n DESC LIMIT 1").fetchone())
        kid, key = store._signing_key(conn)
        body = {"v": auditlog.FORMAT, "kind": "pruned", "chain": store._genesis["chain"], "n": n + 1, "prev": prev,
                "at": iso(clock.t) if at == "now" else at, "kid": kid, "through_seq": through,
                "through_hash": through_hash, "deleted": 0, "kept": [], "cutoff": iso(clock.t - 90 * DAY)}
        if at == "absent":
            del body["at"]
        data = canonical(body)
        conn.execute("INSERT INTO audit_marks (n, kind, body, hash, sig) VALUES (?, 'pruned', ?, ?, ?)",
                     (n + 1, data.decode(), mark_hash(body), sign(key, SIG_CTX, data)))
    store._head_cache = None


def mark_numbers(store: TraceStore) -> list[int]:
    return [int(n) for (n,) in store.db.conn.execute("SELECT n FROM audit_marks ORDER BY n").fetchall()]


def replay(store: TraceStore, original: list[dict]) -> None:
    """Write the original rows again, checkpointing where the original did (after seq 5 and after seq 10)."""
    for start, stop in ((0, 5), (5, 10)):
        for r in original[start:stop]:
            store.write(TraceEvent(id=r["id"], at=r["at"], kind=r["kind"], tool=r["tool"], lane=r["lane"],
                                   status=r["status"], summary=r["summary"],
                                   args=json.loads(r["args"]) if r["args"] is not None else None))
        store.checkpoint()


def wipe(path: Path) -> None:
    """delete traces.db and secrets/: the node forgets its log and its keys."""
    for p in path.glob("traces.db*"):
        p.unlink()
    shutil.rmtree(path / "secrets", ignore_errors=True)


def two_anchors(store: TraceStore, clock: Clock):
    """Checkpoint 2 at seq 5 and checkpoint 3 at seq 10, both held by the controller."""
    fill(store, clock, 5)
    fill(store, clock, 5, 5)


# ---------------------------------------------------------------- 1 to 3: rewriting, and what is not anchored

def test_01_rewrite_a_row_before_the_first_anchor_and_resign_everything_after(store, clock):
    a = held(store)
    two_anchors(store, clock)
    rewrite(store, 3, summary="nothing happened here")
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("01", alone, findings)
    assert any(f.level == "tampered" and "rewritten" in f.text for f in findings)


def test_02_rewrite_only_rows_after_the_newest_anchor(store, clock):
    a = held(store)
    two_anchors(store, clock)
    store.on_mark = None  # the controller was away for the next checkpoint
    fill(store, clock, 3, 10)  # checkpoint 4 at seq 13, not anchored
    rewrite(store, 12, summary="a quieter afternoon")
    alone = verify_status(store)
    findings, summary = check(store, a)
    report("02", alone, findings)
    assert not any(f.level == "tampered" for f in findings)
    assert summary["matched"] == 2
    assert [f.level for f in findings] == ["unprovable"] and "not anchored" in findings[0].text


def test_02b_rows_after_the_last_checkpoint_are_only_caught_by_verify(store, clock):
    # Rows written after the newest checkpoint, with no checkpoint naming them. check() only speaks of
    # checkpoints newer than the last anchor, so it stays silent; verify() flags the uncovered tail.
    a = held(store)
    two_anchors(store, clock)
    store.on_mark = None
    for i in range(10, 13):
        store.write(ev(i))
        clock.advance(1)
    rewrite(store, 12, summary="a quieter afternoon")
    alone = verify_status(store)
    findings, summary = check(store, a)
    report("02b", alone, findings)
    assert not any(f.level == "tampered" for f in findings)
    assert summary["matched"] == 2
    assert alone == "pending"  # not "ok": the rewritten rows are not sealed by any checkpoint


def test_03a_delete_the_unanchored_tail_with_its_marks_and_fix_the_counter(store, clock):
    a = held(store)
    two_anchors(store, clock)
    store.on_mark = None
    fill(store, clock, 3, 10)  # checkpoint 4 at seq 13, not anchored
    truncate(store, after_seq=10, after_mark=3, counter=10)
    alone = verify_status(store)
    findings, summary = check(store, a)
    report("03a", alone, findings)
    # The rows removed were never covered by an anchor, so no anchor can say they existed. Nothing
    # tampered with the anchored history; this is a limit, not a lie about anchored rows.
    assert not any(f.level == "tampered" for f in findings)
    assert summary["matched"] == 2


def test_03b_delete_rows_into_the_anchored_range_and_the_newest_checkpoint_mark(store, clock):
    a = held(store)
    two_anchors(store, clock)
    truncate(store, after_seq=7, after_mark=2, counter=7)  # takes seq 8-10 and checkpoint 3 with them
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("03b", alone, findings)
    assert any(f.level == "tampered" and "cut back" in f.text for f in findings)


def test_03c_delete_rows_into_the_anchored_range_and_keep_the_checkpoint_mark(store, clock):
    a = held(store)
    two_anchors(store, clock)
    truncate(store, after_seq=7, after_mark=3, counter=7)  # the mark still names seq 10
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("03c", alone, findings)
    assert any(f.level == "tampered" and "deleted from the end" in f.text for f in findings)


# ---------------------------------------------------------------- 4: a forged retention mark

def test_04a_forged_prune_through_an_anchored_seq_hours_after_the_anchor(store, clock):
    a = held(store)
    fill(store, clock, 10)  # one checkpoint, at seq 10
    clock.advance(HOUR)
    delete_rows(store, range(1, 11))
    forge_prune(store, clock, 10, a[0]["hash"])
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("04a", alone, findings)
    assert [f.level for f in findings] == ["unprovable"] and "removed to hide them" in findings[0].text


def test_04b_forged_prune_dated_29_hours_ahead_so_the_gap_looks_lawful(store, clock):
    a = held(store)
    fill(store, clock, 10)
    clock.advance(HOUR)
    delete_rows(store, range(1, 11))
    # the node's clock says it is one hour after the anchor; the mark claims a time 29 hours ahead of now
    forge_prune(store, clock, 10, a[0]["hash"], at=iso(clock.t + 29 * HOUR))
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("04b", alone, findings)
    assert any(f.level == "unprovable" and "removed" in f.text for f in findings)


@pytest.mark.parametrize("at", [None, "absent", "not a time"])
def test_04c_forged_prune_with_no_usable_time_skips_the_gap_check(store, clock, at):
    a = held(store)
    fill(store, clock, 10)
    clock.advance(HOUR)
    delete_rows(store, range(1, 11))
    forge_prune(store, clock, 10, a[0]["hash"], at=at)
    alone = verify_status(store)
    findings, _ = check(store, a)
    report(f"04c {at!r}", alone, findings)
    assert any(f.level == "unprovable" and "removed" in f.text for f in findings)


def test_04d_forged_prune_through_one_before_the_anchored_seq(store, clock):
    # The anchor at seq 10 covers rows 1-9 through its hash chain. Those rows go; the anchored row stays,
    # so the anchor is "present and matching" and the too-young check never looks at the removal.
    a = held(store)
    fill(store, clock, 10)
    clock.advance(HOUR)
    through_hash = hash_at(store, 9)
    delete_rows(store, range(1, 10))
    forge_prune(store, clock, 9, through_hash)
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("04d", alone, findings)
    assert any(f.level == "unprovable" and "removed to hide them" in f.text for f in findings)


# ---------------------------------------------------------------- 5 and 8: swaps, edits and insertions

def test_05a_swap_two_unanchored_rows_between_two_anchors_keeping_every_hash(store, clock):
    a = held(store)
    two_anchors(store, clock)
    x, y = row(store, 6), row(store, 9)
    set_row(store, 6, summary=y["summary"], args=y["args"])
    set_row(store, 9, summary=x["summary"], args=x["args"])
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("05a", alone, findings)
    # verify --anchors runs both: the chain walk catches what the anchor check alone does not
    assert store.verify().status == "tampered" or any(f.level == "tampered" for f in findings)


def test_05b_edit_an_anchored_row_and_keep_its_hash(store, clock):
    a = held(store)
    two_anchors(store, clock)
    set_row(store, 5, summary="nothing happened here")  # the anchored row at seq 5, its hash untouched
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("05b", alone, findings)
    # verify --anchors runs both: the chain walk catches what the anchor check alone does not
    assert store.verify().status == "tampered" or any(f.level == "tampered" for f in findings)


def test_08a_insert_rows_between_anchors_and_renumber_the_rest(store, clock):
    a = held(store)
    two_anchors(store, clock)
    unlock(store)
    with store.db.lock:
        for s in range(10, 5, -1):  # shift seq 6-10 up by two, highest first
            store.db.conn.execute("UPDATE traces SET seq = ? WHERE seq = ?", (s + 2, s))
    template = row(store, 5)
    for s in (6, 7):
        insert_row(store, **{**template, "seq": s, "id": f"tr_forged_{s}", "summary": "inserted", "args": None})
    rechain(store, 6, prev=hash_at(store, 5))
    reseal(store, {3: {"seq": 12, "count": 12, "hash": hash_at(store, 12)}})
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("08a", alone, findings)
    assert any(f.level == "tampered" for f in findings)


def test_08b_replace_the_rows_between_anchors_and_leave_the_anchored_rows_alone(store, clock):
    a = held(store)
    two_anchors(store, clock)
    template = row(store, 5)
    delete_rows(store, range(6, 10))
    prev = hash_at(store, 5)
    for s in range(6, 10):
        r = {**template, "seq": s, "id": f"tr_forged_{s}", "summary": f"forged {s}", "args": None,
             "prev": prev, "hash": None}
        r["hash"] = row_hash(prev, r)
        insert_row(store, **r)
        prev = r["hash"]
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("08b", alone, findings)
    # verify --anchors runs both: the chain walk catches what the anchor check alone does not
    assert store.verify().status == "tampered" or any(f.level == "tampered" for f in findings)


# ---------------------------------------------------------------- 6: starting a new chain

def test_06a_new_chain_from_scratch_replaying_the_original_rows(tmp_path, clock):
    s1 = new_store(tmp_path, clock)
    a = held(s1)
    two_anchors(s1, clock)
    anchors_held, original = list(a), rows(s1)
    s1.close()
    wipe(tmp_path)
    s2 = new_store(tmp_path, clock)
    replay(s2, original)
    alone = s2.verify().status
    findings, _ = check(s2, anchors_held)
    s2.close()
    report("06a", alone, findings)
    assert any(f.level == "tampered" and "reset" in f.text for f in findings)


def test_06b_same_chain_id_with_a_new_genesis_and_the_rows_rebuilt_on_it(tmp_path, clock):
    s1 = new_store(tmp_path, clock)
    a = held(s1)
    two_anchors(s1, clock)
    anchors_held, original, chain = list(a), rows(s1), s1._genesis["chain"]
    s1.close()
    wipe(tmp_path)
    s2 = new_store(tmp_path, clock)
    replay(s2, original)
    rebuild(s2, {n: {"chain": chain} for n in mark_numbers(s2)})  # the same chain id, as the anchors name it
    alone = s2.verify().status
    findings, _ = check(s2, anchors_held)
    s2.close()
    report("06b", alone, findings)
    assert any(f.level == "tampered" and "rewritten" in f.text for f in findings)


# ---------------------------------------------------------------- 7: a checkpoint that was anchored

def test_07a_anchored_checkpoint_rewritten_to_name_another_seq(store, clock):
    a = held(store)
    two_anchors(store, clock)
    reseal(store, {3: {"seq": 9, "count": 9, "hash": hash_at(store, 9)}})
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("07a", alone, findings)
    assert any(f.level == "tampered" for f in findings)


def test_07b_anchored_checkpoint_rewritten_to_name_the_new_key_after_a_reset(store, clock):
    a = held(store)
    fill(store, clock, 5)  # checkpoint 2 at seq 5, signed by K0
    k0_kid, k0 = store._signing_key(store.db.conn)
    store.rotate_audit_key(reset=True)  # K1; K0's private file is deleted
    k1_kid, k1 = store._signing_key(store.db.conn)
    fill(store, clock, 5, 5)  # checkpoint 4 at seq 10, signed by K1
    reseal(store, {2: {"kid": k1_kid}}, keys={k0_kid: k0, k1_kid: k1})
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("07b", alone, findings)
    assert any(f.level == "tampered" for f in findings)


@pytest.mark.parametrize("field", ["at", "chain", "count"])
def test_07c_anchored_checkpoint_field_the_anchor_does_not_hold_is_rewritten(store, clock, field):
    # The anchor holds chain, n, seq, hash, kid and at. check() compares seq, hash and kid, and never looks at
    # the rest of the mark. A rewritten at or chain goes unseen; verify() flags a changed count.
    a = held(store)
    two_anchors(store, clock)
    value = {"at": iso(clock.t - 3 * DAY), "chain": "ch_other", "count": 9}[field]
    reseal(store, {2: {field: value}})  # checkpoint 2 at seq 5
    alone = verify_status(store)
    findings, _ = check(store, a)
    report(f"07c {field}", alone, findings)
    # verify --anchors runs both: the chain walk catches what the anchor check alone does not
    assert store.verify().status == "tampered" or any(f.level == "tampered" for f in findings)


# ---------------------------------------------------------------- 9: anything else

def test_09b_delete_unanchored_rows_in_the_middle_without_chaining_again(store, clock):
    a = held(store)
    two_anchors(store, clock)
    delete_rows(store, [7, 8])  # the anchored heads at seq 5 and 10 keep their stored hashes
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("09b", alone, findings)
    # verify --anchors runs both: the chain walk catches what the anchor check alone does not
    assert store.verify().status == "tampered" or any(f.level == "tampered" for f in findings)


# ---------------------------------------------------------------- 9: retention marks are anchored too
# Case b is named test_09b2_ because test_09b_ (delete unanchored rows in the middle) already exists above.

def held_timed(store: TraceStore, clock: Clock) -> list[dict]:
    """What the controller holds when it stamps each anchor on arrival: every checkpoint and retention mark
    is received at the moment it is written."""
    out: list[dict] = []

    def take(body: dict) -> None:
        if body.get("kind") in anchors.KINDS:
            out.append({**anchors.anchor_of(body), "received_at": iso(clock.t)})

    store.on_mark = take
    return out


def mark_body(store: TraceStore, n: int) -> dict:
    with store.db.lock:
        (raw,) = store.db.conn.execute("SELECT body FROM audit_marks WHERE n = ?", (n,)).fetchone()
    return json.loads(raw)


def aged_log(store: TraceStore, clock: Clock) -> None:
    """Five rows and a checkpoint, then 100 days later five more rows and a checkpoint. A 90-day
    retention removes the first five."""
    fill(store, clock, 5)
    clock.advance(100 * DAY)
    fill(store, clock, 5, 5)


def test_09a_a_forged_retention_mark_sent_an_hour_later_is_dated_by_the_controller(store, clock):
    a = held_timed(store, clock)
    fill(store, clock, 10)  # checkpoint 2 at seq 10, anchored when written
    clock.advance(HOUR)
    delete_rows(store, range(1, 11))
    forge_prune(store, clock, 10, a[0]["hash"], at=iso(clock.t + 29 * HOUR))
    forged = mark_body(store, max(mark_numbers(store)))
    a.append({**anchors.anchor_of(forged), "received_at": iso(clock.t)})  # the service sent it an hour later
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("09a", alone, findings)
    assert [f.level for f in findings] == ["unprovable"] and "removed to hide them" in findings[0].text


def test_09b2_a_later_checkpoint_received_on_time_dates_a_retention_mark_held_back(store, clock):
    a = held_timed(store, clock)
    fill(store, clock, 10)  # checkpoint 2 at seq 10
    clock.advance(HOUR)
    delete_rows(store, range(1, 11))
    forge_prune(store, clock, 10, a[0]["hash"], at=iso(clock.t + 29 * HOUR))  # never sent to the controller
    clock.advance(5 * 60)
    fill(store, clock, 3, 10)  # checkpoint 4 at seq 13, a few minutes later, and it is anchored
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("09b2", alone, findings)
    assert [f.level for f in findings] == ["unprovable"] and "removed to hide them" in findings[0].text


def test_09c_lawful_retention_with_controller_times_matches_every_anchor(store, clock):
    a = held_timed(store, clock)
    aged_log(store, clock)
    assert store.purge(older_than_days=90) == 5
    alone = verify_status(store)
    findings, summary = check(store, a)
    report("09c", alone, findings)
    assert findings == [] and summary["matched"] == 3 and summary["retention_anchored"] == 1


def test_09d_an_anchored_retention_mark_rewritten_to_prune_further_is_tampered(store, clock):
    a = held_timed(store, clock)
    aged_log(store, clock)
    assert store.purge(older_than_days=90) == 5
    mark = max(mark_numbers(store))
    through_10 = hash_at(store, 10)
    delete_rows(store, range(6, 11))  # rows 6 to 10 go too, and the mark is signed to say so
    reseal(store, {mark: {"through_seq": 10, "through_hash": through_10}})
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("09d", alone, findings)
    assert any(f.level == "tampered" and f.mark == mark for f in findings)


def test_09e_an_anchored_retention_mark_deleted_is_a_log_cut_back(store, clock):
    a = held_timed(store, clock)
    aged_log(store, clock)
    assert store.purge(older_than_days=90) == 5
    newest = max(mark_numbers(store))
    unlock(store)
    with store.db.lock:
        store.db.conn.execute("DELETE FROM audit_marks WHERE n = ?", (newest,))
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("09e", alone, findings)
    assert any(f.level == "tampered" and "cut back" in f.text for f in findings)


def test_09f_a_retention_mark_never_anchored_while_a_later_checkpoint_was(store, clock):
    a = held_timed(store, clock)
    aged_log(store, clock)
    hook = store.on_mark
    store.on_mark = None  # the controller misses the retention mark
    assert store.purge(older_than_days=90) == 5
    store.on_mark = hook
    fill(store, clock, 3, 10)  # checkpoint 5 at seq 13, anchored
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("09f", alone, findings)
    assert [f.level for f in findings] == ["unprovable"] and "never anchored" in findings[0].text


def test_09g_a_controller_older_than_retention_anchors_keeps_the_head_not_the_kind(store, clock):
    a = held_timed(store, clock)
    aged_log(store, clock)
    assert store.purge(older_than_days=90) == 5
    assert a[-1]["kind"] == "pruned"
    a[-1] = {k: v for k, v in a[-1].items() if k != "kind"}  # the retention anchor as an older controller stores it
    alone = verify_status(store)
    findings, summary = check(store, a)
    report("09g", alone, findings)
    # it cannot have been a checkpoint (checkpoint 2 already reached past its seq), so it is only a note
    assert [f.level for f in findings] == ["note"] and "older than retention anchors" in findings[0].text
    assert summary["matched"] == 3 and summary["retention_anchored"] == 1


def test_09h_a_checkpoint_swapped_for_a_retention_mark_with_the_same_head_is_tampered(store, clock):
    a = held_timed(store, clock)
    fill(store, clock, 5)  # checkpoint 2 at seq 5: the controller holds it, and a checkpoint anchor has no kind
    fill(store, clock, 5, 5)  # checkpoint 3 at seq 10
    head = hash_at(store, 5)
    delete_rows(store, range(1, 6))  # the rows up to the swapped mark go
    unlock(store)
    with store.db.lock:
        store.db.conn.execute("UPDATE audit_marks SET kind = 'pruned' WHERE n = 2")
    reseal(store, {2: {"kind": "pruned", "through_seq": 5, "through_hash": head}})
    alone = verify_status(store)
    findings, _ = check(store, a)
    report("09h", alone, findings)
    assert any(f.level == "tampered" for f in findings)


# ---------------------------------------------------------------- 10: what the second round of attacks found

def test_10a_young_rows_no_checkpoint_ever_covered_are_not_lawful_retention(store, clock):
    # no checkpoint before the retention mark, so no anchored head ever covered the rows it removed
    a = held_timed(store, clock)
    for i in range(10):
        store.write(ev(i))
        clock.advance(1)
    store.flush()
    clock.advance(HOUR)
    h10 = hash_at(store, 10)
    delete_rows(store, range(1, 11))
    forge_prune(store, clock, 10, h10)
    a.append({**anchors.anchor_of(mark_body(store, max(mark_numbers(store)))), "received_at": iso(clock.t)})
    findings, _ = check(store, a)
    report("10a", verify_status(store), findings)
    assert [f.level for f in findings] == ["unprovable"] and "no anchored checkpoint before it" in findings[0].text


def test_10b_retention_older_than_every_anchor_held_is_not_judged(store, clock):
    # an older controller hands back only its newest heads: a lawful retention mark below them is left alone
    a = held_timed(store, clock)
    aged_log(store, clock)
    assert store.purge(older_than_days=90) == 5
    clock.advance(DAY)
    fill(store, clock, 3, 10)
    window = [x for x in a if x["n"] > max(mark_numbers(store)) - 1]  # only the newest checkpoint
    findings, _ = check(store, window)
    report("10b", verify_status(store), findings)
    assert not any("retention mark" in f.text for f in findings)


def test_10c_a_retention_mark_of_rows_from_before_the_chain_needs_no_anchor():
    # purge writes through_hash null when it removed only rows from before the chain: no anchor can name it
    marks = {2: {"_kind": "checkpoint", "seq": 5, "hash": "sha256:" + "a" * 64, "chain": "c", "n": 2},
             3: {"_kind": "pruned", "n": 3, "chain": "c", "through_seq": 3, "through_hash": None}}
    held_cp = [{"chain": "c", "n": 2, "seq": 5, "hash": "sha256:" + "a" * 64, "at": None}]
    assert anchors._retention(marks, "c", held_cp, held_cp, {}, set()) == []


def test_10d_a_kindless_anchor_that_could_be_a_checkpoint_is_still_tampered_when_swapped(store, clock):
    # 09h with an earlier checkpoint mark below the swapped seq: still nothing shows it cannot be a checkpoint
    a = held_timed(store, clock)
    fill(store, clock, 3)   # checkpoint 2 at seq 3
    fill(store, clock, 3, 3)  # checkpoint 3 at seq 6
    head = hash_at(store, 6)
    delete_rows(store, range(1, 7))
    unlock(store)
    with store.db.lock:
        store.db.conn.execute("UPDATE audit_marks SET kind = 'pruned' WHERE n = 3")
    reseal(store, {3: {"kind": "pruned", "through_seq": 6, "through_hash": head}})
    findings, _ = check(store, a)
    report("10d", verify_status(store), findings)
    assert any(f.level == "tampered" for f in findings)
