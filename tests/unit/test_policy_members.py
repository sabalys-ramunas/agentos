"""Members: a person's grant per scope from fixed roles,
`approve` and `control`, decisions checked against the decider's grant, four-eyes, revocation,
and members never being bearer callers."""

from __future__ import annotations

import sqlite3

import pytest
from test_api_members import join, owner, role
from test_api_members import pending as pending_on_node
from test_node_panel import Node
from test_policy_support import OWNER, PRESENT, SPECS, Clock, make_service

from agentos.core.errors import ApprovalPending, InvalidRequest
from agentos.core.models import Effect
from agentos.policy import bind_request_token, tokens, wrap_tool
from agentos.policy.approvals import ApprovalPolicy, ApprovalStore, FourEyesRule
from agentos.policy.errors import Denied, TokenInvalid
from agentos.policy.grants import ApproveCap, Grant, Specials, Subject
from agentos.policy.routing import PolicyConfig, load_policy_config


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    svc, rt = make_service(tmp_path, clock=clock)
    yield svc, rt, clock
    svc.close()


def four_eyes_service(tmp_path, clock):
    cfg = PolicyConfig(approvals=ApprovalPolicy(four_eyes=[FourEyesRule(workspace="biz-*", effects=["pay"])]))
    return make_service(tmp_path, clock=clock, config=cfg)


async def pending(svc, *, lane="biz-c/insta", effect=Effect.COMMIT, owner=None, content="post it", facts=None,
                  cost=None, workspaces=None):
    """An agent's approval on `lane`; returns (approval id, the agent's Grant)."""
    _, tok = svc.issue(subject={"id": "claude-code"}, workspaces=workspaces or [lane.split("/")[0]],
                       ceiling=Effect.PAY, owner=owner)
    g = svc.resolve(tok)[1]
    ctx = svc.context(g, tool="lane_post")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(effect, lane_id=lane, content=content, summary="Do it", facts=facts, cost=cost)
    return e.value.approval_id, g


def denied(reason):
    return pytest.raises(Denied, match=f"^{reason}:")


# ---------------------------------------------------------------- the story in section 3.1

async def test_a_biz_c_approver_decides_biz_c_commit_and_nothing_else(env):
    svc, _, _ = env
    va = svc.issue_member_grant("m_va", "biz-c", "approver", handle="va")
    ap, _ = await pending(svc, lane="biz-c/insta")
    done = svc.approvals.decide(ap, "approve", decider=va, via="passkey", by="va")
    assert done.state == "approved" and done.decided_by == "va" and done.decided_via == "passkey"
    other, _ = await pending(svc, lane="biz-a/insta", content="other")
    with denied("not_approver"):
        svc.approvals.decide(other, "approve", decider=va, via="passkey")
    # a draft, an exec or a question elsewhere: still nothing outside approve's effects and scope
    assert va.approve_problem("exec", "biz-c/insta") is not None
    assert va.approve_problem("commit", "biz-c/insta") is None


async def test_pay_needs_max_usd_and_stays_under_it(env):
    svc, _, _ = env
    no_limit = svc.issue_member_grant("m_va", "biz-c", "approver", {"effects": ["commit", "pay"]})
    capped = svc.issue_member_grant("m_bo", "biz-c", "approver", {"max_usd": 50})
    assert set(capped.caps("approve")[0].effects) == {Effect.COMMIT, Effect.PAY}  # pay only with max_usd
    small, _ = await pending(svc, effect=Effect.PAY, facts={"amount_usd": 20.0}, content="small")
    big, _ = await pending(svc, effect=Effect.PAY, cost={"usd": 80.0}, content="big")
    unknown, _ = await pending(svc, effect=Effect.PAY, content="how much")
    assert svc.approvals.get(small).amount_usd == 20.0 and svc.approvals.get(big).amount_usd == 80.0
    with denied("not_approver"):
        svc.approvals.decide(small, "approve", decider=no_limit)
    with denied("not_approver") as e:
        svc.approvals.decide(big, "approve", decider=capped)
    assert "80.00 USD is over" in str(e.value)
    with denied("not_approver"):
        svc.approvals.decide(unknown, "approve", decider=capped)  # an amount the node does not know
    assert svc.approvals.decide(small, "approve", decider=capped, proof=PRESENT).state == "approved"
    assert svc.approvals.decide(big, "approve", decider=svc.owner_grant(), proof=PRESENT).state == "approved"  # the owner


