"""Memory that moves: export and import records (design 4.13), run summaries that change status,
and the decisions digest getter for commit traces."""

from __future__ import annotations

import pytest
from testkit.memory import fake_memory

from openajar.memory import Caller

OWNER = Caller.owner()


@pytest.fixture
def src(tmp_path):
    m = fake_memory(tmp_path / "a")
    yield m
    m.service.close()


@pytest.fixture
def dst(tmp_path):
    m = fake_memory(tmp_path / "b", node_id="box-2")
    yield m
    m.service.close()


def _fill(m):
    s = m.service
    s.write(OWNER, "biz-a", {"type": "decision", "title": "No posts on Sundays", "key": "biz-a.posting.sunday",
                             "attrs": {"scope": ["biz-a/linkedin"]}})
    note = s.write(m.agent(), "biz-a", {"type": "fact", "title": "r/x removes link titles",
                                         "attrs": {"subject": "lane:biz-a/reddit"}})
    s.confirm(OWNER, note["id"], 1)
    gone = s.write(OWNER, "biz-a", {"type": "contact", "title": "Jane", "body": "jane's phone 555"})
    s.forget(OWNER, gone["id"], "asked to be forgotten")
    s.run_summary({"lane": "biz-a/instagram", "run_id": "run_1", "status": "succeeded-unverified"})
    return note, gone


def test_import_under_the_same_id_keeps_every_hash(src, dst):
    note, gone = _fill(src)
    records = src.service.export_records("biz-a", node_pubkey="ed25519:abc")
    assert records[0]["record"] == "memory" and records[0]["chain_ok"] and records[0]["node_pubkey"] == "ed25519:abc"
    assert "jane" not in str(records)          # forgotten content never leaves
    out = dst.service.import_records("biz-a", records)
    assert out["ok"] and out["same_hashes"] and out["forgotten"] == 1
    assert dst.service.verify("biz-a")["head"] == src.service.verify("biz-a")["head"]
    entry = dst.service.get(OWNER, note["id"], history=True).data
    assert entry["trust"] == "owner" and len(entry["history"]) == 2
    assert entry["prov"]["imported_from"]["node"] == "box-1"
    forgotten = dst.service.get(OWNER, gone["id"]).data
    assert forgotten["state"] == "forgotten" and forgotten["body"] is None and forgotten["tombstone"]
    hits = dst.service.search(OWNER, "biz-a", "removes").data["hits"]
    assert [h["id"] for h in hits] == [note["id"]]
    assert dst.service.search(OWNER, "biz-a", "jane").data["hits"] == []


def test_import_as_a_new_id_moves_keys_scopes_and_lanes(src, dst):
    note, _ = _fill(src)
    out = dst.service.import_records("biz-c", src.service.export_records("biz-a"))
    assert out["ok"] and not out["same_hashes"]
    rows = {r["type"]: r for r in dst.service.list_entries(OWNER, "biz-c").data["entries"]}
    decision = dst.service.get(OWNER, rows["decision"]["id"]).data
    assert decision["key"] == "biz-c.posting.sunday" and decision["attrs"]["scope"] == ["biz-c/linkedin"]
    fact = dst.service.get(OWNER, note["id"], version=1).data
    assert fact["subject"] == "lane:biz-c/reddit" and fact["attrs"]["subject"] == "lane:biz-c/reddit"
    assert fact["prov"]["imported_from"]["workspace"] == "biz-a"
    assert fact["prov"]["imported_from"]["version_hash"].startswith("sha256:")
    run = dst.service.get(OWNER, rows["run_summary"]["id"]).data
    assert run["attrs"]["lane"] == "biz-c/instagram"
    assert dst.service.decisions_for_lane("biz-c", "biz-c/linkedin")


def test_an_import_never_merges_and_a_bad_one_leaves_nothing(src, dst):
    _fill(src)
    records = src.service.export_records("biz-a")
    dst.service.write(OWNER, "biz-a", {"type": "fact", "title": "already here"})
    with pytest.raises(Exception, match="never merges"):
        dst.service.import_records("biz-a", records)
    tampered = [dict(r) for r in records]
    for r in tampered:
        if r["record"] == "version":
            r["at"] = r["at"] + 1
            break
    with pytest.raises(Exception, match="does not verify"):
        dst.service.import_records("biz-b", tampered)
    assert not dst.service.has_memory("biz-b") and "biz-b" not in dst.service.stores.workspaces()


def test_an_empty_workspace_exports_nothing(src):
    assert src.service.export_records("biz-z") == []
    assert src.service.import_records("biz-z", [])["entries"] == 0


def test_a_run_summary_with_a_new_status_is_a_new_version(src):
    s = src.service
    first = s.run_summary({"lane": "biz-a/instagram", "run_id": "run_9", "status": "succeeded-unverified"})
    same = s.run_summary({"lane": "biz-a/instagram", "run_id": "run_9", "status": "succeeded-unverified"})
    assert same["existing"] and same["version"] == 1
    later = s.run_summary({"lane": "biz-a/instagram", "run_id": "run_9", "status": "succeeded-verified"})
    assert later["id"] == first["id"] and later["version"] == 2 and later["trust"] == "node"
    assert s.get(OWNER, first["id"]).data["attrs"]["status"] == "succeeded-verified"


def test_the_digest_getter_is_none_without_decisions(src):
    s = src.service
    assert s.loaded_digest("biz-a") is None
    s.write(OWNER, "biz-a", {"type": "fact", "title": "a fact"})
    assert s.loaded_digest("biz-a") is None
    s.write(OWNER, "biz-a", {"type": "decision", "title": "No Sundays"})
    assert s.loaded_digest("biz-a") == s.decisions_digest("biz-a")
