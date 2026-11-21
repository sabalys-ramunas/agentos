"""Every refusal reason of memory_write and the read gates (4.6, 4.7, 4.10, 4.11, 4.14)."""

from __future__ import annotations

import threading

import pytest
from testkit.memory import FakeTrace, fake_memory

from openajar.core.errors import InvalidRequest
from openajar.memory.errors import (
    BadEvidence,
    EntryNotFound,
    MemoryConflict,
    OutOfScope,
    PrivacyRefused,
    RateLimited,
    SecretInMemory,
    TooLarge,
    TrustTooLow,
    TypeNotAllowed,
)
from openajar.memory.models import Caller, Limits, WriteRequest

FAMILY = "family:gr_a"
OWNER = Caller.owner()


@pytest.fixture
def mem(tmp_path):
    m = fake_memory(tmp_path, vault_values=["hunter2-biz-a-pass"])
    yield m
    m.service.close()


def fact(title="a fact", **kw):
    return {"type": "fact", "title": title, **kw}


# ---- secret_in_memory -------------------------------------------------------------------------

def test_vault_value_is_refused(mem):
    with pytest.raises(SecretInMemory) as err:
        mem.service.write(mem.agent(), "biz-a", fact("login", body="the password is hunter2-biz-a-pass"))
    assert err.value.code == "secret_in_memory"
    assert "vault_value" in err.value.kinds
    assert "hunter2" not in err.value.message


@pytest.mark.parametrize("text", [
    "key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv",
    "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "grant agt1_abcdefghijklmnop",
    "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP",
    "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
])
def test_credential_patterns_are_refused(mem, text):
    with pytest.raises(SecretInMemory):
        mem.service.write(mem.agent(), "biz-a", fact("note", body=text))


def test_secret_in_title_and_attrs_is_refused(mem):
    with pytest.raises(SecretInMemory):
        mem.service.write(mem.agent(), "biz-a", fact("hunter2-biz-a-pass"))
    with pytest.raises(SecretInMemory):
        mem.service.write(mem.agent(), "biz-a", fact("x", attrs={"subject": "hunter2-biz-a-pass"}))


def test_secret_named_attribute_keys_are_refused(mem):
    with pytest.raises(SecretInMemory, match="attribute named password"):
        mem.service.write(mem.agent(private=True), "biz-a",
                          {"type": "contact", "title": "Ona", "attrs": {"handles": {"password": "x"}}})


def test_vault_entry_names_are_fine(mem):
    out = mem.service.write(mem.agent(), "biz-a", fact("log in with vault:biz-a/instagram"))
    assert out["trust"] == "agent"


def test_vault_scrub_failure_refuses(tmp_path):
    m = fake_memory(tmp_path)

    def broken(_text):
        raise RuntimeError("vault locked")
    m.service.scanner.vault_scrub = broken
    with pytest.raises(SecretInMemory, match="vault_unavailable"):
        m.service.write(m.agent(), "biz-a", fact())


def test_secret_learned_later_is_scrubbed_on_read_and_flagged(mem):
    note = mem.service.write(mem.agent(), "biz-a", fact("api", body="the new key is Zq9-new-secret-value"))
    mem.vault.values.add("Zq9-new-secret-value")
    entry = mem.service.get(mem.agent(), note["id"]).data
    assert "Zq9" not in entry["body"] and "[redacted]" in entry["body"]
    assert entry["flagged"] == ["secret_on_read"] or "secret_on_read" in mem.service.get(
        OWNER, note["id"]).data["flagged"]
    assert any(k == "memory_flagged" for k, _ in mem.events)


# ---- bad_evidence ---------------------------------------------------------------------------------

def test_missing_trace_is_bad_evidence(mem):
    with pytest.raises(BadEvidence):
        mem.service.write(mem.agent(), "biz-a", fact(evidence=["tr_nope"]))


def test_trace_of_another_workspace_is_bad_evidence(mem):
    mem.policy.add_trace(FakeTrace(id="tr_b", workspace="biz-b", family=FAMILY))
    with pytest.raises(BadEvidence):
        mem.service.write(mem.agent(), "biz-a", fact(evidence=["tr_b"]))


