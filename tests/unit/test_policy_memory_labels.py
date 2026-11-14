"""Ledger views and evidence for workspace memory (docs/design/workspace-memory.md 4.3, 4.5, 4.12):
the `ever` labels, context receipts and decisions_loaded, trace pins, and the memory port."""

from __future__ import annotations

import pytest
from test_policy_support import OWNER, Clock, make_service

from agentos.core.errors import ApprovalPending, InvalidRequest
from agentos.core.models import Effect
from agentos.policy.errors import Denied
from agentos.policy.grants import Specials
from agentos.policy.labels import KNOWN_LABELS
from agentos.policy.memory_rules import MemoryCap
from agentos.policy.taint import TAINT_LABELS, family_key
from agentos.policy.traces import TraceEvent, TraceStore

LANE = "biz-a/instagram"
HOUR = 3600


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    svc, _ = make_service(tmp_path, clock=clock)
    yield svc, clock
    svc.close()


def agent(svc, *, ttl_s: int = 7 * 86400, memory: dict | None = None, **kw):
    kw.setdefault("workspaces", ["biz-a"])
    g, tok = svc.issue(subject={"id": "agent-1"}, ceiling=Effect.COMMIT, ttl_s=ttl_s,
                       specials=Specials(memory=MemoryCap(**(memory or {"write": True}))), **kw)
    return g, tok


def test_the_known_labels_match_the_taint_ledger():
    assert KNOWN_LABELS == TAINT_LABELS


# ---------------------------------------------------------------- ever

def test_ever_outlives_the_12_hour_label(env):
    svc, clock = env
    g, tok = agent(svc)
    ctx = svc.context_for_token(tok, tool="browser_snapshot")
    ctx.trace("web_content", summary="read a page")
    fam = family_key(g)
    assert svc.taint.get(fam) == {"untrusted"} == svc.taint.ever(fam)
    clock.advance(13 * HOUR)
    assert svc.taint.get(fam) == set()
    assert svc.taint.ever(fam) == {"untrusted"}
    svc.taint.purge()
    assert svc.taint.ever(fam) == {"untrusted"}       # the root grant is still live


def test_a_child_shares_the_family_ever(env):
    svc, _ = env
    g, tok = agent(svc)
    _, child = svc.attenuate(tok, ceiling=Effect.DRAFT)
    svc.context_for_token(child, tool="t").taint("private")
    assert svc.taint.ever(family_key(g)) == {"private"}


def test_ever_is_dropped_when_the_root_grant_is_revoked(env):
    svc, _ = env
    g, tok = agent(svc)
    svc.context_for_token(tok, tool="t").taint("untrusted")
    _, child = svc.attenuate(tok, ceiling=Effect.DRAFT)
    svc.revoke(svc.resolve(child)[1].id)   # a child's revocation does not end the family
    svc.taint.purge()
    assert svc.taint.ever(family_key(g)) == {"untrusted"}
    svc.revoke(g.id)
    svc.taint.purge()
    assert svc.taint.ever(family_key(g)) == set()


def test_ever_is_dropped_after_the_root_grant_expires(env):
    svc, clock = env
    g, tok = agent(svc, ttl_s=2 * HOUR)
    other, other_tok = agent(svc)
    for t in (tok, other_tok):
        svc.context_for_token(t, tool="t").taint("untrusted")
    clock.advance(4 * HOUR)
    svc.taint.purge()
    assert svc.taint.ever(family_key(g)) == set()
    assert svc.taint.ever(family_key(other)) == {"untrusted"}


def test_the_owner_in_person_adds_no_labels(env):
    svc, _ = env
    ctx = svc.context(svc.owner_grant(), tool="panel", in_person=True)
    ctx.taint("untrusted")
    assert svc.taint.ever(ctx.taint_key) == set()


def test_ever_keeps_when_a_label_was_first_seen(env):
    svc, clock = env
    g, tok = agent(svc)
    svc.context_for_token(tok, tool="t").taint("untrusted")
    first = clock()
    clock.advance(HOUR)
    svc.context_for_token(tok, tool="t").taint("untrusted")
    assert svc.taint.ever_ledger.since(family_key(g)) == {"untrusted": first}


# ---------------------------------------------------------------- the port

def test_the_port_adds_labels_to_both_views_and_refuses_unknown_ones(env):
    svc, _ = env
    g, _ = agent(svc)
    port = svc.memory_rules
    fam = family_key(g)
    port.taint_add(fam, ["private"])
    assert port.taint_live(fam) == {"private"} == port.taint_ever(fam)
    with pytest.raises(InvalidRequest):
        port.add_labels(fam, ["secret"])