async def test_routing_facts_are_recorded_on_the_approval(env):
    svc, _, clock = env
    ap, g = await pending(svc, lane="biz-c/insta", owner="m_va",
                          facts={"not_after": clock.t + 900, "amount_usd": "999"}, content="x")
    row = svc.approvals.get(ap)
    assert (row.workspace, row.requester_owner, row.target) == ("biz-c", "m_va", "tool:lane_post")
    assert row.not_after == clock.t + 900 and row.amount_usd is None  # only pay carries an amount
    pub = row.public()
    assert pub["workspace"] == "biz-c" and pub["requester_owner"] == "m_va" and pub["decided_via"] is None
    assert g.subject.owner == "m_va"


def test_old_approval_tables_gain_the_new_columns(tmp_path):
    db_path = tmp_path / "policy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE approvals (id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, effect TEXT, "
                 "tool TEXT, lane TEXT, domain TEXT, summary TEXT NOT NULL, content_hash TEXT, preview TEXT, "
                 "options TEXT, answer TEXT, reason TEXT, requester TEXT NOT NULL, grant_id TEXT, grant_path TEXT, "
                 "created_at REAL NOT NULL, expires_at REAL NOT NULL, decided_by TEXT, decided_at REAL, note TEXT, "
                 "consumed_at REAL)")
    conn.execute("INSERT INTO approvals (id, kind, state, summary, requester, created_at, expires_at) "
                 "VALUES ('ap_old', 'action', 'pending', 's', '{\"owner\": \"owner\"}', 0, 9e12)")
    conn.commit()
    conn.close()
    svc, _ = make_service(tmp_path)
    cols = {r[1] for r in svc.db.conn.execute("PRAGMA table_info(approvals)")}
    assert {"workspace", "requester_owner", "target", "amount_usd", "not_after", "decided_via"} <= cols
    assert svc.approvals.get("ap_old").workspace is None
    svc.close()


# ---------------------------------------------------------------- roles to grants

def test_roles_map_to_ceilings_and_specials(env):
    svc, _, _ = env
    viewer = svc.issue_member_grant("m_vi", "biz-c", "viewer")
    watcher = svc.issue_member_grant("m_wa", "biz-c", "viewer", {"watch": True})
    op = svc.issue_member_grant("m_op", "biz-c", "operator", {"max_session_s": 900})
    admin = svc.issue_member_grant("m_ad", "*", "admin", {"max_usd": 100})
    both = svc.issue_member_grant("m_bo", "biz-c", ["approver", "operator"])
    auditor = svc.issue_member_grant("m_au", "biz-a", "auditor")
    assert viewer.ceiling == Effect.READ and viewer.caps("approve") is None and viewer.caps("control") is None
    assert watcher.control_terms("biz-c/insta") == {"watch_only": True, "max_session_s": None}
    assert op.ceiling == Effect.COMMIT and op.caps("approve") is None
    assert op.control_terms("biz-c/insta") == {"watch_only": False, "max_session_s": 900}
    assert op.control_terms("biz-a/insta") is None
    assert admin.ceiling == Effect.IRREVERSIBLE and not admin.admin  # a workspace admin is never the owner
    assert admin.approve_problem("pay", "biz-q/x", amount_usd=99) is None
    assert admin.approve_problem("irreversible", "biz-q/x") is None
    assert both.ceiling == Effect.COMMIT and both.approve_problem("commit", "biz-c/x") is None
    assert both.control_terms("biz-c/x")["watch_only"] is False
    assert auditor.ceiling == Effect.READ and auditor.caps("approve") is None and auditor.caps("control") is None
    for g in (viewer, op, admin):
        assert g.subject.kind == "human" and g.subject.owner == g.subject.id and g.blocks[0].depth == 0