def test_trace_of_another_family_is_bad_evidence(mem):
    mem.policy.add_trace(FakeTrace(id="tr_other", workspace="biz-a", family="family:gr_other"))
    with pytest.raises(BadEvidence):
        mem.service.write(mem.agent(), "biz-a", fact(evidence=["tr_other"]))


def test_evidence_snapshot_and_source_url_come_from_the_node(mem):
    mem.policy.add_trace(FakeTrace(id="tr_1", workspace="biz-a", family=FAMILY, summary="snapshot of r/somesub rules",
                                   lane="biz-a/reddit", url="https://www.reddit.com/r/somesub/about/rules"))
    note = mem.service.write(mem.agent(), "biz-a", fact(evidence=["tr_1"]))
    prov = mem.service.get(OWNER, note["id"]).data["prov"]
    assert prov["source_url"] == "https://www.reddit.com/r/somesub/about/rules"
    assert prov["lane"] == "biz-a/reddit"
    assert prov["evidence"][0] == {"trace": "tr_1", "tool": "browser_snapshot", "at": prov["evidence"][0]["at"],
                                   "status": "ok", "summary": "snapshot of r/somesub rules"}
    assert prov["principal"]["id"] == "claude-code" and prov["node"] == "box-1"
    assert prov["write_trace"] == "tr_call"


def test_owner_may_cite_any_trace_of_the_workspace(mem):
    mem.policy.add_trace(FakeTrace(id="tr_x", workspace="biz-a", family="family:gr_other"))
    assert mem.service.write(OWNER, "biz-a", fact(evidence=["tr_x"]))["trust"] == "owner"


def test_no_policy_port_means_no_evidence(tmp_path):
    m = fake_memory(tmp_path)
    m.service.ports.policy = None
    with pytest.raises(BadEvidence):
        m.service.write(m.agent(), "biz-a", fact(evidence=["tr_1"]))


# ---- conflict and compare-and-set -----------------------------------------------------------------

def test_existing_key_without_supersedes_is_a_conflict(mem):
    first = mem.service.write(mem.agent(), "biz-a", fact("4,210 followers", key="ig.followers"))
    with pytest.raises(MemoryConflict) as err:
        mem.service.write(mem.agent(), "biz-a", fact("4,300 followers", key="ig.followers"))
    assert err.value.code == "conflict"
    assert err.value.current == {"id": first["id"], "version": 1, "trust": "agent"}


def test_stale_supersedes_is_a_conflict(mem):
    first = mem.service.write(mem.agent(), "biz-a", fact("v1"))
    mem.service.write(mem.agent(), "biz-a", fact("v2", supersedes=f"{first['id']}@1"))
    with pytest.raises(MemoryConflict) as err:
        mem.service.write(mem.agent(), "biz-a", fact("v2 again", supersedes=f"{first['id']}@1"))
    assert err.value.current["version"] == 2


def test_compare_and_set_race_one_writer_wins(mem):
    first = mem.service.write(mem.agent(), "biz-a", fact("v1"))
    barrier = threading.Barrier(8)
    results: list[str] = []

    def writer(i: int) -> None:
        caller = mem.agent(family=f"family:gr_{i}", principal=f"agent-{i}")
        barrier.wait()
        try:
            mem.service.write(caller, "biz-a", fact(f"from {i}", supersedes=f"{first['id']}@1"))
            results.append("won")
        except MemoryConflict:
            results.append("conflict")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["conflict"] * 7 + ["won"]
    assert mem.service.get(OWNER, first["id"]).data["current"] == 2
    assert mem.service.verify("biz-a")["ok"]


def test_store_rechecks_inside_the_transaction(mem):
    """Two plans made against version 1: the store, not the pre-check, decides."""
    first = mem.service.write(mem.agent(), "biz-a", fact("v1"))
    a = mem.service._prepare(mem.agent(), "biz-a", WriteRequest.parse(fact("a", supersedes=f"{first['id']}@1")))
    b = mem.service._prepare(mem.agent(), "biz-a", WriteRequest.parse(fact("b", supersedes=f"{first['id']}@1")))
    mem.service._store_prepared(mem.agent(), "biz-a", a)
    with pytest.raises(MemoryConflict):
        mem.service._store_prepared(mem.agent(), "biz-a", b)


