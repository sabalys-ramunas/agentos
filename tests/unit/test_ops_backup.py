"""Backups: create, verify, restore, retention, and every way an archive can be bad."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_ops_support import PASSPHRASE, PROXY_PASS, TOKEN, FakeHost, make_home

from agentos.core import age
from agentos.ops import backup
from agentos.ops.backup import ArchiveError, BackupRefused

LOG_N = 10
T0 = datetime(2026, 11, 4, 3, 0, 0, tzinfo=UTC)


def create(cfg, dest, **kw):
    kw.setdefault("host", FakeHost())
    kw.setdefault("log_n", LOG_N)
    kw.setdefault("now", T0)
    return backup.create_backup(cfg.home, dest, PASSPHRASE, node=cfg.name, **kw)


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class FakeCoordinator:
    def __init__(self, host: FakeHost, warm: set[str] = frozenset(), available: bool = True):
        self.host, self.warm_set, self.up = host, set(warm), available
        self.calls: list[tuple[str, str]] = []

    def available(self) -> bool:
        return self.up

    def warm(self) -> set[str]:
        return set(self.warm_set)

    def close(self, lane_id: str) -> None:
        self.calls.append(("close", lane_id))
        for key in list(self.host.browsers):
            if f"/lanes/{lane_id}/" in key + "/":
                self.host.browsers.pop(key)

    def open(self, lane_id: str) -> None:
        self.calls.append(("open", lane_id))


def test_round_trip_restores_every_kept_file(tmp_path):
    cfg = make_home(tmp_path)
    home = cfg.home
    (home / "logs").mkdir()
    (home / "logs" / "display.log").write_text("log")
    (home / "run").mkdir()
    (home / ".cache" / "camoufox").mkdir(parents=True)
    (home / ".cache" / "camoufox" / "browser.bin").write_bytes(b"big browser")
    shots = cfg.lane_dir("personal/reddit") / "shots"
    shots.mkdir()
    (shots / "a.png").write_bytes(b"png")
    os.symlink("127.0.0.1:+123", cfg.profile_dir("personal/reddit") / "lock")
    (home / "secrets" / "vault.key").write_text("KEY")
    (home / "secrets" / "vault.key").chmod(0o600)

    result = create(cfg, tmp_path / "out")
    assert result.path.name == "agentos-test-node-20261104T030000Z.tar.gz.age"
    assert result.path.stat().st_mode & 0o777 == 0o600
    assert [lane["copied"] for lane in result.lanes] == ["cold", "cold"]
    assert not list((tmp_path / "out").glob(".partial-*")) and not list((tmp_path / "out").glob(".staging-*"))
    assert [r["location"] for r in backup.read_made(home)] == [str(result.path)]  # where status --check looks

    restored = tmp_path / "restored"
    r = backup.restore_backup(result.path, PASSPHRASE, restored, host=FakeHost())
    assert r.lanes == ["personal/reddit", "work/site"] and r.permission_problems == []
    got = tree(restored)
    want = {k: v for k, v in tree(home).items()
            if not k.startswith(("logs/", ".cache/", "backups/")) and "/cache2/" not in k and "/shots/" not in k}
    assert set(got) == set(want)
    for k, v in want.items():
        if not k.endswith(".db"):
            assert got[k] == v, k
    assert got["secrets/vault.key"] == b"KEY"
    assert not (restored / "run").exists()
    assert not (restored / "lanes/personal/reddit/profile/lock").exists()
    assert (restored / "lanes/personal/reddit/profile/storage/default/ls.sqlite").exists()


def test_sqlite_is_copied_consistently_while_open(tmp_path):
    cfg = make_home(tmp_path)
    writer = sqlite3.connect(cfg.home / "agentos.db")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE IF NOT EXISTS notes (x TEXT)")
    writer.executemany("INSERT INTO notes VALUES (?)", [(f"n{i}",) for i in range(500)])
    writer.commit()  # committed into the WAL, not checkpointed into the main file
    assert (cfg.home / "agentos.db-wal").stat().st_size > 0
    try:
        result = create(cfg, tmp_path / "out")
    finally:
        writer.close()
    contents = backup.verify_backup(result.path, PASSPHRASE)
    assert "agentos.db" in contents.sqlite_checked
    restored = tmp_path / "restored"
    backup.restore_backup(result.path, PASSPHRASE, restored, host=FakeHost())
    assert not (restored / "agentos.db-wal").exists()
    with sqlite3.connect(restored / "agentos.db") as db:
        assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 500
        assert db.execute("SELECT COUNT(*) FROM lanes").fetchone()[0] == 2


def test_a_database_left_by_a_killed_browser_does_not_stop_the_backup(tmp_path):
    """A killed browser leaves -wal and -shm beside its database. Opening the database for the
    snapshot folds them in and removes them, so they are gone when the walk reaches their names."""
    cfg = make_home(tmp_path / "src")
    live = tmp_path / "live.sqlite"
    db = sqlite3.connect(live)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t (v TEXT)")
    db.execute("INSERT INTO t VALUES ('kept')")
    db.commit()
    left = cfg.home / "trash" / "scratch" / "profile"
    left.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm"):  # what the disk held when the process died
        (left / f"places.sqlite{suffix}").write_bytes(Path(f"{live}{suffix}").read_bytes())
    db.close()
    (left / "z-after.txt").write_text("after the database in the walk")

    result = create(cfg, tmp_path / "out")

    target = tmp_path / "restored"
    backup.restore_backup(result.path, PASSPHRASE, target, host=FakeHost())
    restored = target / "trash" / "scratch" / "profile"
    assert sqlite3.connect(restored / "places.sqlite").execute("SELECT v FROM t").fetchall() == [("kept",)]
    assert (restored / "z-after.txt").read_text() == "after the database in the walk"


def test_permissions_after_restore_are_private(tmp_path):
    cfg = make_home(tmp_path)
    (cfg.home / "extra").mkdir(mode=0o755)
    (cfg.home / "extra" / "open.txt").write_text("x")
    (cfg.home / "extra" / "open.txt").chmod(0o644)
    result = create(cfg, tmp_path / "out")
    restored = tmp_path / "restored"
    backup.restore_backup(result.path, PASSPHRASE, restored, host=FakeHost())
    for p in [restored, *restored.rglob("*")]:
        assert p.stat().st_mode & 0o777 == (0o700 if p.is_dir() else 0o600), p


def test_manifest_and_header(tmp_path):
    cfg = make_home(tmp_path)
    result = create(cfg, tmp_path / "out")
    contents = backup.verify_backup(result.path, PASSPHRASE)
    assert contents.header["format"] == "agentos-backup" and contents.header["node"] == "test-node"
    files = {f["path"]: f for f in contents.manifest["files"]}
    assert files["agentos.db"]["kind"] == "sqlite"
    assert "lanes/personal/reddit/fingerprint.json" in files
    assert "cache2" in contents.manifest["excluded"]["profile"]
    assert contents.files == result.files


def test_secrets_stay_inside_the_encryption(tmp_path):
    cfg = make_home(tmp_path)
    result = create(cfg, tmp_path / "out")
    blob = result.path.read_bytes()
    for secret in (TOKEN.encode(), PROXY_PASS.encode(), b"cookie-db-", b"personal/reddit"):
        assert secret not in blob


def test_wrong_passphrase_leaves_nothing(tmp_path):
    cfg = make_home(tmp_path)
    result = create(cfg, tmp_path / "out")
    target = tmp_path / "restored"
    with pytest.raises(ArchiveError, match="wrong passphrase"):
        backup.restore_backup(result.path, "a different passphrase", target, host=FakeHost())
    assert not target.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".restored.restore-")]
    with pytest.raises(ArchiveError, match="wrong passphrase"):
        backup.verify_backup(result.path, "a different passphrase")


def test_corrupted_archive_is_refused_and_target_untouched(tmp_path):
    cfg = make_home(tmp_path)
    result = create(cfg, tmp_path / "out")
    data = bytearray(result.path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    bad = tmp_path / "bad.tar.gz.age"
    bad.write_bytes(bytes(data))
    with pytest.raises(ArchiveError, match="damaged"):
        backup.verify_backup(bad, PASSPHRASE)
    target = tmp_path / "restored"
    target.mkdir()
    with pytest.raises(ArchiveError):
        backup.restore_backup(bad, PASSPHRASE, target, host=FakeHost())
    assert list(target.iterdir()) == []
    assert not [p for p in tmp_path.iterdir() if ".restore-" in p.name]


def test_truncated_archive(tmp_path):
    cfg = make_home(tmp_path)
    (cfg.home / "big.bin").write_bytes(os.urandom(400_000))  # several 64 KiB chunks
    result = create(cfg, tmp_path / "out")
    data = result.path.read_bytes()
    header_end = data.index(b"\n", data.index(b"\n---") + 1) + 1
    chunks = (len(data) - header_end - 16) // (age.CHUNK + age.TAG)
    cut = header_end + 16 + chunks * (age.CHUNK + age.TAG)
    short = tmp_path / "short.tar.gz.age"
    short.write_bytes(data[:cut])
    with pytest.raises(ArchiveError, match="truncated"):
        backup.verify_backup(short, PASSPHRASE)
    short.write_bytes(data[: len(data) // 2])
    with pytest.raises(ArchiveError):
        backup.verify_backup(short, PASSPHRASE)


def test_interrupted_backup_leaves_no_archive(tmp_path):
    cfg = make_home(tmp_path)
    out = tmp_path / "out"

    def stop(rel: str) -> None:
        if rel.endswith("cookies.sqlite"):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        create(cfg, out, fail_after=stop)
    assert list(out.iterdir()) == []
    assert backup.list_archives(out) == []


def test_partial_files_never_count_and_old_ones_are_pruned(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    partial = out / f"{backup.PARTIAL_PREFIX}agentos-n-20261101T000000Z.tar.gz.age"
    partial.write_bytes(b"half")
    assert backup.list_archives(out) == []
    old = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(partial, (old, old))
    removed = backup.prune(out, 3, "n")
    assert partial in removed and not partial.exists()


def test_retention_keeps_newest_of_this_node(tmp_path):
    cfg = make_home(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    other = out / "agentos-other-node-20200101T000000Z.tar.gz.age"
    other.write_bytes(b"not ours")
    unrelated = out / "notes.txt"
    unrelated.write_text("keep")
    paths = [create(cfg, out, now=T0 + timedelta(hours=i), keep=2).path for i in range(3)]
    left = backup.list_archives(out, "test-node")
    assert [a.path for a in left] == paths[1:]
    assert other.exists() and unrelated.exists()
    assert [a.node for a in backup.list_archives(out)] == ["other-node", "test-node", "test-node"]


def test_refusals(tmp_path):
    cfg = make_home(tmp_path)
    with pytest.raises(BackupRefused, match="at least"):
        backup.create_backup(cfg.home, tmp_path / "o", "short", node="n", host=FakeHost())
    with pytest.raises(BackupRefused, match="inside AGENTOS_HOME"):
        create(cfg, cfg.home / "lanes" / "x")
    host = FakeHost()
    host.default_disk = (10, 10, 1)
    with pytest.raises(BackupRefused, match="not enough space"):
        create(cfg, tmp_path / "o", host=host)
    with pytest.raises(BackupRefused, match="no node.toml"):
        backup.create_backup(tmp_path, tmp_path / "o", PASSPHRASE, node="n", host=FakeHost())


def test_default_dir_inside_home_is_allowed_and_excluded(tmp_path):
    cfg = make_home(tmp_path)
    first = create(cfg, cfg.home / "backups")
    second = create(cfg, cfg.home / "backups", now=T0 + timedelta(minutes=1))
    names = {f["path"] for f in backup.verify_backup(second.path, PASSPHRASE).manifest["files"]}
    assert not any(n.startswith("backups/") for n in names)
    assert first.path.exists() and (cfg.home / "backups").stat().st_mode & 0o777 == 0o700


def test_warm_lane_without_daemon_is_refused_or_copied_live(tmp_path):
    cfg = make_home(tmp_path)
    host = FakeHost()
    host.browsers[str(cfg.profile_dir("personal/reddit"))] = [4242]
    with pytest.raises(BackupRefused, match="personal/reddit is warm"):
        create(cfg, tmp_path / "out", host=host)
    assert backup.list_archives(tmp_path / "out") == []
    result = create(cfg, tmp_path / "out", host=host, allow_live=True)
    lanes = {lane["id"]: lane["copied"] for lane in result.lanes}
    assert lanes == {"personal/reddit": "live", "work/site": "cold"}
    assert any("live" in w for w in result.warnings)


def test_warm_lane_is_paused_through_the_daemon_and_warmed_again(tmp_path):
    cfg = make_home(tmp_path)
    host = FakeHost()
    host.browsers[str(cfg.profile_dir("personal/reddit"))] = [4242]
    coord = FakeCoordinator(host, warm={"personal/reddit"})
    result = create(cfg, tmp_path / "out", host=host, coordinator=coord)
    assert coord.calls == [("close", "personal/reddit"), ("open", "personal/reddit")]
    assert {lane["id"]: lane["copied"] for lane in result.lanes}["personal/reddit"] == "paused"


class FlakyHost(FakeHost):
    """The browser comes back once while the lane is being copied (an agent warmed it)."""

    def __init__(self, profile: Path):
        super().__init__()
        self.profile = str(profile)
        self.seen = 0

    def browser_pids(self, profile: Path) -> list[int]:
        if str(profile) != self.profile:
            return []
        self.seen += 1
        return [7] if self.seen == 2 else []


def test_browser_that_starts_during_the_copy_means_a_second_copy(tmp_path):
    cfg = make_home(tmp_path)
    host = FlakyHost(cfg.profile_dir("personal/reddit"))
    coord = FakeCoordinator(host)
    result = create(cfg, tmp_path / "out", host=host, coordinator=coord)
    assert coord.calls == [("close", "personal/reddit")]  # it was cold before, so not warmed again
    assert {lane["id"]: lane["copied"] for lane in result.lanes}["personal/reddit"] == "paused"
    host = FlakyHost(cfg.profile_dir("personal/reddit"))
    with pytest.raises(BackupRefused, match="started while it was being copied"):
        create(cfg, tmp_path / "out2", host=host)


def test_restore_refuses_a_non_empty_home_unless_forced(tmp_path):
    cfg = make_home(tmp_path)
    result = create(cfg, tmp_path / "out")
    target = tmp_path / "target"
    target.mkdir()
    (target / "old.txt").write_text("old")
    with pytest.raises(BackupRefused, match="not empty"):
        backup.restore_backup(result.path, PASSPHRASE, target, host=FakeHost())
    r = backup.restore_backup(result.path, PASSPHRASE, target, force=True, host=FakeHost(), now=T0)
    assert r.moved_aside == tmp_path / "target.before-restore-20261104T030000Z"
    assert (r.moved_aside / "old.txt").read_text() == "old"
    assert (target / "node.toml").exists() and not (target / "old.txt").exists()


def test_restore_refuses_while_a_daemon_runs_on_the_home(tmp_path):
    cfg = make_home(tmp_path)
    result = create(cfg, tmp_path / "out")
    host = FakeHost()
    host.sockets["ops.sock"] = {"ok": True}
    with pytest.raises(BackupRefused, match="stop agentosd"):
        backup.restore_backup(result.path, PASSPHRASE, cfg.home, force=True, host=host)


def test_restore_owner_needs_root(tmp_path):
    cfg = make_home(tmp_path)
    result = create(cfg, tmp_path / "out")
    host = FakeHost()
    host.euid_value = 1000
    with pytest.raises(BackupRefused, match="needs root"):
        backup.restore_backup(result.path, PASSPHRASE, tmp_path / "t", owner="agentos", host=host)


def test_restore_by_a_user_who_cannot_write_the_parent_says_so_without_a_traceback(tmp_path):
    """QA 2026-11-10 finding 35: the service user restoring its own home in /var/lib."""
    cfg = make_home(tmp_path)
    result = create(cfg, tmp_path / "out")
    parent = tmp_path / "varlib"
    parent.mkdir()
    (parent / "agentos").mkdir()
    parent.chmod(0o555)
    try:
        with pytest.raises(BackupRefused, match="Run it as root") as e:
            backup.restore_backup(result.path, PASSPHRASE, parent / "agentos", host=FakeHost())
        assert "sudo agentos backup restore" in e.value.message and "—" not in e.value.message
        assert sorted(p.name for p in parent.iterdir()) == ["agentos"]  # nothing was staged
    finally:
        parent.chmod(0o755)


def test_the_unit_user_is_read_only_from_a_unit_for_this_home(tmp_path):
    unit = tmp_path / "agentosd.service"
    unit.write_text("[Service]\nUser=agentos\nEnvironment=AGENTOS_HOME=/var/lib/agentos\n")
    assert backup.unit_user(Path("/var/lib/agentos"), unit) == "agentos"
    assert backup.unit_user(Path("/srv/other"), unit) is None
    unit.write_text("[Service]\nEnvironment=AGENTOS_HOME=/var/lib/agentos\n")
    assert backup.unit_user(Path("/var/lib/agentos"), unit) is None  # root
    assert backup.unit_user(Path("/var/lib/agentos"), tmp_path / "missing") is None


# ---- hand-made archives: restore must reject what create would never write -------------

def craft(path: Path, members: list[tuple[str, bytes | None, bytes]], *, manifest: dict | None = None) -> None:
    """members: (name, data or None for a directory, tar type)."""
    files = []
    with open(path, "wb") as raw:
        enc = age.Encryptor(raw, PASSPHRASE, log_n=LOG_N)
        with tarfile.open(fileobj=enc, mode="w|gz") as tar:
            def add(name: str, data: bytes | None, kind: bytes = tarfile.REGTYPE, link: str = "") -> None:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.linkname = link
                info.size = len(data) if data is not None and kind == tarfile.REGTYPE else 0
                tar.addfile(info, io.BytesIO(data) if info.size else None)

            add("agentos-backup/format.json", json.dumps({"format": "agentos-backup", "version": 1}).encode())
            for name, data, kind in members:
                add(name, data, kind, link="../../etc/passwd" if kind == tarfile.SYMTYPE else "")
                if kind == tarfile.REGTYPE and data is not None:
                    import hashlib
                    files.append({"path": name.removeprefix("home/"), "size": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()})
            add("agentos-backup/manifest.json", json.dumps(manifest or {"files": files, "lanes": []}).encode())
        enc.close()


@pytest.mark.parametrize("name,kind,match", [
    ("home/../evil", tarfile.REGTYPE, "unsafe path"),
    ("/etc/passwd", tarfile.REGTYPE, "unexpected member"),
    ("home/link", tarfile.SYMTYPE, "not a regular file"),
    ("home/fifo", tarfile.FIFOTYPE, "not a regular file"),
])
def test_restore_rejects_unsafe_members(tmp_path, name, kind, match):
    archive = tmp_path / "evil.tar.gz.age"
    craft(archive, [(name, b"x", kind)])
    with pytest.raises(ArchiveError, match=match):
        backup.restore_backup(archive, PASSPHRASE, tmp_path / "t", host=FakeHost())
    assert not (tmp_path / "t").exists() and not (tmp_path / "evil").exists()


def test_restore_rejects_a_checksum_mismatch_and_a_missing_manifest(tmp_path):
    archive = tmp_path / "a.tar.gz.age"
    craft(archive, [("home/node.toml", b"name = 'x'\n", tarfile.REGTYPE)],
          manifest={"files": [{"path": "node.toml", "size": 11, "sha256": "0" * 64}]})
    with pytest.raises(ArchiveError, match="checksum mismatch"):
        backup.restore_backup(archive, PASSPHRASE, tmp_path / "t", host=FakeHost())
    craft(archive, [("home/node.toml", b"x", tarfile.REGTYPE)],
          manifest={"files": [{"path": "node.toml", "size": 1, "sha256": "0" * 64},
                              {"path": "secrets/gone", "size": 1, "sha256": "0" * 64}]})
    with pytest.raises(ArchiveError, match="does not match"):
        backup.verify_backup(archive, PASSPHRASE)


def test_verify_checks_database_integrity(tmp_path):
    archive = tmp_path / "a.tar.gz.age"
    broken = b"SQLite format 3\x00" + b"\x00" * 84 + os.urandom(4000)
    craft(archive, [("home/agentos.db", broken, tarfile.REGTYPE)])
    with pytest.raises(ArchiveError, match="integrity"):
        backup.verify_backup(archive, PASSPHRASE)


def test_not_an_archive(tmp_path):
    p = tmp_path / "x.tar.gz.age"
    p.write_bytes(b"hello")
    with pytest.raises(ArchiveError, match="not an encrypted agentos backup"):
        backup.verify_backup(p, PASSPHRASE)
    with pytest.raises(ArchiveError, match="cannot open"):
        backup.verify_backup(tmp_path / "missing", PASSPHRASE)
