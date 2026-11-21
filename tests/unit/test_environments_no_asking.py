"""Nothing asks a person inside an agent's own environment (the owner's decision of 2026-11-13,
docs/design/environments.md "No asking inside"). After the agent read a web page, a click in its
environment's browser that publishes goes through the commit gate without a question, and so does a
command on its terminal; a publish on an account lane after the same read still asks. The browser is
the dev node's fake site with the real commit gate and the real policy; the environment is a record
in the real environment manager."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from test_policy_support import Clock

from openajar.core.errors import ApprovalPending
from openajar.core.models import AccountRef, ActionKind, Effect, LaneSpec, utcnow
from openajar.core.runtime import Runtime
from openajar.dev.lanes import DevLaneManager
from openajar.dev.web import DevInteraction, new_tab
from openajar.environments.manager import EnvironmentManager
from openajar.environments.models import Environment, EnvSettings
from openajar.node import commitgate
from openajar.node import tools as node_tools
from openajar.policy.service import PolicyService
from openajar.policy.wrap import bind_request_token, wrap_tool
from openajar.runner.fakes import BASE

ENV_ID = "eabcdefghjk"
TERMINAL = f"env/{ENV_ID}"
BROWSER = f"env/{ENV_ID}-browser"
ACCOUNT = "biz-a/social"
SPECIALS = {"exec": {}, "install_overlay": {}, "service": {}}


class Node:
    def __init__(self, tmp_path):
        self.clock = Clock()
        self.lanes = DevLaneManager(make_page=new_tab)
        self.runtime = Runtime(interaction=DevInteraction())
        self.runtime.register("lanes", self.lanes)
        self.policy = PolicyService(tmp_path / "policy", runtime=self.runtime, clock=self.clock)
        self.runtime.register("policy", self.policy)
        self.runtime.register("approvals", self.policy.approvals)
        self.envs = EnvironmentManager(tmp_path / "home", runtime=self.runtime, settings=EnvSettings())
        self.runtime.register("environments", self.envs)
        self.grant, self.token = self.policy.issue(subject={"id": "agent-1"}, workspaces=["biz-a", "env"],
                                                   ceiling=Effect.COMMIT, specials=SPECIALS)
        now = utcnow()
        self.envs.store.put(Environment(id=ENV_ID, title="Work", state="ready", grant_id=self.grant.id,
                                        grant_root=self.grant.root_id, binding="session:chat-1", created_at=now,
                                        ready_at=now, expires_at=now + timedelta(hours=2)))
        self.refs: dict[str, str] = {}

    def ctx(self, lane: str, token: str | None = None):
        ctx = self.policy.context_for_token(token or self.token, tool="browser_act", lane_id=lane)
        ctx.mcp = True
        return ctx

    def page(self, lane: str):
        return self.lanes._page_for(lane)

    async def read_a_page(self, lane: str) -> None:
        """browser_navigate and browser_snapshot: the page's text labels the grant family untrusted."""
        await node_tools.browser_navigate(self.ctx(lane), on=lane, url=f"{BASE}/compose")
        text = await node_tools.browser_snapshot(self.ctx(lane), on=lane)
        self.refs = {name: ref for name, ref in re.findall(r'"([^"]+)" \[ref=(\w+)\]', text)}

    async def act(self, lane: str, name: str, action: ActionKind = ActionKind.CLICK, text: str | None = None):
        return await node_tools.browser_act(self.ctx(lane), on=lane, ref=self.refs[name], action=action, text=text)

    def untrusted(self) -> bool:
        return self.policy.read_untrusted(self.ctx(ACCOUNT))


@pytest.fixture
async def node(tmp_path):
    commitgate.ASKED.clear()
    n = Node(tmp_path)
    await n.lanes.create(LaneSpec(id=BROWSER))  # no account, like the browser lane the node makes
    await n.lanes.create(LaneSpec(id=ACCOUNT, account=AccountRef(site="social", handle="@biza.coffee")))
    yield n
    await n.lanes.shutdown()
    n.policy.close()