def test_the_caller_comes_from_the_call_context(env):
    svc, _ = env
    g, tok = agent(svc, memory={"workspaces": ["biz-a"], "write": True, "types": ["fact"]})
    port = svc.memory_rules
    ctx = svc.context_for_token(tok, tool="memory_write", lane_id=LANE)
    c = port.caller(ctx, "biz-a", write=True, entry_type="fact")
    assert (c.principal_id, c.family, c.viewer, c.in_person, c.owner_token, c.lane) == (
        "agent-1", family_key(g), g.id, False, False, LANE)
    assert c.prov()["grant"] == {"id": g.id, "root": g.root_id, "path": g.path}
    with pytest.raises(Denied) as e:
        port.caller(ctx, "biz-b")
    assert e.value.reason == "out_of_scope"
    with pytest.raises(Denied) as e:
        port.caller(ctx, "biz-a", write=True, entry_type="decision")
    assert e.value.reason == "special"


def test_the_owner_token_is_not_the_owner_in_person(env):
    svc, _ = env
    port = svc.memory_rules
    c = port.caller(svc.context_for_token(OWNER, tool="memory_write"), "biz-a", write=True)
    assert c.owner_token and not c.in_person and c.viewer is None
    c = port.caller(svc.context(svc.owner_grant(), tool="panel", in_person=True), "biz-a", write=True)
    assert c.in_person


# ---------------------------------------------------------------- receipts and decisions_loaded

def test_receipt_states(env):
    svc, _ = env
    port = svc.memory_rules
    assert port.receipt_state("family:gr_x", "biz-a", "sha256:1") == "never"
    port.record_receipt("family:gr_x", "biz-a", "sha256:1")
    assert port.receipt_state("family:gr_x", "biz-a", "sha256:1") == "current"
    assert port.receipt_state("family:gr_x", "biz-a", "sha256:2") == "stale"
    assert port.receipt_state("family:gr_x", "biz-b", "sha256:1") == "never"


def _authorize_traces(svc) -> list[dict]:
    events, _ = svc.traces.query(kind="authorize")
    return [e.args or {} for e in events]


async def _commit(svc, tok) -> None:
    ctx = svc.context_for_token(tok, tool="lane_post", lane_id=LANE)
    with pytest.raises(ApprovalPending):
        await ctx.authorize(Effect.COMMIT, lane_id=LANE, content={"caption": "hi"})


async def test_a_commit_trace_says_whether_the_decisions_were_loaded(env):
    svc, _ = env
    g, tok = agent(svc)
    port = svc.memory_rules
    await _commit(svc, tok)
    assert "decisions_loaded" not in _authorize_traces(svc)[0]     # memory not wired: nothing said
    digests = {"biz-a": "sha256:d1"}
    port.set_decisions_digest(digests.get)
    await _commit(svc, tok)
    assert _authorize_traces(svc)[0]["decisions_loaded"] == "never"
    port.record_receipt(family_key(g), "biz-a", "sha256:d1")
    await _commit(svc, tok)
    assert _authorize_traces(svc)[0]["decisions_loaded"] == "current"
    digests["biz-a"] = "sha256:d2"                                   # the owner added a decision
    await _commit(svc, tok)
    assert _authorize_traces(svc)[0]["decisions_loaded"] == "stale"


async def test_an_auto_commit_is_traced_with_its_receipt(tmp_path):
    from agentos.policy.routing import PolicyConfig, RoutingTable, Rule

    cfg = PolicyConfig(routing=RoutingTable(rules=[Rule(effect="*", then="auto")]))
    svc, _ = make_service(tmp_path, clock=Clock(), config=cfg)
    try:
        _, tok = agent(svc)
        svc.memory_rules.set_decisions_digest(lambda ws: "sha256:d1")
        d = await svc.context_for_token(tok, tool="lane_post").authorize(Effect.COMMIT, lane_id=LANE)
        assert d.route == "auto" and _authorize_traces(svc)[0]["decisions_loaded"] == "never"
        await svc.context_for_token(tok, tool="lane_post").authorize(Effect.DRAFT, lane_id=LANE)
        assert len(_authorize_traces(svc)) == 1                      # other effects stay untraced
    finally:
        svc.close()


async def test_a_broken_digest_getter_never_fails_the_commit(env):
    svc, _ = env
    _, tok = agent(svc)

    def broken(ws: str) -> str:
        raise RuntimeError("memory is down")

    svc.memory_rules.set_decisions_digest(broken)
    await _commit(svc, tok)
    assert "decisions_loaded" not in _authorize_traces(svc)[0]


async def test_no_decisions_means_no_receipt_field(env):
    svc, _ = env
    _, tok = agent(svc)
    svc.memory_rules.set_decisions_digest(lambda ws: None)
    await _commit(svc, tok)
    assert "decisions_loaded" not in _authorize_traces(svc)[0]


# ---------------------------------------------------------------- evidence and pins