def test_member_options_are_checked(env):
    svc, _, _ = env
    with pytest.raises(InvalidRequest, match="owner role"):
        svc.issue_member_grant("m_va", "biz-c", "owner")
    with pytest.raises(InvalidRequest, match="role"):
        svc.issue_member_grant("m_va", "biz-c", "superuser")
    with pytest.raises(InvalidRequest, match="unknown member options"):
        svc.issue_member_grant("m_va", "biz-c", "approver", {"admin": True})
    with pytest.raises(InvalidRequest, match="not inside biz-c"):
        svc.issue_member_grant("m_va", "biz-c", "approver", {"lanes": ["biz-a/x"]})
    with pytest.raises(InvalidRequest, match="max_usd"):
        svc.issue_member_grant("m_va", "biz-c", "approver", {"max_usd": float("inf")})
    with pytest.raises(InvalidRequest, match="member id"):
        svc.issue_member_grant("owner", "biz-c", "approver")
    with pytest.raises(InvalidRequest, match="one workspace"):
        svc.issue_member_grant("m_va", "biz-*", "approver")
    lanes = svc.issue_member_grant("m_va", "biz-c", "approver", {"lanes": ["biz-c/insta"]})
    assert lanes.approve_problem("commit", "biz-c/insta") is None
    assert lanes.approve_problem("commit", "biz-c/linkedin") is not None


def test_a_role_change_replaces_the_grant_for_that_scope_only(env):
    svc, _, _ = env
    old = svc.issue_member_grant("m_va", "biz-c", "approver")
    elsewhere = svc.issue_member_grant("m_va", "biz-a", "viewer")
    new = svc.issue_member_grant("m_va", "biz-c", "viewer")
    with denied("revoked"):
        svc.grant_by_id(old.id)
    assert {g.id for g in svc.member_grants("m_va")} == {new.id, elsewhere.id}


def test_membership_expiry_is_the_grant_expiry(env):
    svc, _, clock = env
    g = svc.issue_member_grant("m_va", "biz-c", "approver", exp=clock.t + 90 * 86400)
    assert g.expires_at == int(clock.t) + 90 * 86400
    with pytest.raises(InvalidRequest, match="already ended"):
        svc.issue_member_grant("m_bo", "biz-c", "approver", exp=clock.t - 1)


# ---------------------------------------------------------------- separation of humans and agents

def _member_token(svc, workspace="biz-c"):
    """The token behind a member grant (never handed out by issue_member_grant)."""
    return svc.issue(subject=Subject(kind="human", id="m_tk", owner="m_tk"), workspaces=[workspace],
                     specials=Specials(approve=ApproveCap(effects=[Effect.COMMIT])), _member=True)


async def test_a_member_grant_is_never_a_bearer_or_an_mcp_caller(env):
    svc, rt, _ = env
    _, tok = _member_token(svc)
    with pytest.raises(TokenInvalid, match="not a bearer token"):
        svc.resolve(tok)
    assert svc.authenticate_panel(tok) is None
    assert svc.visible_tools(tok, SPECS) == []
    call = wrap_tool(SPECS[0], rt)
    with bind_request_token(tok), pytest.raises(Exception, match="invalid_token"):
        await call(on="biz-c/insta")
    with pytest.raises(InvalidRequest, match="person's grant"):
        svc.issue(subject={"id": "va", "kind": "human"}, workspaces=["biz-c"])


def test_a_member_grant_cannot_be_attenuated(env):
    svc, _, _ = env
    g, tok = _member_token(svc)
    with pytest.raises(InvalidRequest, match="cannot be narrowed"):
        svc.attenuate(tok, ceiling="read")
    with pytest.raises(InvalidRequest, match="cannot be narrowed"):
        svc.mint_child_token(g.principal(), purpose="brain", workspace="biz-c", lanes=["biz-c/insta"])
    # an offline narrowed block is not registered either
    child = svc._block(parent=g.id, version=g.version, specials=None, budget=None, ceiling=Effect.READ)
    offline = tokens.append(tok, child.to_data())
    with pytest.raises(TokenInvalid, match="cannot be narrowed"):
        svc.resolve(offline, allow_human=True)


