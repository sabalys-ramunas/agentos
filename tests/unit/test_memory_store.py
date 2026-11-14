"""The workspace file: append-only versions, the hash chain, forgetting, retention (4.3, 4.9, 4.15)."""

from __future__ import annotations

import sqlite3
import stat

import pytest

from agentos.core.errors import ServiceUnavailable
from agentos.memory.errors import EntryNotFound, OwnerTrustRequired
from agentos.memory.fakes import fake_memory
from agentos.memory.models import Caller, Limits, load_limits
from agentos.memory.store import WorkspaceDB

OWNER = Caller.owner(session="sess_1")


@pytest.fixture
def mem(tmp_path):
    m = fake_memory(tmp_path)
    yield m
    m.service.close()


def raw(mem, ws="biz-a") -> sqlite3.Connection:
    return sqlite3.connect(str(mem.service.stores.path(ws)))


def test_file_modes(mem):
    mem.service.write(OWNER, "biz-a", {"type": "fact", "title": "x"})
    path = mem.service.stores.path("biz-a")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    con = raw(mem)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_versions_are_append_only(mem):
    mem.service.write(OWNER, "biz-a", {"type": "fact", "title": "x"})
    con = raw(mem)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("UPDATE versions SET trust = 'owner'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("DELETE FROM versions")


def test_chain_verifies_and_links_versions(mem):
    s = mem.service
    a = s.write(OWNER, "biz-a", {"type": "fact", "title": "v1"})
    s.write(OWNER, "biz-a", {"type": "fact", "title": "v2", "supersedes": f"{a['id']}@1"})
    s.write(mem.agent(), "biz-a", {"type": "fact", "title": "other"})
    report = s.verify("biz-a")
    assert report["ok"] and report["versions"] == 3 and report["problems"] == []
    history = s.get(OWNER, a["id"], history=True).data["history"]
    assert history[1]["prev_hash"] == history[0]["version_hash"]
    assert report["head"] == s.get(OWNER, s.search(OWNER, "biz-a", "other").data["hits"][0]["id"]).data[
        "version_hash"]


def test_verify_without_a_file(mem):
    assert mem.service.verify("biz-z") == {"workspace": "biz-z", "ok": True, "versions": 0, "forgotten": 0,
                                           "head": None, "problems": []}


def test_altered_content_breaks_verify(mem):
    mem.service.write(OWNER, "biz-a", {"type": "fact", "title": "No posts on Sundays"})
    con = raw(mem)
    con.execute("UPDATE blobs SET data = 'Posts on Sundays are fine' WHERE data = 'No posts on Sundays'")
    con.commit()
    report = mem.service.verify("biz-a")
    assert not report["ok"] and any("title content was altered" in p for p in report["problems"])


def test_rewritten_version_breaks_verify(mem):
    mem.service.write(OWNER, "biz-a", {"type": "fact", "title": "a"})
    mem.service.write(mem.agent(), "biz-a", {"type": "fact", "title": "b"})
    con = raw(mem)
    con.execute("DROP TRIGGER versions_no_update")
    con.execute("UPDATE versions SET trust = 'owner' WHERE seq = 2")
    con.commit()
    problems = mem.service.verify("biz-a")["problems"]
    assert any("version_hash does not match" in p for p in problems)
    assert any("versions_no_update is missing" in p for p in problems)


def test_removed_version_breaks_verify(mem):
    for t in ("a", "b", "c"):
        mem.service.write(OWNER, "biz-a", {"type": "fact", "title": t})
    con = raw(mem)
    con.execute("DROP TRIGGER versions_no_delete")
    con.execute("DELETE FROM versions WHERE seq = 2")
    con.commit()
    problems = mem.service.verify("biz-a")["problems"]
    assert any("a version is missing" in p for p in problems)
    assert any("prev_hash does not match" in p for p in problems)


def test_missing_content_of_a_live_entry_breaks_verify(mem):
    mem.service.write(OWNER, "biz-a", {"type": "fact", "title": "a", "body": "b"})
    con = raw(mem)
    con.execute("DELETE FROM blobs WHERE data = 'b'")
    con.commit()
    assert any("body content is missing" in p for p in mem.service.verify("biz-a")["problems"])


# ---- forgetting ----------------------------------------------------------------------------------

def test_forget_erases_content_and_the_chain_still_verifies(mem):
    s = mem.service
    c = s.write(OWNER, "biz-a", {"type": "contact", "title": "Jonas Petraitis", "body": "+370 600 00000",
                                 "key": "contact.jonas", "attrs": {"handles": {"instagram": "@jonas"}}})
    s.write(OWNER, "biz-a", {"type": "contact", "title": "Jonas Petraitis", "body": "moved to Kaunas",
                             "supersedes": f"{c['id']}@1"})
    s.write(OWNER, "biz-a", {"type": "fact", "title": "unrelated"})
    out = s.forget(OWNER, c["id"], "GDPR erasure request")
    assert out["state"] == "forgotten" and out["versions"] == 2 and out["blobs_erased"] == 5     # title, body, attrs of v1; title, body of v2
    report = s.verify("biz-a")
    assert report["ok"] and report["forgotten"] == 1 and report["versions"] == 3
    entry = s.get(OWNER, c["id"], history=True).data
    assert entry["state"] == "forgotten" and entry["title"] is None and entry["body"] is None
    assert entry["key"] is None and entry["tombstone"]["reason"] == "GDPR erasure request"
    assert entry["tombstone"]["by"] == "sess_1"
    assert [v["forgotten"] for v in entry["history"]] == [True, True]
    assert entry["history"][0]["version_hash"]          # the hashes stay
    # really erased: not in the file, its free pages, the WAL, or the FTS index (stemmed, lowercase)
    on_disk = b"".join(p.read_bytes() for p in mem.service.stores.path("biz-a").parent.glob("biz-a.db*"))
    for needle in (b"Jonas", b"jonas", b"Petraitis", b"petraiti", b"Kaunas", b"kauna", b"+370 600", b"@jonas"):
        assert needle not in on_disk, needle
    # the key can be used again
    s.write(OWNER, "biz-a", {"type": "contact", "title": "Someone else", "key": "contact.jonas"})


def test_forget_needs_the_owner(mem):
    s = mem.service
    note = s.write(mem.agent(), "biz-a", {"type": "fact", "title": "x"})
    agent = mem.agent()
    plan = s.plan_forget(agent, note["id"], "wrong")
    assert "erases its content" in plan["summary"]
    with pytest.raises(OwnerTrustRequired) as err:
        s.forget(agent, note["id"], "wrong")
    assert err.value.digest == plan["digest"]
    with pytest.raises(Exception, match="different forget"):
        s.forget(agent.with_approval("apv_1", plan["digest"]), note["id"], "another reason")
    out = s.forget(agent.with_approval("apv_1", plan["digest"]), note["id"], "wrong")
    assert out["state"] == "forgotten"
    assert s.get(OWNER, note["id"]).data["tombstone"]["by"] == "claude-code (approval apv_1)"
    again = s.forget(OWNER, note["id"], "wrong")
    assert again["already"] is True


def test_forget_reason_is_checked(mem):
    note = mem.service.write(OWNER, "biz-a", {"type": "fact", "title": "x"})
    with pytest.raises(Exception, match="say why"):
        mem.service.forget(OWNER, note["id"], " ")
    with pytest.raises(Exception, match="secret"):
        mem.service.forget(OWNER, note["id"], "key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv")


def test_superseding_a_forgotten_entry_is_refused(mem):
    note = mem.service.write(OWNER, "biz-a", {"type": "fact", "title": "x"})
    mem.service.forget(OWNER, note["id"], "r")
    with pytest.raises(EntryNotFound):
        mem.service.write(OWNER, "biz-a", {"type": "fact", "title": "y", "supersedes": f"{note['id']}@1"})


# ---- retention ---------------------------------------------------------------------------------------

def test_expiry_and_purge(mem):
    s = mem.service
    mem.policy.read_untrusted("family:gr_web")
    u = s.write(mem.agent(family="family:gr_web"), "biz-a", {"type": "fact", "title": "likes 12"})
    a = s.write(mem.agent(family="family:gr_clean"), "biz-a", {"type": "fact", "title": "likes 13"})
    o = s.write(OWNER, "biz-a", {"type": "fact", "title": "likes 14"})
    run = s.run_summary({"lane": "biz-a/ig", "run_id": "r1", "status": "failed"})
    mem.clock.advance(31 * 86400)
    assert s.expire() == {"biz-a": {"expired": 1, "purged": 0}}
    assert s.get(OWNER, u["id"]).data["state"] == "expired"
    assert u["id"] not in [n["id"] for n in s.context(mem.agent(), "biz-a").data["notes"]]
    mem.clock.advance(90 * 86400)
    assert s.expire() == {"biz-a": {"expired": 0, "purged": 1}}
    gone = s.get(OWNER, u["id"]).data
    assert gone["state"] == "forgotten" and gone["title"] is None and gone["tombstone"]["by"] == "node"
    mem.clock.advance(60 * 86400)      # 181 days: the run summary expires
    assert s.expire() == {"biz-a": {"expired": 1, "purged": 0}}
    assert s.get(OWNER, run["id"]).data["state"] == "expired"
    mem.clock.advance(200 * 86400)     # past 365 days: the agent fact expires; the owner fact never
    s.expire()
    assert s.get(OWNER, a["id"]).data["state"] == "expired"
    assert s.get(OWNER, o["id"]).data["state"] == "live"
    assert s.verify("biz-a")["ok"]


def test_confirming_stops_expiry(mem):
    mem.policy.read_untrusted("family:gr_web")
    u = mem.service.write(mem.agent(family="family:gr_web"), "biz-a", {"type": "fact", "title": "likes 12"})
    mem.service.confirm(OWNER, u["id"], 1)
    mem.clock.advance(400 * 86400)
    assert mem.service.expire() == {}


def test_expired_entries_can_be_confirmed(mem):
    mem.policy.read_untrusted("family:gr_web")
    u = mem.service.write(mem.agent(family="family:gr_web"), "biz-a", {"type": "fact", "title": "x"})
    mem.clock.advance(31 * 86400)
    mem.service.expire()
    out = mem.service.confirm(OWNER, u["id"], 1, edit={"title": "x, checked"})
    assert out["trust"] == "owner"
    entry = mem.service.get(OWNER, u["id"]).data
    assert entry["state"] == "live" and entry["title"] == "x, checked"


# ---- bi-temporal facts ---------------------------------------------------------------------------

def test_later_reading_closes_the_earlier_one(mem):
    s = mem.service
    f = s.write(OWNER, "biz-a", {"type": "fact", "title": "followers 4,210", "key": "ig.followers",
                                 "attrs": {"subject": "lane:biz-a/instagram", "valid_from": "2026-11-01T00:00:00Z"}})
    s.write(OWNER, "biz-a", {"type": "fact", "title": "followers 4,300", "supersedes": f"{f['id']}@1",
                             "attrs": {"subject": "lane:biz-a/instagram", "valid_from": "2026-11-08T00:00:00Z"}})
    history = s.get(OWNER, f["id"], history=True).data["history"]
    assert history[0]["valid_to"] == "2026-11-08T00:00:00Z"
    assert s.get(OWNER, f["id"], as_of="2026-11-03T00:00:00Z").data["title"] == "followers 4,210"
    assert s.get(OWNER, f["id"], as_of="2026-11-09T00:00:00Z").data["title"] == "followers 4,300"
    with pytest.raises(EntryNotFound):
        s.get(OWNER, f["id"], as_of="2026-10-01T00:00:00Z")
    assert s.get(OWNER, f["id"], version=1).data["title"] == "followers 4,210"


# ---- the file itself -----------------------------------------------------------------------------

def test_newer_schema_is_refused(mem):
    mem.service.write(OWNER, "biz-a", {"type": "fact", "title": "x"})
    mem.service.close()
    con = raw(mem)
    con.execute("UPDATE schema_version SET version = 99")
    con.commit()
    with pytest.raises(ServiceUnavailable, match="newer agentos"):
        WorkspaceDB(mem.service.stores.path("biz-a"), "biz-a")


def test_damaged_file_is_unavailable_not_silent(tmp_path):
    m = fake_memory(tmp_path)
    path = m.service.stores.path("biz-a")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a database" * 100)
    with pytest.raises(ServiceUnavailable, match="could not be read") as err:
        m.service.write(OWNER, "biz-a", {"type": "fact", "title": "x"})
    assert err.value.code == "unavailable"
    # another workspace is not affected
    assert m.service.write(OWNER, "biz-b", {"type": "fact", "title": "x"})["trust"] == "owner"


def test_reads_never_create_a_file(mem):
    mem.service.search(mem.agent(), "biz-a", "x")
    mem.service.list_entries(mem.agent(), "biz-a")
    assert not mem.service.stores.path("biz-a").exists()


def test_limits_from_memory_toml(tmp_path):
    (tmp_path / "memory.toml").write_text("writes_per_hour = 7\n[body_chars]\nfact = 100\n")
    lim = load_limits(tmp_path)
    assert lim.writes_per_hour == 7 and lim.body_chars["fact"] == 100 and lim.body_chars["document"] == 65536
    (tmp_path / "memory.toml").write_text("nope = 1\n")
    with pytest.raises(Exception, match="not valid"):
        load_limits(tmp_path)
    assert Limits().context_budget_default == 1500


def test_events_carry_no_content(mem):
    note = mem.service.write(OWNER, "biz-a", {"type": "fact", "title": "secret plan", "body": "details"})
    kind, payload = mem.events[-1]
    assert kind == "memory_write"
    assert payload["id"] == note["id"] and payload["version_hash"] == note["version_hash"]
    assert "secret plan" not in str(payload) and "details" not in str(payload)