async def test_a_publish_in_the_environments_browser_after_a_page_read_does_not_ask(node):
    await node.read_a_page(BROWSER)
    assert node.untrusted()  # reading a page there still labels the family
    await node.act(BROWSER, "Write a caption...", ActionKind.TYPE, "Hello from the environment.")
    result = await node.act(BROWSER, "Share")
    assert result["ok"] is True and result.get("status") != "approval_pending"
    assert len(node.page(BROWSER).context.sent) == 1  # the gate let it through, as a commit
    assert node.policy.approvals.list(state="pending") == []


async def test_browser_act_through_the_mcp_wrapper_on_the_environment_does_not_ask(node):
    await node.read_a_page(BROWSER)
    await node.act(BROWSER, "Write a caption...", ActionKind.TYPE, "Through the wrapper.")
    spec = next(s for s in node_tools.tool_specs(node.runtime) if s.name == "browser_act")
    call = wrap_tool(spec, node.runtime)
    with bind_request_token(node.token, session="chat-1"):  # the session that holds it (its lease)
        out = await call(on=BROWSER, ref=node.refs["Share"], action=ActionKind.CLICK)
    assert out.get("status") != "approval_pending" and out["ok"] is True, out


async def test_a_post_on_an_account_lane_after_the_same_read_still_asks(node):
    await node.read_a_page(BROWSER)  # the page was read in the environment's browser
    ctx = node.ctx(ACCOUNT)
    with pytest.raises(ApprovalPending) as err:
        await ctx.authorize(Effect.COMMIT, lane_id=ACCOUNT, summary="Post on x", content={"post": "hi"})
    assert "untrusted" in (err.value.why or "")
    # the reason is the page it read: a clean grant's same commit runs on its own (Ask me first is off)
    _g, clean = node.policy.issue(subject={"id": "agent-2"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    d = await node.ctx(ACCOUNT, clean).authorize(Effect.COMMIT, lane_id=ACCOUNT, summary="Post on x",
                                                 content={"post": "hi"})
    assert d.route == "auto"


async def test_every_overlay_is_off_on_the_environments_own_lanes(node):
    """The routing table, Ask me first, the untrusted-content downgrade and the lethal trifecta: none
    of them asks on the family's own environment lanes. A deny would still deny; pay still asks."""
    ctx = node.ctx(BROWSER)
    ctx.taint("untrusted", "private", source="browser_snapshot", kind="web_page", host="evil.example")
    node.policy.ask_first.set("lane", BROWSER, True, by="owner", person=True)
    for lane in (BROWSER, TERMINAL):
        for eff in (Effect.DRAFT, Effect.COMMIT, Effect.EXEC, Effect.INSTALL_OVERLAY, Effect.SERVICE):
            d = await node.ctx(lane).authorize(eff, lane_id=lane, domain="evil.example", summary="x",
                                               content={"n": eff.value}, facts={"sink": True})
            assert d.route == "auto", (lane, eff)
    table = node.policy.cfg.routing
    asks = table.decide(Effect.PAY, {"environment": True})
    assert PolicyService._inside_environment(asks, Effect.PAY, {"environment": True}).then == "approval"
    # another family's environment lane is not found, never auto
    _g, other = node.policy.issue(subject={"id": "agent-3"}, workspaces=["env"], ceiling=Effect.COMMIT)
    with pytest.raises(Exception, match="no browser named"):
        await node.ctx(BROWSER, other).authorize(Effect.COMMIT, lane_id=BROWSER, summary="x")
    # a closed environment's lanes are lanes like any other: the usual rules hold
    node.envs.store.update(ENV_ID, lambda e: setattr(e, "state", "closed"))
    with pytest.raises(ApprovalPending):
        await node.ctx(BROWSER).authorize(Effect.COMMIT, lane_id=BROWSER, summary="x", content={"n": 1})