def test_an_agent_grant_cannot_carry_approve_or_control(env):
    svc, _, _ = env
    with pytest.raises(InvalidRequest, match="person's grant"):
        svc.issue(subject={"id": "claude-code"}, workspaces=["biz-c"], specials={"approve": {"effects": ["commit"]}})
    with pytest.raises(InvalidRequest, match="person's grant"):
        svc.issue(subject={"id": "claude-code"}, workspaces=["biz-c"], specials={"control": {}})
    _, tok = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-c"], ceiling=Effect.COMMIT)
    with pytest.raises(InvalidRequest, match="specials.approve"):
        svc.attenuate(tok, specials={"approve": {"effects": ["commit"]}})
    # the owner token's child is a fresh agent grant: still no approve
    with pytest.raises(InvalidRequest, match="person's grant"):
        svc.attenuate(OWNER, subject={"id": "helper"}, specials={"approve": {"effects": ["commit"]}})
    # an offline block adding approve to an agent's chain is refused when the token is read
    parent = svc.resolve(tok)[1]
    block = svc._block(parent=parent.id, version=parent.version, budget=None,
                       specials={"approve": {"effects": ["commit"]}})
    with pytest.raises(TokenInvalid, match="approve and control"):
        svc.resolve(tokens.append(tok, block.to_data()))
    agent = Grant(parent.blocks)
    assert agent.approve_problem("commit", "biz-c/x") is not None


async def test_an_agent_grant_never_decides_and_a_family_never_decides_its_own(env):
    svc, _, _ = env
    ap, agent = await pending(svc)
    with denied("not_approver"):
        svc.approvals.decide(ap, "approve", decider=agent)
    with denied("not_approver"):
        svc.approvals.decide(ap, "deny", decider=agent)
    # an admin grant is the owner's delegate: it decides, but never for its own family
    _, admin_tok = svc.issue(subject={"id": "panel", "kind": "panel"}, workspaces=["*"], ceiling=Effect.COMMIT,
                             admin=True)
    admin = svc.resolve(admin_tok)[1]
    ctx = svc.context(admin, tool="lane_post")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-c/insta", content="mine", summary="Mine")
    with denied("self_approval"):
        svc.approvals.decide(e.value.approval_id, "approve", decider=admin)
    assert svc.approvals.decide(ap, "approve", decider=admin).state == "approved"


# ---------------------------------------------------------------- four-eyes

async def test_four_eyes_refuses_a_member_deciding_their_own_agents_request(tmp_path):
    clock = Clock()
    svc, _ = four_eyes_service(tmp_path, clock)
    va = svc.issue_member_grant("m_va", "biz-c", "approver", {"max_usd": 100})
    bo = svc.issue_member_grant("m_bo", "biz-c", "approver", {"max_usd": 100})
    ap, _ = await pending(svc, effect=Effect.PAY, owner="m_va", facts={"amount_usd": 10.0})
    assert not svc.approvals.can_decide(svc.approvals.get(ap), va)
    with denied("four_eyes"):
        svc.approvals.decide(ap, "approve", decider=va)
    assert svc.approvals.decide(ap, "deny", decider=va).state == "denied"  # denying is always safe
    ap2, _ = await pending(svc, effect=Effect.PAY, owner="m_va", facts={"amount_usd": 10.0}, content="again")
    assert svc.approvals.can_decide(svc.approvals.get(ap2), bo)
    assert svc.approvals.decide(ap2, "approve", decider=bo, proof=PRESENT).state == "approved"
    # commit is not under the rule: va decides their own agent's post
    ap3, _ = await pending(svc, owner="m_va", content="a post")
    assert svc.approvals.decide(ap3, "approve", decider=va).state == "approved"
    # the owner's own agent under four-eyes: the owner cannot approve it either (CLI path too)
    ap4, _ = await pending(svc, effect=Effect.PAY, facts={"amount_usd": 5.0}, content="owner's")
    with denied("four_eyes"):
        svc.approvals.decide(ap4, "approve", by="owner (cli)")
    assert svc.approvals.decide(ap4, "approve", decider=bo, proof=PRESENT).state == "approved"
    svc.close()


def test_four_eyes_loads_from_policy_toml(tmp_path):
    (tmp_path / "policy.toml").write_text('[approvals]\nfour_eyes = [{workspace = "biz-*", effects = ["pay"]}]\n')
    cfg = load_policy_config(tmp_path)
    assert cfg.approvals.four_eyes == [FourEyesRule(workspace="biz-*", effects=["pay"])]
    assert load_policy_config(tmp_path / "nowhere").approvals.four_eyes == []
    (tmp_path / "policy.toml").write_text('[approvals]\nfour_eyes = [{workspace = "biz-*", effects = ["post"]}]\n')
    with pytest.raises(ValueError):
        load_policy_config(tmp_path)


# ---------------------------------------------------------------- revocation and expiry

