"""The versioned lane format (openajar.node.lanefmt): the v1 stamp changes nothing, a newer lane is
refused, warm lanes are never touched, steps back up what they change and survive an interrupt."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_lanes import make

from openajar.core.models import LaneSpec, LaneState
from openajar.node import lanefmt
from openajar.node.lanefmt import LaneMigration, LaneMigrationContext
from openajar.node.lanes import LaneFormatMismatch
from openajar.node.store import LaneStore

LANE = "personal/reddit"


def lane_copy(home: Path, lane_id: str = LANE) -> Path:
    """A copy of a production-shaped lane: a profile, a v2 pin, the traces, files and its rows."""
    ws, name = lane_id.split("/")
    d = home / "lanes" / ws / name
    (d / "profile" / "storage" / "default").mkdir(parents=True)
    (d / "profile" / "prefs.js").write_text('user_pref("browser.startup.page", 3);\n')
    (d / "profile" / "cookies.sqlite").write_bytes(b"SQLite format 3\x00" + os.urandom(64))
    (d / "profile" / "storage" / "default" / "ls.bin").write_bytes(os.urandom(32))
    (d / "fingerprint.json").write_text(json.dumps({"navigator.userAgent": "Mozilla/5.0 Firefox/158.0",
                                                    "agentos:pin": {"version": 2}}))
    (d / "fingerprint.json.pin-v1.bak").write_text("{}")
    (d / "egress-trace.jsonl").write_text('{"host": "reddit.com"}\n')
    (d / "files" / "inbox").mkdir(parents=True)
    (d / "files" / "inbox" / "photo.jpg").write_bytes(os.urandom(16))
    (d / "shots").mkdir()
    store = LaneStore(home / "openajar.db")
    store.add(LaneSpec(id=lane_id))
    store.add_event(lane_id, "fingerprint_completed", {"keys": 3})
    return d


def tree_hash(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.name in (lanefmt.FORMAT_NAME, lanefmt.LOCK_NAME):
            continue
        rel = str(p.relative_to(root))
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
    return out


def rows(home: Path) -> list:
    conn = sqlite3.connect(home / "openajar.db")
    try:
        return conn.execute("SELECT * FROM lanes ORDER BY id").fetchall()
    finally:
        conn.close()


# ---- the v1 baseline -------------------------------------------------------------------

def test_an_existing_lane_is_stamped_v1_with_no_change(tmp_path):
    d = lane_copy(tmp_path)
    before, before_rows = tree_hash(d), rows(tmp_path)
    [st] = lanefmt.lane_status(tmp_path)
    assert (st.current, st.stamped, st.latest) == (1, False, lanefmt.LATEST)
    assert st.pending and st.in_use is None
    result = lanefmt.migrate_lanes(tmp_path)
    assert result.applied == [(LANE, 1, lanefmt.LANE_MIGRATIONS[0].name)]
    assert tree_hash(d) == before and rows(tmp_path) == before_rows
    data = json.loads((d / lanefmt.FORMAT_NAME).read_text())
    assert data["version"] == 1 and data["history"][0]["version"] == 1
    assert (d / lanefmt.FORMAT_NAME).stat().st_mode & 0o777 == 0o600
    # the backup says the lane had no version file, so a rollback removes it
    assert (result.backup_dir / LANE / lanefmt.ABSENT_MARK).exists()
    assert result.backup_dir.stat().st_mode & 0o777 == 0o700
    # idempotent: a second run does nothing and changes nothing
    again = lanefmt.migrate_lanes(tmp_path)
    assert again.applied == [] and again.backup_dir is None
    assert lanefmt.lane_status(tmp_path)[0].pending == []
    assert lanefmt.check_openable(d) is None


def test_dry_run_changes_nothing(tmp_path):
    d = lane_copy(tmp_path)
    result = lanefmt.migrate_lanes(tmp_path, dry_run=True)
    assert result.applied == [] and result.statuses[0].pending
    assert not (d / lanefmt.FORMAT_NAME).exists() and not lanefmt.backup_root(tmp_path).exists()


def test_rollback_removes_the_stamp(tmp_path):
    d = lane_copy(tmp_path)
    result = lanefmt.migrate_lanes(tmp_path)
    assert lanefmt.restore_lanes(result.backup_dir, tmp_path) == [LANE]
    assert not (d / lanefmt.FORMAT_NAME).exists()


# ---- refusing ----------------------------------------------------------------------------

def test_a_newer_lane_stops_the_run_and_the_daemon(tmp_path):
    lane_copy(tmp_path, "a/old")
    newer = lane_copy(tmp_path, "b/new")
    (newer / lanefmt.FORMAT_NAME).write_text(json.dumps({"version": lanefmt.LATEST + 1}))
    with pytest.raises(lanefmt.NewerLaneFormat, match="b/new"):
        lanefmt.migrate_lanes(tmp_path)
    assert not (tmp_path / "lanes" / "a" / "old" / lanefmt.FORMAT_NAME).exists()  # nothing was changed
    assert "newer openajar" in lanefmt.check_openable(newer)


def test_a_broken_version_file_is_never_guessed(tmp_path):
    d = lane_copy(tmp_path)
    (d / lanefmt.FORMAT_NAME).write_text("{not json")
    assert lanefmt.lane_status(tmp_path)[0].error
    with pytest.raises(lanefmt.LaneFormatError, match="unreadable"):
        lanefmt.migrate_lanes(tmp_path)
    assert "broken" in lanefmt.check_openable(d)


def test_warm_lanes_are_left_alone(tmp_path):
    held = lane_copy(tmp_path, "a/held")
    running = lane_copy(tmp_path, "a/running")
    cold = lane_copy(tmp_path, "a/cold")
    lock = lanefmt.LaneLock(held)  # what agentosd holds while a lane is warm
    assert lock.acquire(exclusive=False)
    try:
        result = lanefmt.migrate_lanes(
            tmp_path, browser_pids=lambda p: [4242] if p == running / "profile" else [])
    finally:
        lock.release()
    assert [lane for lane, *_ in result.applied] == ["a/cold"]
    assert dict(result.skipped) == {"a/held": "agentosd has it warm", "a/running": "a browser (pid 4242) runs it"}
    assert not (held / lanefmt.FORMAT_NAME).exists() and not (running / lanefmt.FORMAT_NAME).exists()
    assert (cold / lanefmt.FORMAT_NAME).exists()
    # the next run, once they are cold, stamps them
    assert len(lanefmt.migrate_lanes(tmp_path).applied) == 2


def test_a_held_profile_lock_counts_as_in_use(tmp_path):
    d = lane_copy(tmp_path)
    os.symlink(f"127.0.0.1:+{os.getpid()}", d / "profile" / "lock")  # a live browser's lock (ours)
    result = lanefmt.migrate_lanes(tmp_path)
    assert result.applied == [] and result.skipped == [(LANE, "a browser holds its profile lock")]


# ---- a later step: backups, interrupts, idempotence ------------------------------------

def _v2(fail_after_write: list[bool]):
    def up(ctx: LaneMigrationContext) -> None:
        ctx.backup_file("egress-trace.jsonl")
        ctx.backup_rows("lanes")
        trace = ctx.path / "egress-trace.jsonl"
        if not trace.read_text().startswith("#v2"):  # idempotent: a rerun does not add it twice
            trace.write_text("#v2\n" + trace.read_text())
        if fail_after_write and fail_after_write.pop():
            raise KeyboardInterrupt  # the run is cut short part way
        with ctx.db() as conn:
            conn.execute("UPDATE lanes SET last_used = 'v2' WHERE id = ?", (ctx.lane_id,))
    return up


def test_a_step_backs_up_what_it_changes_and_survives_an_interrupt(tmp_path):
    d = lane_copy(tmp_path)
    lanefmt.migrate_lanes(tmp_path)  # stamped v1
    original = (d / "egress-trace.jsonl").read_text()
    steps = (*lanefmt.LANE_MIGRATIONS, LaneMigration(2, "trace header", _v2([True])))
    with pytest.raises(lanefmt.LaneFormatError, match="stays at format 1") as err:
        lanefmt.migrate_lanes(tmp_path, migrations=steps)
    assert lanefmt.lane_version(d) == (1, True)
    backup = err.value.backup_dir / LANE
    assert (backup / "egress-trace.jsonl").read_text() == original
    assert json.loads((backup / "openajar.db" / "lanes.jsonl").read_text())["id"] == LANE
    result = lanefmt.migrate_lanes(tmp_path, migrations=steps)  # the rerun finishes it, once
    assert result.applied == [(LANE, 2, "trace header")]
    assert (d / "egress-trace.jsonl").read_text() == "#v2\n" + original
    assert rows(tmp_path)[0][2] == "v2"
    assert lanefmt.lane_version(d) == (2, True)
    # the rollback puts the file, the row and the version file back
    lanefmt.restore_lanes(result.backup_dir, tmp_path)
    assert (d / "egress-trace.jsonl").read_text() == "#v2\n" + original  # this run's backup: after the first try
    assert rows(tmp_path)[0][2] is None
    assert lanefmt.lane_version(d) == (1, True)


def test_migration_versions_must_be_in_order():
    with pytest.raises(ValueError):
        lanefmt.lane_status(Path("/nonexistent"), migrations=(LaneMigration(2, "x", lambda c: None),))


# ---- the daemon --------------------------------------------------------------------------

async def test_daemon_stamps_new_lanes_and_holds_the_lock_while_warm(tmp_path):
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    d = mgr.config.lane_dir("a/one")
    assert lanefmt.lane_version(d) == (lanefmt.LATEST, True)
    await mgr.open("a/one")
    probe = lanefmt.LaneLock(d)
    assert not probe.acquire(exclusive=True)  # a migrator keeps off a warm lane
    assert lanefmt.migrate_lanes(tmp_path).applied == []
    await mgr.close("a/one")
    assert probe.acquire(exclusive=True)
    probe.release()
    await mgr.shutdown()


async def test_daemon_refuses_a_newer_lane(tmp_path):
    mgr, engine, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    lanefmt.format_path(mgr.config.lane_dir("a/one")).write_text(json.dumps({"version": lanefmt.LATEST + 1}))
    with pytest.raises(LaneFormatMismatch, match="newer openajar"):
        await mgr.open("a/one")
    assert engine.launches == [] and mgr.get("a/one").state == LaneState.COLD
    await mgr.shutdown()


async def test_daemon_opens_an_unstamped_v1_lane(tmp_path):
    lane_copy(tmp_path)
    mgr, _engine, *_ = make(tmp_path)
    assert (await mgr.open(LANE)).state == LaneState.WARM
    assert not lanefmt.format_path(mgr.config.lane_dir(LANE)).exists()  # the daemon never migrates
    await mgr.shutdown()


async def test_daemon_waits_out_a_migration_then_refuses(tmp_path, monkeypatch):
    mgr, _engine, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    lock = lanefmt.LaneLock(mgr.config.lane_dir("a/one"))
    assert lock.acquire(exclusive=True)
    real = lanefmt.LaneLock.acquire
    monkeypatch.setattr(lanefmt.LaneLock, "acquire", lambda self, **kw: real(self, **{**kw, "wait": 0.1}))
    try:
        with pytest.raises(LaneFormatMismatch, match="migrating"):
            await mgr.open("a/one")
    finally:
        lock.release()
    assert mgr.get("a/one").state == LaneState.COLD
    assert (await mgr.open("a/one")).state == LaneState.WARM
    await mgr.shutdown()


def test_doctor_reports_lane_formats(tmp_path):
    from openajar.core import migrate
    from openajar.ops import doctor

    d = lane_copy(tmp_path)
    ctx = doctor.DoctorContext(home=tmp_path)
    migrate.migrate(tmp_path, backup=False)
    lane_check = doctor.check_migrations(ctx)[1]
    assert lane_check.id == "lane_format" and lane_check.status == doctor.PASS and "not stamped" in lane_check.detail
    lanefmt.migrate_lanes(tmp_path)
    assert "not stamped" not in doctor.check_migrations(ctx)[1].detail
    lanefmt.format_path(d).write_text(json.dumps({"version": 99}))
    assert doctor.check_migrations(ctx)[1].status == doctor.FAIL