def test_supersede_keeps_type_and_key(mem):
    first = mem.service.write(mem.agent(), "biz-a", fact("v1", key="k.one"))
    with pytest.raises(InvalidRequest):
        mem.service.write(mem.agent(), "biz-a", {"type": "preference", "title": "x",
                                                 "supersedes": f"{first['id']}@1"})
    with pytest.raises(InvalidRequest):
        mem.service.write(mem.agent(), "biz-a", fact("v2", key="k.two", supersedes=f"{first['id']}@1"))
    out = mem.service.write(mem.agent(), "biz-a", fact("v2", supersedes=f"{first['id']}@1"))
    assert out["key"] == "k.one"


# ---- too_large ------------------------------------------------------------------------------------

def test_size_caps(mem):
    with pytest.raises(TooLarge):
        mem.service.write(mem.agent(), "biz-a", fact("t" * 201))
    with pytest.raises(TooLarge):
        mem.service.write(mem.agent(), "biz-a", fact(body="b" * 2001))
    assert mem.service.write(mem.agent(), "biz-a", {"type": "document", "title": "doc", "body": "b " * 30000})
    with pytest.raises(TooLarge):
        mem.service.write(mem.agent(), "biz-a", {"type": "document", "title": "doc", "body": "b" * 65537})
    with pytest.raises(TooLarge):
        mem.service.write(mem.agent(), "biz-a", fact(evidence=[f"tr_{i}" for i in range(21)]))
    with pytest.raises(TooLarge):
        mem.service.write(mem.agent(private=True), "biz-a", {
            "type": "contact", "title": "Ona", "attrs": {"handles": {f"p{i}": "h" * 190 for i in range(25)}}})


def test_live_entry_and_content_caps(tmp_path):
    m = fake_memory(tmp_path, limits=Limits(live_entries=3, workspace_bytes=10_000))
    for i in range(3):
        m.service.write(OWNER, "biz-a", fact(f"f{i}"))
    with pytest.raises(TooLarge, match="live entries"):
        m.service.write(OWNER, "biz-a", fact("one more"))
    m2 = fake_memory(tmp_path / "b", limits=Limits(workspace_bytes=3000))
    m2.service.write(OWNER, "biz-a", fact("x", body="y" * 1900))
    with pytest.raises(TooLarge, match="size cap"):
        m2.service.write(OWNER, "biz-a", fact("x", body="y" * 1900))


# ---- rate_limited -------------------------------------------------------------------------------------

def test_writes_per_family_per_hour(tmp_path):
    m = fake_memory(tmp_path, limits=Limits(writes_per_hour=5, untrusted_writes_per_hour=2, writes_per_day=8))
    agent = m.agent()
    for i in range(5):
        m.service.write(agent, "biz-a", fact(f"f{i}"))
    with pytest.raises(RateLimited):
        m.service.write(agent, "biz-a", fact("f5"))
    # a child grant of the same family shares the cap
    with pytest.raises(RateLimited):
        m.service.write(m.agent(principal="child"), "biz-a", fact("f6"))
    # another family does not
    m.service.write(m.agent(family="family:gr_b"), "biz-a", fact("other"))
    m.clock.advance(3601)
    for i in range(3):
        m.service.write(agent, "biz-a", fact(f"g{i}"))
    with pytest.raises(RateLimited, match="today"):
        m.service.write(agent, "biz-a", fact("over the day"))
    # the owner and the node are not rate limited
    for i in range(10):
        m.service.write(OWNER, "biz-a", fact(f"o{i}"))


def test_untrusted_writes_have_a_lower_cap(tmp_path):
    m = fake_memory(tmp_path, limits=Limits(untrusted_writes_per_hour=2))
    m.policy.read_untrusted(FAMILY)
    m.service.write(m.agent(), "biz-a", fact("u1"))
    m.service.write(m.agent(), "biz-a", fact("u2"))
    with pytest.raises(RateLimited, match="untrusted"):
        m.service.write(m.agent(), "biz-a", fact("u3"))


def test_refused_writes_do_not_count(tmp_path):
    m = fake_memory(tmp_path, limits=Limits(writes_per_hour=1))
    with pytest.raises(TooLarge):
        m.service.write(m.agent(), "biz-a", fact("t" * 300))
    m.service.write(m.agent(), "biz-a", fact("ok"))