async def test_revoke_member_makes_the_next_decision_fail(env):
    svc, _, _ = env
    va = svc.issue_member_grant("m_va", "biz-c", "approver")
    svc.issue_member_grant("m_va", "biz-a", "approver")
    _, agent_tok = svc.issue(subject={"id": "va-agent"}, workspaces=["biz-c"], owner="m_va")
    ap, _ = await pending(svc)
    out = svc.revoke_member("m_va", reason="contract ended")
    assert len(out["grants"]) == 2 and len(out["agent_grants"]) == 1
    with denied("revoked"):
        svc.approvals.decide(ap, "approve", decider=va)
    with denied("revoked"):
        svc.decide(ap, "approve", member_id="m_va", via="passkey")
    with denied("revoked"):
        svc.resolve(agent_tok)  # their agents go with them
    with denied("revoked"):
        svc.issue_member_grant("m_va", "biz-c", "approver")
    assert svc.member_grants("m_va") == []
    with pytest.raises(InvalidRequest):
        svc.revoke_member("owner")
    assert svc.approvals.get(ap).state == "pending"


async def test_an_expired_member_grant_fails(env):
    svc, _, clock = env
    va = svc.issue_member_grant("m_va", "biz-c", "approver", exp=clock.t + 3600)
    ap, _ = await pending(svc)
    clock.advance(3600 + svc.cfg.clock_skew_s + 1)
    ap = (await pending(svc, content="later"))[0]
    with denied("expired"):
        svc.approvals.decide(ap, "approve", decider=va)
    with denied("expired"):
        svc.decide(ap, "approve", member_id="m_va")


async def test_decide_by_member_picks_the_grant_that_covers_it(env):
    svc, _, _ = env
    svc.issue_member_grant("m_va", "biz-a", "viewer")
    svc.issue_member_grant("m_va", "biz-c", "approver")
    ap, _ = await pending(svc, lane="biz-c/insta")
    done = svc.decide(ap, "approve", member_id="m_va", via="passkey", by="va")
    assert done.state == "approved" and done.decided_by == "va" and done.decided_via == "passkey"
    other, _ = await pending(svc, lane="biz-a/insta", content="o")
    with denied("not_approver"):
        svc.decide(other, "approve", member_id="m_va")
    with denied("not_approver"):
        svc.decide(other, "approve", member_id="m_nobody")


# ---------------------------------------------------------------- seeing approvals and questions

async def test_members_see_the_approvals_of_their_lanes(env):
    svc, _, _ = env
    viewer = svc.issue_member_grant("m_vi", "biz-c", "viewer")
    c1, _ = await pending(svc, lane="biz-c/insta", content="c")
    a1, _ = await pending(svc, lane="biz-a/insta", content="a")
    seen = {a.id for a in svc.approvals.list(viewer_grant=viewer)}
    assert seen == {c1}
    assert ApprovalStore.visible_to(svc.approvals.get(c1), viewer)
    assert not ApprovalStore.visible_to(svc.approvals.get(a1), viewer)
    with denied("not_approver"):
        svc.approvals.decide(c1, "approve", decider=viewer)
    with denied("not_approver"):
        svc.approvals.decide(a1, "deny", decider=viewer)
    assert svc.approvals.decide(c1, "deny", decider=viewer).state == "denied"  # anyone who sees it may deny
    assert len(svc.approvals.list(viewer_grant=svc.owner_grant())) == 2


async def test_questions_are_answered_by_members_of_their_workspace(env):
    svc, rt, _ = env
    from agentos.policy.tools import tool_specs

    _, tok = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-c"], ceiling=Effect.COMMIT)
    ask = wrap_tool(next(s for s in tool_specs(rt) if s.name == "human_ask"), rt)
    with bind_request_token(tok):
        q = (await ask(question="Which photo?", options=["a", "b"]))["approval_id"]
    assert svc.approvals.get(q).workspace == "biz-c"
    viewer = svc.issue_member_grant("m_vi", "biz-c", "viewer", {"watch": True})
    elsewhere = svc.issue_member_grant("m_el", "biz-a", "approver")
    op = svc.issue_member_grant("m_op", "biz-c", "operator")
    with denied("not_approver"):
        svc.approvals.decide(q, "approve", decider=viewer, answer="a")
    with denied("not_approver"):
        svc.approvals.decide(q, "approve", decider=elsewhere, answer="a")
    assert svc.approvals.decide(q, "approve", decider=op, answer="b").answer == "b"


