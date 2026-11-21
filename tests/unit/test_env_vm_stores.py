"""The environments and VM stores keep their file and rows: a database the old code wrote opens, a
write reads back, the file stays 600, and the CLI's second process sees the daemon's writes."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from openajar.core.errors import NotFound
from openajar.environments.models import Environment
from openajar.environments.store import SCHEMA as ENV_SCHEMA
from openajar.environments.store import EnvironmentStore
from openajar.vms.models import VmRecord
from openajar.vms.store import SCHEMA as VM_SCHEMA
from openajar.vms.store import VmStore

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


def _env(env_id: str = "e7k2", state: str = "ready") -> Environment:
    return Environment(id=env_id, title="fix the shop", state=state, grant_id="g1", binding="session:s1",
                       created_at=NOW, expires_at=NOW + timedelta(hours=2))


def _vm(vm_id: str = "vm_1", state: str = "ready") -> VmRecord:
    return VmRecord(id=vm_id, lane_id="env/e7k2", node="node-1", profile="small", server_type="cx22",
                    location="hel1", image="ubuntu-24.04", state=state, created_at=NOW,
                    expires_at=NOW + timedelta(hours=1))


def _old_file(path, schema: str, sql: str, args: tuple) -> None:
    """A database as the old store wrote it: the same schema, one row, WAL."""
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(schema)
    db.execute(sql, args)
    db.close()


def test_environments_store_reads_an_old_file_and_round_trips(tmp_path):
    path = tmp_path / "environments.db"
    old = _env()
    _old_file(path, ENV_SCHEMA, "INSERT INTO environments (id, state, binding, grant_id, title, created_at, record) "
              "VALUES (?, ?, ?, ?, ?, ?, ?)", (old.id, old.state, old.binding, old.grant_id, old.title,
                                               old.created_at.isoformat(), old.model_dump_json()))
    store = EnvironmentStore(path)
    assert store.get("e7k2") == old and store.has("e7k2") and store.bound("session:s1") == old
    store.put(_env("e9xx", state="closed"))
    assert [e.id for e in store.open()] == ["e7k2"] and {e.id for e in store.recent()} == {"e7k2", "e9xx"}
    stored = store.update("e7k2", lambda env: setattr(env, "title", "renamed"))
    assert stored.title == "renamed" and store.get("e7k2").title == "renamed"
    assert store.update("e7k2", lambda env: (setattr(env, "title", "not kept"), False)[1]).title == "renamed"
    with pytest.raises(NotFound):
        store.update("nope", lambda env: None)
    other = EnvironmentStore(path)  # the CLI, in another process, on the same file
    assert other.get("e7k2").title == "renamed"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    rows = sqlite3.connect(path).execute("SELECT id, state, binding FROM environments ORDER BY id").fetchall()
    assert rows == [("e7k2", "ready", "session:s1"), ("e9xx", "closed", "session:s1")]
    store.close()
    other.close()


def test_vm_store_reads_an_old_file_and_round_trips(tmp_path):
    path = tmp_path / "vms.db"
    old = _vm()
    _old_file(path, VM_SCHEMA, "INSERT INTO vms (id, lane_id, state, created_at, record) VALUES (?, ?, ?, ?, ?)",
              (old.id, old.lane_id, old.state, old.created_at.isoformat(), old.model_dump_json()))
    path.chmod(0o644)
    store = VmStore(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"  # tightened on open, as before
    assert store.get("vm_1") == old and store.live_for_lane("env/e7k2") == old
    store.put(_vm("vm_2", state="deleted"))
    assert [r.id for r in store.live()] == ["vm_1"] and len(store.recent()) == 2 and len(store.since(NOW)) == 2
    with pytest.raises(NotFound):
        store.get("vm_3")
    assert VmStore(path).get("vm_2").state == "deleted"
    store.close()