# ---- trust_too_low ----------------------------------------------------------------------------------------

def test_untrusted_cannot_replace_an_owner_entry(mem):
    doc = mem.service.write(OWNER, "biz-a", {"type": "document", "title": "Brand voice", "body": "warm"})
    mem.policy.read_untrusted(FAMILY)
    with pytest.raises(TrustTooLow, match="contests"):
        mem.service.write(mem.agent(), "biz-a", {"type": "document", "title": "Brand voice", "body": "loud",
                                                 "supersedes": f"{doc['id']}@1"})
    # the contests note is the way
    note = mem.service.write(mem.agent(), "biz-a", fact("brand voice may be outdated", contests=[doc["id"]]))
    assert mem.service.get(OWNER, doc["id"]).data["linked_from"] == {"contests": [note["id"]]}


def test_agent_cannot_replace_a_node_entry(mem):
    run = mem.service.run_summary({"lane": "biz-a/ig", "run_id": "r1", "status": "failed"})
    with pytest.raises(TypeNotAllowed):
        mem.service.write(mem.agent(), "biz-a", {"type": "run_summary", "title": "x", "supersedes": f"{run['id']}@1"})
    node_fact = mem.service.write(Caller.node(), "biz-a", fact("observed by the node"))
    with pytest.raises(TrustTooLow):
        mem.service.write(mem.agent(), "biz-a", fact("no", supersedes=f"{node_fact['id']}@1"))


def test_untrusted_cannot_replace_agent_but_agent_replaces_untrusted(mem):
    clean = mem.service.write(mem.agent(family="family:gr_clean"), "biz-a", fact("clean"))
    mem.policy.read_untrusted(FAMILY)
    with pytest.raises(TrustTooLow):
        mem.service.write(mem.agent(), "biz-a", fact("dirty", supersedes=f"{clean['id']}@1"))
    dirty = mem.service.write(mem.agent(), "biz-a", fact("dirty"))
    out = mem.service.write(mem.agent(family="family:gr_clean"), "biz-a", fact("fixed", supersedes=f"{dirty['id']}@1"))
    assert out["trust"] == "agent"


# ---- type_not_allowed ----------------------------------------------------------------------------------------

def test_only_the_node_writes_run_summaries(mem):
    with pytest.raises(TypeNotAllowed):
        mem.service.write(mem.agent(), "biz-a", {"type": "run_summary", "title": "x", "attrs": {"status": "failed"}})
    with pytest.raises(TypeNotAllowed):
        mem.service.write(OWNER, "biz-a", {"type": "run_summary", "title": "x", "attrs": {"status": "failed"}})


def test_grant_types_restrict_writes(mem):
    agent = mem.agent(types=["fact"])
    mem.service.write(agent, "biz-a", fact())
    with pytest.raises(TypeNotAllowed):
        mem.service.write(agent, "biz-a", {"type": "document", "title": "doc"})


def test_contacts_need_private(mem):
    with pytest.raises(TypeNotAllowed, match="private"):
        mem.service.write(mem.agent(), "biz-a", {"type": "contact", "title": "Ona"})


# ---- privacy -------------------------------------------------------------------------------------------------

def test_local_only_refuses_cloud_callers(mem):
    mem.privacy["biz-l"] = "local-only"
    mem.service.write(OWNER, "biz-l", fact("private plans"))
    agent = mem.agent("biz-l")
    for call in (lambda: mem.service.search(agent, "biz-l", "plans"),
                 lambda: mem.service.context(agent, "biz-l"),
                 lambda: mem.service.write(agent, "biz-l", fact())):
        with pytest.raises(PrivacyRefused, match="memory_cloud_ok"):
            call()
    # a node brain routed to a local model reads it whole
    local = mem.agent("biz-l", cloud=False, private=True, kind="brain")
    assert mem.service.search(local, "biz-l", "plans").data["hits"][0]["snippet"] is not None
    mem.cloud_ok.add("biz-l")
    hits = mem.service.search(mem.agent("biz-l", private=True), "biz-l", "plans").data["hits"]
    assert hits and "withheld" not in hits[0]


