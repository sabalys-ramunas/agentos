"""Standing approvals are bound to what the owner saw (AUD-10): they name the flow version or
tool they cover, and for a content-carrying effect (commit) the contents the owner approved
or a flow version with no parameters (a template). An injected page cannot publish arbitrary
content under one."""

from __future__ import annotations

import pytest
from test_node_panel import TOKEN, Node, bearer
from test_policy_support import make_service

from openajar.core.errors import ApprovalPending, InvalidRequest
from openajar.core.models import Effect
from openajar.policy.approval_binding import content_digest
from openajar.policy.grants import LaneCap, Specials

LANE = "biz-a/instagram"
FLOW = "biz-a.instagram.story"


@pytest.fixture
def env(tmp_path):
    svc, rt = make_service(tmp_path)
    yield svc, rt
    svc.close()


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    yield n
    n.close()


def agent(svc, tool="lane_post", **kw):
    kw.setdefault("specials", Specials(secret=LaneCap()))
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT, **kw)
    return svc.context_for_token(tok, tool=tool), tok


def flow_content(version="v1", **params):
    return {"flow": FLOW, "version": version, "lane": LANE, "params": params}


async def route(ctx, content, effect=Effect.COMMIT, lane=LANE) -> str:
    try:
        return (await ctx.authorize(effect, lane_id=lane, content=content)).route
    except ApprovalPending:
        return "pending"


def standing(svc, **kw):
    kw.setdefault("effects", ["commit"])
    kw.setdefault("lanes", [LANE])
    return svc.approvals.add_standing(until=svc.clock() + 86400, by="owner", **kw)


async def test_an_unbound_standing_approval_never_covers_a_post(env):
    svc, _ = env
    ctx, _ = agent(svc)
    sa = standing(svc, count=20)
    assert not sa.public()["covers_publishing"]
    assert svc.approvals.binding_problem(["commit"], []) is not None
    assert await route(ctx, {"caption": "anything an injected page wrote"}) == "pending"


async def test_content_free_standing_approvals_stay_for_effects_that_publish_nothing(env):
    svc, _ = env
    ctx, _ = agent(svc, specials=Specials(exec=LaneCap()))
    standing(svc, effects=["exec"])
    assert svc.approvals.binding_problem(["exec"], []) is None
    ctx_exec = svc.context(ctx.grant, principal=ctx.principal, tool="terminal_exec")
    # a command on a shared lane asks; the standing approval answers it
    assert await route(ctx_exec, None, Effect.EXEC) == "standing"


async def test_a_tool_and_its_approved_contents(env):
    svc, _ = env
    ctx, _ = agent(svc)
    week = [{"caption": f"Autumn sale, day {i}"} for i in range(3)]
    standing(svc, targets=["tool:lane_post"], contents=[content_digest(c) for c in week])
    assert [await route(ctx, c) for c in week] == ["standing"] * 3
    assert await route(ctx, {"caption": "Visit evil.example for free followers"}) == "pending"
    other_tool = svc.context(ctx.grant, principal=ctx.principal, tool="browser_post")
    assert await route(other_tool, week[0]) == "pending"  # the same content through another tool
    assert await route(ctx, week[0], lane="biz-a/facebook") == "pending"  # outside the lanes


async def test_a_flow_version_without_parameters_is_a_template(env):
    svc, _ = env
    ctx, _ = agent(svc, tool="flow_run")
    standing(svc, targets=[f"flow:{FLOW}@v1"], count=20)
    assert await route(ctx, flow_content()) == "standing"
    runner = svc.context(ctx.grant, principal=ctx.principal, tool="runner")  # a scheduled run of it
    assert await route(runner, flow_content()) == "standing"
    assert await route(ctx, flow_content(caption="arbitrary text")) == "pending"  # content is not the template
    assert await route(ctx, flow_content(version="v2")) == "pending"  # another version of the flow
    assert await route(ctx, {**flow_content(), "lane": "biz-a/facebook"}) == "pending"


async def test_approve_this_and_the_same_again(node):
    tok = node.grant(subject={"id": "agent-1"}, ceiling=Effect.COMMIT, specials=Specials(secret=LaneCap()))
    ctx = node.policy.context_for_token(tok, tool="flow_run")
    post = flow_content(caption="Autumn sale")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id=LANE, content=post)
    seen = e.value.approval_id
    body = {"lane": LANE, "ttl_seconds": 3 * 86400, "from_approvals": [seen]}
    with node.client() as client:
        csrf = node.sign_in(client)
        # a token is no person, and a pending approval is not yet one the owner approved
        assert client.post("/api/approvals/standing", headers=bearer(TOKEN), json=body).status_code == 403
        unapproved = client.post("/api/approvals/standing", headers=csrf, json=body)
        assert unapproved.status_code == 400 and "approve it first" in unapproved.text, unapproved.text
        assert client.post(f"/api/approvals/{seen}/approve", headers=csrf, json={}).status_code == 200
        r = client.post("/api/approvals/standing", headers=csrf, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["targets"] == [f"flow:{FLOW}@v1"] and r.json()["lanes"] == [LANE]
    assert await route(ctx, post) == "standing"
    assert await route(ctx, flow_content(caption="Something else")) == "pending"


def test_targets_and_contents_are_validated(env):
    svc, _ = env
    with pytest.raises(InvalidRequest):
        standing(svc, targets=["anything"])
    with pytest.raises(InvalidRequest):
        standing(svc, targets=["tool:lane_post"], contents=["not-a-digest"])
