"""The profile store's file, pinned: what one store writes, a second store on the same file (the
CLI in another process, or the daemon after a restart) reads back, row for row, and the file keeps
its name, mode and WAL journal."""

from __future__ import annotations

import sqlite3
import stat

from test_profiles import data

from agentos.profiles.models import Profile, ProfileState
from agentos.profiles.store import ProfileStore


def _fill(store: ProfileStore) -> dict:
    profile = Profile.model_validate(data())
    pv, created = store.put(profile, created_by={"id": "owner"}, reason="first")
    store.decide(profile.workspace, profile.id, pv.version, ProfileState.APPROVED, by="owner")
    store.mark("acme/linkedin", "headline", "h1", by="owner")
    store.set_sync("acme/linkedin", "headline", "synced", value_hash="h1", run_id="r1")
    lesson = store.add_lesson(site="linkedin", field="headline", surface="acme/linkedin", lane="acme/linkedin", by="a",
                              grant_id=None)
    store.set_lesson(lesson, "done")
    return {"profile": profile, "version": pv.version, "created": created}


def test_what_one_store_writes_another_on_the_same_file_reads(tmp_path):
    first = ProfileStore(tmp_path)
    put = _fill(first)
    second = ProfileStore(tmp_path)              # open while the first still is
    profile = put["profile"]
    assert put["created"] is True
    active = second.active(profile.workspace, profile.id)
    assert active is not None and active.version == put["version"] and active.state == ProfileState.APPROVED
    assert active.definition == first.active(profile.workspace, profile.id).definition
    assert [v.version for v in second.versions(profile.workspace, profile.id)] == [1]
    assert second.marks("acme/linkedin")["headline"]["value_hash"] == "h1"
    assert second.sync("acme/linkedin")["headline"]["status"] == "synced"
    assert [r["status"] for r in second.lessons(site="linkedin")] == ["done"]
    # the same content again is the same version, through either store
    assert second.put(profile, created_by=None)[1] is False
    first.close()
    second.close()
    third = ProfileStore(tmp_path)              # after a restart
    assert third.active(profile.workspace, profile.id).version == put["version"]
    third.close()


def test_the_file_keeps_its_name_mode_and_journal(tmp_path):
    store = ProfileStore(tmp_path)
    _fill(store)
    path = store.path
    store.close()
    assert path.name == "profiles.db"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"profiles", "photos", "marks", "sync"} <= tables
    finally:
        con.close()
