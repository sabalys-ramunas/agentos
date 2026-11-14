"""Migrations: ordering, idempotency, the pre-migration backup, failures and newer schemas."""

from __future__ import annotations

import sqlite3
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentos.ops import layout, migrate
from agentos.ops.migrate import Migration, MigrationError, MigrationSet, NewerSchema


def calls_set(log: list[str], *, fail_at: int | None = None, db: str = "thing.db", create: bool = True):
    def step(n):
        def up(conn):
            log.append(f"v{n}")
            conn.execute(f"CREATE TABLE t{n} (x)")
            if n == fail_at:
                conn.execute(f"INSERT INTO t{n} VALUES (1)")
                raise RuntimeError("boom")
        return up

    return MigrationSet(package="thing", db=db, create=create,
                        migrations=tuple(Migration(n, f"step {n}", step(n)) for n in (1, 2, 3)))


def tables(path) -> set[str]:
    with sqlite3.connect(path) as db:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def as_root(monkeypatch):
    """What root gave away, on a home that is someone else's. Nothing here really runs as root."""
    given: list[Path] = []
    monkeypatch.setattr(layout.os, "geteuid", lambda: 0)
    monkeypatch.setattr(layout.os, "chown", lambda path, *a, **kw: given.append(Path(path)))
    return given


def test_root_gives_away_each_directory_it_makes_for_a_nested_database(tmp_path, as_root):
    migrate.migrate(tmp_path, [calls_set([], db="deep/er/thing.db")], backup=False)
    assert {tmp_path / "deep", tmp_path / "deep/er", tmp_path / "deep/er/thing.db"} <= set(as_root)


def test_root_leaves_a_directory_that_was_already_there(tmp_path, as_root):
    (tmp_path / "deep").mkdir()
    migrate.migrate(tmp_path, [calls_set([], db="deep/er/thing.db")], backup=False)
    assert tmp_path / "deep/er" in as_root and tmp_path / "deep" not in as_root


def test_versions_must_be_contiguous_and_names_unique():
    noop = lambda conn: None
    with pytest.raises(ValueError, match="1..n in order"):
        MigrationSet("p", "p.db", (Migration(1, "a", noop), Migration(3, "c", noop)))
    with pytest.raises(ValueError, match="1..n in order"):
        MigrationSet("p", "p.db", (Migration(2, "b", noop), Migration(1, "a", noop)))
    with pytest.raises(ValueError, match="unique"):
        MigrationSet("p", "p.db", (Migration(1, "a", noop), Migration(2, "a", noop)))
    with pytest.raises(ValueError, match="inside AGENTOS_HOME"):
        MigrationSet("p", "../p.db", (Migration(1, "a", noop),))


def test_applies_in_order_and_records_versions(tmp_path):
    log: list[str] = []
    s = calls_set(log)
    result = migrate.migrate(tmp_path, [s])
    assert log == ["v1", "v2", "v3"]
    assert [(p, v) for p, v, _ in result.applied] == [("thing", 1), ("thing", 2), ("thing", 3)]
    assert result.backup_dir is None  # the database did not exist, so there was nothing to back up
    with sqlite3.connect(tmp_path / "thing.db") as db:
        rows = db.execute("SELECT package, version, name FROM schema_version ORDER BY version").fetchall()
    assert rows == [("thing", 1, "step 1"), ("thing", 2, "step 2"), ("thing", 3, "step 3")]
    assert (tmp_path / "thing.db").stat().st_mode & 0o777 == 0o600


def test_rerun_is_a_no_op_without_a_backup(tmp_path):
    log: list[str] = []
    migrate.migrate(tmp_path, [calls_set(log)])
    again = migrate.migrate(tmp_path, [calls_set(log)])
    assert log == ["v1", "v2", "v3"] and again.applied == [] and again.backup_dir is None
    assert not (tmp_path / "backups").exists()


def test_new_migration_backs_up_first(tmp_path):
    log: list[str] = []
    two = MigrationSet("thing", "thing.db", calls_set(log).migrations[:2], create=True)
    migrate.migrate(tmp_path, [two])
    result = migrate.migrate(tmp_path, [calls_set(log)], now=datetime(2026, 11, 4, tzinfo=UTC))
    assert [v for _, v, _ in result.applied] == [3]
    snap = result.backup_dir / "thing.db"
    assert snap.exists() and snap.stat().st_mode & 0o777 == 0o600
    assert result.backup_dir.stat().st_mode & 0o777 == 0o700
    assert tables(snap) == {"schema_version", "t1", "t2"}  # the state before migration 3
    assert "t3" in tables(tmp_path / "thing.db")