# ---------------------------------------------------------------- the panel routes and co-signing

@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    yield n
    n.close()


def test_the_panel_routes_take_a_members_grant_with_approve(node):
    """A member signed in with a passkey on the mounted panel: an approver sees and decides the approvals
    of their workspace and no other, the owner's routes stay the owner's, and a revoked member is out."""
    boss = owner(node)
    va = join(node, boss, "va", [role("approver")])  # biz-a
    c1 = pending_on_node(node, "biz-a/instagram", content="c")
    a1 = pending_on_node(node, "biz-b/facebook", content="a")
    assert [a["id"] for a in va.get("/api/approvals").json()] == [c1]
    assert va.get(f"/api/approvals/{a1}").status_code == 404
    r = va.post(f"/api/approvals/{c1}/approve", {"note": "ok"})
    assert r.status_code == 200 and r.json()["state"] == "approved" and r.json()["decided_via"] == "passkey"
    assert r.json()["decided_by"] == "va"
    assert va.post(f"/api/approvals/{a1}/approve", {}).status_code == 404  # not theirs to see
    assert va.get("/api/grants").status_code == 403  # an approver connects no agents
    assert va.post("/api/grants", {"subject": "x", "workspaces": ["biz-a"]}).status_code == 403
    assert va.get("/api/settings/notifications").status_code == 403  # the owner's settings
    node.policy.revoke_member(va.member["id"], reason="contract ended")
    assert va.get("/api/approvals").status_code == 401


def test_cosign_in_person_only_with_approve(tmp_path):
    clock = Clock()
    svc, _ = four_eyes_service(tmp_path, clock)
    op = svc.issue_member_grant("m_op", "biz-c", "operator")
    ap = svc.issue_member_grant("m_ap", "biz-c", ["approver", "operator"], {"max_usd": 20})
    assert svc.owner_context(tool="panel").cosigns(Effect.PAY, lane_id="biz-c/x")
    assert not svc.member_context(op, tool="panel").cosigns(Effect.COMMIT, lane_id="biz-c/x")
    ctx = svc.member_context(ap.id, tool="panel")
    assert ctx.in_person and ctx.principal.kind == "human" and ctx.principal.id == "m_ap"
    assert ctx.cosigns(Effect.COMMIT, lane_id="biz-c/x")
    assert not ctx.cosigns(Effect.COMMIT, lane_id="biz-a/x")
    assert not ctx.cosigns(Effect.PAY, lane_id="biz-c/x", amount_usd=10)  # four-eyes: someone else decides
    agent_tok = svc.issue(subject={"id": "a"}, workspaces=["biz-c"])[1]
    assert not svc.context_for_token(agent_tok, tool="panel").cosigns(Effect.COMMIT, lane_id="biz-c/x")
    with pytest.raises(InvalidRequest):
        svc.member_context(svc.owner_grant())
    svc.close()


async def test_who_can_decide_a_new_approval(tmp_path):
    clock = Clock()
    svc, _ = four_eyes_service(tmp_path, clock)
    svc.issue_member_grant("m_va", "biz-c", "approver", {"max_usd": 100})
    svc.issue_member_grant("m_bo", "biz-c", "approver", {"max_usd": 5})
    svc.issue_member_grant("m_vi", "biz-c", "viewer")
    svc.issue_member_grant("m_ad", "*", "admin", {"max_usd": 1000})
    svc.issue_member_grant("m_gone", "biz-c", "approver")
    svc.revoke_member("m_gone")
    post, _ = await pending(svc, content="post")
    assert svc.members_who_can_decide(post) == ["m_ad", "m_bo", "m_va"]
    pay, _ = await pending(svc, effect=Effect.PAY, owner="m_va", facts={"amount_usd": 50.0}, content="pay")
    assert svc.members_who_can_decide(pay) == ["m_ad"]  # m_bo's limit is 5, m_va asked for it (four-eyes)
    assert "m_vi" in svc.members_who_can_decide(pay, "deny")
    elsewhere, _ = await pending(svc, lane="biz-a/x", content="a")
    assert svc.members_who_can_decide(elsewhere) == ["m_ad"]
    assert svc.member_ids() == ["m_ad", "m_bo", "m_va", "m_vi"]
    svc.close()