def _trace(store: TraceStore, **kw) -> TraceEvent:
    return store.write(TraceEvent(**kw))


def test_visible_follows_the_traces_query_rule(env):
    svc, _ = env
    g, tok = agent(svc)
    _, child = svc.attenuate(tok, ceiling=Effect.DRAFT)
    cg = svc.resolve(child)[1]
    other, _ = agent(svc)
    mine = _trace(svc.traces, tool="browser_snapshot", lane=LANE, grant_id=g.id, grant_path=g.path)
    childs = _trace(svc.traces, tool="browser_snapshot", lane=LANE, grant_id=cg.id, grant_path=cg.path)
    theirs = _trace(svc.traces, tool="browser_snapshot", lane=LANE, grant_id=other.id, grant_path=other.path)
    elsewhere = _trace(svc.traces, tool="browser_snapshot", lane="biz-b/x", grant_id=g.id, grant_path=g.path)
    port = svc.memory_rules
    assert port.trace_visible(mine.id, g.id, "biz-a") and port.trace_visible(childs.id, g.id, "biz-a")
    assert not port.trace_visible(mine.id, cg.id, "biz-a")          # a child does not see its parent's
    assert port.trace_visible(mine.id, family_key(cg), "biz-a")      # unless asked for the whole family
    assert not port.trace_visible(theirs.id, g.id, "biz-a")
    assert not port.trace_visible(elsewhere.id, g.id, "biz-a")       # another workspace
    assert not port.trace_visible("tr_missing", None, "biz-a")
    assert port.trace_visible(theirs.id, None, "biz-a")              # the owner sees every family


def test_snapshot_and_the_node_observed_page_url(env):
    svc, _ = env
    store = svc.traces
    start = _trace(store, kind="tool_start", tool="browser_snapshot", lane=LANE, status="started",
                   args={"page_url": "https://evil.example/typed-by-the-agent"})
    inner = _trace(store, kind="page", tool="browser_snapshot", lane=LANE, parent_id=start.id,
                   args={"page_url": "https://www.reddit.com/r/somesub/about/rules"})
    end = _trace(store, kind="tool_end", tool="browser_snapshot", lane=LANE, parent_id=start.id,
                 summary="snapshot of r/somesub rules")
    port = svc.memory_rules
    for tid in (start.id, inner.id, end.id):
        assert port.lane_url_at(tid) == "https://www.reddit.com/r/somesub/about/rules"
    snap = port.trace_snapshot(end.id)
    assert snap["trace"] == end.id and snap["tool"] == "browser_snapshot" and snap["status"] == "ok"
    assert snap["summary"] == "snapshot of r/somesub rules" and snap["at"].endswith("Z")
    alone = _trace(store, kind="tool_start", tool="browser_snapshot", lane=LANE, args={"page_url": "https://x.example"})
    assert port.lane_url_at(alone.id) is None                        # agent arguments never count
    assert port.trace_snapshot("tr_missing") is None and port.lane_url_at("tr_missing") is None


def test_pinned_traces_survive_retention(env):
    svc, clock = env
    old = _trace(svc.traces, tool="browser_snapshot", lane=LANE)
    plain = _trace(svc.traces, tool="browser_snapshot", lane=LANE)
    assert svc.memory_rules.pin(old.id, "mem_a") is True
    clock.advance(100 * 86400)
    assert svc.traces.purge() == 1
    assert svc.traces.get(old.id) is not None and svc.traces.get(plain.id) is None
    assert svc.memory_rules.unpin("mem_a") == 1
    assert svc.traces.purge() == 1 and svc.traces.get(old.id) is None


def test_pins_are_capped_oldest_first(tmp_path):
    clock = Clock()
    store = TraceStore(tmp_path / "traces.db", clock=clock, max_pins=2)
    ids = []
    for i in range(3):
        ids.append(_trace(store, tool="t", lane=LANE).id)
        store.pin(ids[-1], f"mem_{i}")
        clock.advance(1)
    assert [p["trace_id"] for p in store.pins()] == ids[1:]
    clock.advance(100 * 86400)
    store.purge()
    assert store.get(ids[0]) is None and store.get(ids[1]) is not None and store.get(ids[2]) is not None
    assert store.pin("tr_missing", "mem_x") is False


def test_one_trace_may_be_pinned_by_several_entries(env):
    svc, clock = env
    t = _trace(svc.traces, tool="t", lane=LANE)
    svc.memory_rules.pin(t.id, "mem_a")
    svc.memory_rules.pin(t.id, "mem_b")
    svc.memory_rules.unpin("mem_a")
    clock.advance(100 * 86400)
    svc.traces.purge()
    assert svc.traces.get(t.id) is not None
    assert svc.memory_rules.unpin("mem_b", t.id) == 1