def test_failed_migration_rolls_back_and_keeps_earlier_ones(tmp_path):
    log: list[str] = []
    migrate.migrate(tmp_path, [MigrationSet("thing", "thing.db", calls_set(log).migrations[:1], create=True)])
    with pytest.raises(MigrationError, match="migration 2") as exc:
        migrate.migrate(tmp_path, [calls_set(log, fail_at=2)])
    assert exc.value.backup_dir is not None and (exc.value.backup_dir / "thing.db").exists()
    assert "t2" not in tables(tmp_path / "thing.db")
    assert migrate.status(tmp_path, [calls_set([])])[0].current == 1
    # fixed code: the rerun picks up where it stopped
    result = migrate.migrate(tmp_path, [calls_set([])])
    assert [v for _, v, _ in result.applied] == [2, 3]


def test_restore_databases_puts_the_old_file_back(tmp_path):
    log: list[str] = []
    migrate.migrate(tmp_path, [MigrationSet("thing", "thing.db", calls_set(log).migrations[:1], create=True)])
    result = migrate.migrate(tmp_path, [calls_set(log)])
    assert "t3" in tables(tmp_path / "thing.db")
    restored = migrate.restore_databases(result.backup_dir, tmp_path)
    assert restored == ["thing.db"]
    assert tables(tmp_path / "thing.db") == {"schema_version", "t1"}


def test_newer_schema_is_refused(tmp_path):
    migrate.migrate(tmp_path, [calls_set([])])
    older = MigrationSet("thing", "thing.db", calls_set([]).migrations[:2])
    st = migrate.status(tmp_path, [older])[0]
    assert st.newer and st.current == 3 and st.latest == 2
    with pytest.raises(NewerSchema, match="knows only 2"):
        migrate.migrate(tmp_path, [older])


def test_missing_database_without_create_is_left_alone(tmp_path):
    s = calls_set([], create=False)
    assert migrate.status(tmp_path, [s])[0].pending == []
    assert migrate.migrate(tmp_path, [s]).applied == []
    assert not (tmp_path / "thing.db").exists()


def test_node_baseline_stamps_an_m1_database_without_change(tmp_path):
    with sqlite3.connect(tmp_path / "agentos.db") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE lanes (id TEXT PRIMARY KEY, spec TEXT NOT NULL, last_used TEXT, exit_ip TEXT)")
        db.execute("INSERT INTO lanes (id, spec) VALUES ('a/b', '{}')")
    st = migrate.status(tmp_path, [migrate.NODE])[0]
    assert (st.current, st.pending) == (0, ["lanes table"])
    result = migrate.migrate(tmp_path, [migrate.NODE])
    assert result.backup_dir is not None
    with sqlite3.connect(tmp_path / "agentos.db") as db:
        assert db.execute("SELECT COUNT(*) FROM lanes").fetchone()[0] == 1
    assert migrate.status(tmp_path, [migrate.NODE])[0].pending == []


def test_pre_migration_backups_are_pruned(tmp_path):
    for i in range(7):
        s = MigrationSet("p", "p.db", tuple(Migration(n, f"m{n}", lambda c: None) for n in range(1, i + 2)),
                         create=True)
        migrate.migrate(tmp_path, [s], now=datetime(2026, 11, 4, 0, 0, i, tzinfo=UTC))
    dirs = sorted(p.name for p in migrate.pre_migration_dir(tmp_path).iterdir())
    assert len(dirs) == migrate.KEEP_PRE_MIGRATION


def test_dry_run_changes_nothing(tmp_path):
    result = migrate.migrate(tmp_path, [calls_set([])], dry_run=True)
    assert result.applied == [] and not (tmp_path / "thing.db").exists()


def test_discover_finds_package_migrations(monkeypatch):
    fake = types.ModuleType("agentos.fakepkg.migrations")
    fake.MIGRATIONS = MigrationSet("fakepkg", "fake.db", (Migration(1, "init", lambda c: None),))

    class Info:
        name, ispkg = "fakepkg", True

    monkeypatch.setattr(migrate.pkgutil, "iter_modules", lambda path: [Info()])
    monkeypatch.setattr(migrate.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(migrate.importlib, "import_module", lambda name: fake)
    sets = migrate.discover()
    assert [s.package for s in sets] == ["node", "fakepkg"]


def test_discover_on_this_build_has_the_node_set():
    assert migrate.discover()[0] is migrate.NODE
