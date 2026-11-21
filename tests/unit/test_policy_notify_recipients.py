"""Who is pushed: the phase 1 recipient checks (the rules B1's decide applies, used to pick
recipients, never to allow) and the member directory over live human grants."""

from __future__ import annotations

import json
from types import SimpleNamespace

from test_policy_support import Clock, make_service

from openajar.core.models import Effect
from openajar.policy.grants import Grant, GrantBlock, Subject
from openajar.policy.recipients import ApprovalFacts, GrantDirectory, NoMembers, could_control, could_decide


def human(member: str, workspaces: list[str], *, approve: list[str] | None = None, max_usd: float | None = None,
          control: bool = False, watch_only: bool = False, gid: str | None = None) -> Grant:
    """A member grant as B1 mints it (section 4.4). Specials gains `approve` and `control` in B1;
    until then the block carries them as plain objects."""
    tag = member.replace("_", "").lower().ljust(8, "0")
    b = GrantBlock(v=1, id=gid or f"gr_{tag}0001", rid=f"rv_{(gid or tag)[-12:].replace('_', '')}0001",
                   iat=1, sub=Subject(kind="human", id=member, owner=member), workspaces=workspaces, ceiling="read")
    specials = SimpleNamespace(
        approve=SimpleNamespace(effects=approve, lanes=None, max_usd=max_usd) if approve is not None else None,
        control=SimpleNamespace(lanes=None, watch_only=watch_only, max_session_s=3600) if control else None)
    return Grant([b.model_copy(update={"specials": specials})])


def facts(**kw) -> ApprovalFacts:
    base = {"id": "ap_1", "lane": "biz-c/insta", "effect": "commit", "workspace": "biz-c"}
    return ApprovalFacts(**{**base, **kw})


def test_could_decide_follows_the_member_grant():
    va = human("m_va", ["biz-c"], approve=["commit"])
    assert could_decide(va, facts())
    assert not could_decide(va, facts(lane="biz-a/insta"))
    assert not could_decide(va, facts(effect="pay", amount_usd=10))
    assert could_decide(va, facts(kind="question", effect=None))
    buyer = human("m_buy", ["biz-c"], approve=["pay"], max_usd=50)
    assert could_decide(buyer, facts(effect="pay", amount_usd=50))
    assert not could_decide(buyer, facts(effect="pay", amount_usd=50.01))
    assert not could_decide(buyer, facts(effect="pay"))  # no node-parsed amount: not this member
    assert not could_decide(va, facts(requester_root=va.root_id))  # never their own family's request
    assert not could_decide(human("m_view", ["biz-c"]), facts())  # a viewer holds no approve


def test_agents_are_never_recipients():
    b = GrantBlock(v=1, id="gr_agent00001", rid="rv_agent00001", iat=1, sub=Subject(kind="agent", id="cc"),
                   workspaces=["biz-c"])
    agent = Grant([b.model_copy(update={"specials": SimpleNamespace(
        approve=SimpleNamespace(effects=["commit"], lanes=None, max_usd=None), control=None)})])
    assert not could_decide(agent, facts()) and not could_control(agent, "biz-c/insta")


def test_facts_come_from_the_approval_row():
    row = {"id": "ap_x", "kind": "action", "state": "pending", "lane": "biz-c/insta", "effect": "commit",
           "requester": json.dumps({"id": "cc", "owner": "m_va"}), "grant_path": "/gr_root/gr_child/",
           "expires_at": 5.0}
    f = ApprovalFacts.from_row(row)
    assert (f.workspace, f.requester_root, f.requester_owner, f.not_after) == ("biz-c", "gr_root", "m_va", None)
    f2 = ApprovalFacts.from_row({**row, "workspace": "biz-c", "not_after": 9.0, "amount_usd": 12.5,
                                 "requester_owner": "m_x"})
    assert (f2.not_after, f2.amount_usd, f2.requester_owner) == (9.0, 12.5, "m_x")


def _store_grant(svc, g: Grant, *, expires_at=None):
    b = g.blocks[0]
    svc.db.execute("INSERT INTO grants (id, parent_id, root_id, path, depth, rid, token_sha256, source, subject, "
                   "block, issued_at, expires_at, issued_by, v, block_sha256) "
                   "VALUES (?, NULL, ?, ?, 0, ?, NULL, 'issued', ?, ?, 1, ?, 'owner', 1, ?)",
                   (b.id, b.id, f"/{b.id}/", b.rid, json.dumps(g.subject.model_dump()),
                    json.dumps(b.model_copy(update={"specials": None}).to_data()), expires_at, b.digest()))


def test_the_grant_directory_reads_live_human_grants(tmp_path):
    clock = Clock()
    svc, _ = make_service(tmp_path, clock=clock)
    va = human("m_va", ["biz-c"], approve=["commit"])
    _store_grant(svc, va)
    _store_grant(svc, human("m_old", ["biz-c"], gid="gr_mold00001"), expires_at=clock.t - 1)
    _store_grant(svc, human("m_gone", ["biz-c"], gid="gr_mgone0001"))
    svc.db.execute("INSERT INTO revoked (grant_id, rid, revoked_at) VALUES ('gr_mgone0001', NULL, 1)")
    svc.issue(subject={"id": "agent"}, workspaces=["biz-c"], ceiling=Effect.COMMIT)  # agents are not members
    seen = []
    d = GrantDirectory(svc.db, clock=clock, decide=lambda g, f: seen.append(g.id) or g.lane_allowed(f.lane))
    assert d.member_ids() == ["m_va"]
    assert d.can_decide("m_va", facts()) and not d.can_decide("m_va", facts(lane="biz-a/x"))
    assert seen == [va.id, va.id]
    # with the real check, a grant that has no approve special (until B1 adds it) decides nothing
    assert not GrantDirectory(svc.db, clock=clock).can_decide("m_va", facts())
    svc.close()


def test_a_solo_node_has_no_members():
    d = NoMembers()
    assert d.member_ids() == [] and not d.can_decide("m", facts()) and not d.can_control("m", "a/b")


def test_control_needs_more_than_watching():
    op = human("m_op", ["biz-c"], control=True)
    assert could_control(op, "biz-c/insta") and not could_control(op, "biz-a/insta")
    assert not could_control(human("m_w", ["biz-c"], control=True, watch_only=True), "biz-c/insta")
    assert not could_control(human("m_v", ["biz-c"]), "biz-c/insta")