def test_redacted_cloud_masks_and_shows_private_by_title(mem):
    mem.privacy["biz-r"] = "redacted-cloud"
    c = mem.service.write(OWNER, "biz-r", {"type": "contact", "title": "Ona the florist", "body": "call on Mondays",
                                          "attrs": {"handles": {"instagram": "@ona"}}})
    agent = mem.agent("biz-r", private=True)
    hit = mem.service.search(agent, "biz-r", "florist").data["hits"][0]
    assert hit["withheld"] == "private" and "snippet" not in hit
    entry = mem.service.get(agent, c["id"]).data
    assert entry["body"] is None and entry["withheld"].startswith("private")
    local = mem.agent("biz-r", private=True, cloud=False)
    assert mem.service.get(local, c["id"]).data["attrs"]["handles"] == {"instagram": "@ona"}


def test_privacy_lookup_failure_is_local_only(mem):
    def broken(_ws):
        raise RuntimeError("brains down")
    mem.service.ports.privacy_for = broken
    with pytest.raises(PrivacyRefused):
        mem.service.search(mem.agent(), "biz-a", "x")


# ---- scope ---------------------------------------------------------------------------------------------------

def test_other_workspace_is_out_of_scope(mem):
    note = mem.service.write(OWNER, "biz-a", fact("biz-a secret plan"))
    b = mem.agent("biz-b")
    with pytest.raises(OutOfScope) as err:
        mem.service.search(b, "biz-a", "plan")
    assert err.value.code == "out_of_scope"
    with pytest.raises(OutOfScope):
        mem.service.context(b, "biz-a")
    with pytest.raises(EntryNotFound):
        mem.service.get(b, note["id"])
    with pytest.raises(OutOfScope):
        mem.service.get(b, note["id"], workspace="biz-a")


def test_read_only_grant_cannot_write(mem):
    with pytest.raises(OutOfScope, match="not write"):
        mem.service.write(mem.agent(write=False), "biz-a", fact())


def test_audience_owner_entries_stay_hidden(mem):
    secret = mem.service.write(OWNER, "biz-a", fact("the VA's contract ends in March", audience="owner"))
    va = mem.agent()
    with pytest.raises(EntryNotFound):
        mem.service.get(va, secret["id"])
    assert mem.service.search(va, "biz-a", "contract").data["hits"] == []
    assert mem.service.context(va, "biz-a", task="contract").data["relevant"] == []
    with pytest.raises(OutOfScope):
        mem.service.write(va, "biz-a", fact("x", audience="owner"))
    admin = mem.agent(audience_owner=True)
    assert mem.service.search(admin, "biz-a", "contract").data["hits"][0]["id"] == secret["id"]


def test_private_entries_need_the_private_scope(mem):
    c = mem.service.write(OWNER, "biz-a", {"type": "contact", "title": "Ona", "body": "phone on file"})
    agent = mem.agent()
    with pytest.raises(OutOfScope, match="private"):
        mem.service.get(agent, c["id"])
    hit = mem.service.search(agent, "biz-a", "Ona").data["hits"][0]
    assert hit["withheld"] == "private"


def test_workspace_exists_port(mem):
    mem.service.ports.workspace_exists = lambda ws: ws == "biz-a"
    with pytest.raises(Exception, match="no workspace"):
        mem.service.write(OWNER, "biz-z", fact())


def test_bad_inputs_are_invalid(mem):
    with pytest.raises(InvalidRequest):
        mem.service.write(OWNER, "Biz A", fact())
    with pytest.raises(InvalidRequest):
        mem.service.write(OWNER, "biz-a", fact(key="Not A Slug"))
    with pytest.raises(InvalidRequest):
        mem.service.write(OWNER, "biz-a", fact(supersedes="mem_x"))
    with pytest.raises(InvalidRequest):
        mem.service.write(OWNER, "biz-a", fact(relates=["mem_0000000000000000"]))
    with pytest.raises(InvalidRequest):
        mem.service.write(OWNER, "biz-a", {"type": "fact", "title": "  "})
    with pytest.raises(InvalidRequest):
        mem.service.write(OWNER, "biz-a", fact(attrs={"nope": 1}))
    with pytest.raises(InvalidRequest):
        mem.service.write(OWNER, "biz-a", fact(attrs={"valid_from": "2026-11-02", "valid_to": "2026-11-01"}))
