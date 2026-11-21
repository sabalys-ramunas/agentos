"""SQLite storage for workspaces: schema version, WAL, adopt-vs-record, overrides, corrupt files."""

from __future__ import annotations

import sqlite3

import pytest

from openajar.core.errors import ServiceUnavailable
from openajar.workspaces.store import SCHEMA_VERSION, WorkspaceStore

NOW = "2026-11-04T10:00:00+00:00"


def workspace_row(workspace_id: str = "acme", **over) -> dict:
    row = {"id": workspace_id, "name": "Acme", "color": "#4f7cff", "default_proxy": "lt1", "notes": "",
           "created_at": NOW, "updated_at": NOW}
    return {**row, **over}


def lane_row(lane_id: str = "acme/instagram", **over) -> dict:
    workspace, name = lane_id.split("/")
    row = {"id": lane_id, "workspace": workspace, "surface": name, "platform": name, "template": "social-6",
           "home_url": None, "login_url": "https://www.instagram.com/accounts/login/", "created_at": NOW}
    return {**row, **over}


@pytest.fixture
def store(tmp_path):
    s = WorkspaceStore(tmp_path / "workspaces.db")
    yield s
    s.close()


def test_schema_version_wal_and_private_file(tmp_path, store):
    row = sqlite3.connect(store.path).execute("SELECT version FROM schema_version").fetchone()
    assert row == (SCHEMA_VERSION,)
    journal = sqlite3.connect(store.path).execute("PRAGMA journal_mode").fetchone()[0]
    assert journal == "wal"
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_reopening_keeps_data_and_does_not_add_a_second_version_row(tmp_path):
    first = WorkspaceStore(tmp_path / "workspaces.db")
    first.insert_workspace(workspace_row())
    first.close()
    second = WorkspaceStore(tmp_path / "workspaces.db")
    try:
        assert second.get_workspace("acme")["name"] == "Acme"
        assert len(sqlite3.connect(second.path).execute("SELECT * FROM schema_version").fetchall()) == 1
    finally:
        second.close()


def test_workspace_round_trip_update_and_delete_takes_its_lane_rows(store):
    store.insert_workspace(workspace_row())
    store.adopt_lane(lane_row())
    store.update_workspace("acme", {"name": "Acme Ltd", "notes": "main brand"}, "2026-11-05T00:00:00+00:00")
    got = store.get_workspace("acme")
    assert got["name"] == "Acme Ltd" and got["notes"] == "main brand" and got["color"] == "#4f7cff"
    assert got["updated_at"] == "2026-11-05T00:00:00+00:00"
    assert [w["id"] for w in store.list_workspaces()] == ["acme"]

    store.delete_workspace("acme")
    assert store.get_workspace("acme") is None
    assert store.lane_rows() == {}


def test_update_refuses_columns_that_do_not_exist(store):
    store.insert_workspace(workspace_row())
    with pytest.raises(ValueError, match="not workspace columns"):
        store.update_workspace("acme", {"id": "other"}, NOW)


def test_adopt_fills_missing_details_but_never_touches_readiness_or_existing_values(store):
    store.adopt_lane(lane_row(login_url=None))
    store.set_readiness("acme/instagram", workspace="acme", platform="instagram", readiness="ready",
                        note=None, by="owner", now=NOW)
    store.adopt_lane(lane_row(login_url="https://other.example.com/login", surface="ig", template="personal"))
    row = store.lane_row("acme/instagram")
    assert row["readiness"] == "ready"
    assert row["login_url"] == "https://other.example.com/login"  # filled in by the first adopt
    assert row["template"] == "social-6"                          # never overwritten
    assert row["surface"] == "instagram"


def test_readiness_on_a_lane_without_a_row_starts_from_the_default(store):
    store.set_readiness("personal/reddit", workspace="personal", platform="reddit", readiness="needs_attention",
                        note="2FA code asked", by="owner", now=NOW)
    row = store.lane_row("personal/reddit")
    assert (row["readiness"], row["note"], row["platform"], row["updated_by"]) == (
        "needs_attention", "2FA code asked", "reddit", "owner")
    assert row["created_at"] == NOW and row["surface"] is None


def test_record_lane_starts_as_needs_login(store):
    store.record_lane(lane_row())
    assert store.lane_row("acme/instagram")["readiness"] == "needs_login"


def test_overrides_are_kept_newest_first_with_their_rules(store):
    first = store.add_override(at=NOW, by="owner", lane="acme/reddit", proxy="lt1",
                               rules=["proxy lt1 is already used by ..."], reason="same person")
    second = store.add_override(at=NOW, by="claude", lane="acme/facebook", proxy="lt1", rules=[], reason="x")
    listed = store.list_overrides(limit=10)
    assert [o["id"] for o in listed] == [second, first]
    assert listed[1]["rules"] == ["proxy lt1 is already used by ..."] and listed[1]["reason"] == "same person"
    assert store.list_overrides(limit=1)[0]["id"] == second


def test_a_damaged_file_gives_a_clean_error_that_says_lanes_are_fine(tmp_path):
    path = tmp_path / "workspaces.db"
    path.write_bytes(b"this is not a database" * 200)
    with pytest.raises(ServiceUnavailable) as info:
        WorkspaceStore(path)
    assert "workspaces.db could not be read or written" in info.value.message
    assert "Lanes are not affected" in info.value.message
    assert info.value.code == "unavailable"


def test_a_file_from_a_newer_openajar_is_refused_not_downgraded(tmp_path):
    path = tmp_path / "workspaces.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION + 5,))
    db.commit()
    db.close()
    with pytest.raises(ServiceUnavailable, match="written by a newer openajar"):
        WorkspaceStore(path)
