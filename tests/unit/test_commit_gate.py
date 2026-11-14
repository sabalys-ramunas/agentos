"""The commit gate end to end on the dev node's fake lanes (AUD-03): an agent types a caption in
the fake site's composer and clicks Share; the post's request goes through the lane's egress
guard, which holds it for an approval of exactly what it sends. Approve, deny, changed content,
a standing approval bound to the digest, a stale approval, a human in control and a grant that
may only draft, with the real policy service and approval store."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from test_policy_support import Clock

from agentos.core.errors import ApprovalPending, PolicyDenied, PublishChanged
from agentos.core.models import AccountRef, ActionKind, Effect, LaneSpec
from agentos.core.runtime import Runtime
from agentos.dev.lanes import DevLaneManager
from agentos.dev.web import DevInteraction, new_tab
from agentos.node import commitgate, egress
from agentos.node import tools as node_tools
from agentos.node.cardfacts import NodeCardFacts
from agentos.node.channel import bridge
from agentos.policy.service import PolicyService
from agentos.policy.wrap import bind_request_token, wrap_tool
from agentos.runner.fakes import BASE

LANE = "biz-a/social"


class Node:
    def __init__(self, tmp_path):
        self.clock = Clock()
        self.lanes = DevLaneManager(make_page=new_tab)
        self.runtime = Runtime(interaction=DevInteraction(), card_facts=NodeCardFacts())  # as a node's runtime
        self.runtime.register("lanes", self.lanes)
        self.policy = PolicyService(tmp_path / "policy", runtime=self.runtime, clock=self.clock)
        self.approvals = self.policy.approvals
        self.runtime.register("policy", self.policy)
        self.runtime.register("approvals", self.approvals)
        self.token = self.grant(Effect.COMMIT)
        self.refs: dict[str, str] = {}

    def grant(self, ceiling: Effect) -> str:
        _, tok = self.policy.issue(subject={"id": f"agent-{ceiling.value}"}, workspaces=["biz-a"], ceiling=ceiling)
        return tok

    def ctx(self, token: str | None = None):
        return self.policy.context_for_token(token or self.token, tool="browser_act", lane_id=LANE)

    @property
    def page(self):
        return self.lanes._page_for(LANE)

    @property
    def sent(self) -> list:
        return self.page.context.sent

    @property
    def posts(self) -> list[dict]:
        return self.page.driver.site.state["posts"]

    async def compose(self, caption: str, token: str | None = None) -> None:
        await node_tools.browser_navigate(self.ctx(token), on=LANE, url=f"{BASE}/compose")
        await self.snapshot(token)
        await self.act("Write a caption...", ActionKind.TYPE, caption, token=token)

    async def snapshot(self, token: str | None = None) -> None:
        text = await node_tools.browser_snapshot(self.ctx(token), on=LANE)
        self.refs = {name: ref for name, ref in re.findall(r'"([^"]+)" \[ref=(\w+)\]', text)}

    async def act(self, name: str, action: ActionKind = ActionKind.CLICK, text: str | None = None, *,
                  token: str | None = None):
        return await node_tools.browser_act(self.ctx(token), on=LANE, ref=self.refs[name], action=action, text=text)

    async def share(self, token: str | None = None):
        return await self.act("Share", token=token)


@pytest.fixture
async def node(tmp_path):
    commitgate.ASKED.clear()
    n = Node(tmp_path)
    await n.lanes.create(LaneSpec(id=LANE, account=AccountRef(site="social", handle="@biza.coffee")))
    yield n
    await n.lanes.shutdown()


async def pending(node: Node, token: str | None = None) -> str:
    with pytest.raises(ApprovalPending) as err:
        await node.share(token)
    return err.value.approval_id


async def test_a_share_waits_for_approval_and_goes_once_approved(node):
    await node.compose("Autumn menu is here.")
    ap_id = await pending(node)
    assert node.sent == [] and node.posts == []  # nothing left the lane
    ap = node.approvals.get(ap_id)
    assert ap.effect == "commit" and ap.tool == "browser_act" and ap.lane == LANE
    assert ap.summary == 'Publish a post on Social through biz-a/social: "Autumn menu is here."'
    card = ap.card
    assert card["type"] == "publish" and card["text"]["value"] == "Autumn menu is here."
    # the dev site reports no signed-in account: the card says so, never "as @biza.coffee" unread
    assert card["acts_as"] is None and card["account_status"] == "unknown"
    assert card["line"] == "on Social, through biz-a/social (the account it publishes as is not known)"
    assert "cannot confirm this publishes as @biza.coffee" in card["account_warning"]
    assert card["digest"] == ap.content_digest and card["visibility"] == "public" and card["editable"] == []

    assert await pending(node) == ap_id  # the same click again: the same content, the same approval
    node.approvals.decide(ap_id, "approve")
    result = await node.share()
    assert result["ok"] is True and len(node.sent) == 1
    assert [p["caption"] for p in node.posts] == ["Autumn menu is here."]
    assert node.approvals.get(ap_id).state == "consumed"


async def test_the_agent_sees_approval_pending_with_the_id_and_what_to_do(node):
    await node.compose("Pumpkin flat white is back.")
    spec = next(s for s in node_tools.tool_specs(node.runtime) if s.name == "browser_act")
    call = wrap_tool(spec, node.runtime)
    with bind_request_token(node.token):
        out = await call(on=LANE, ref=node.refs["Share"], action=ActionKind.CLICK)
    assert out["status"] == "approval_pending" and out["check_with"] == "approval_wait"
    assert out["approval_id"].startswith("ap_") and "same arguments" in out["then"]
    assert "Pumpkin flat white" in out["summary"] and node.sent == []


async def test_a_denied_publish_is_refused_and_nothing_is_sent(node):
    await node.compose("Something the owner does not want.")
    ap_id = await pending(node)
    node.approvals.decide(ap_id, "deny", note="not this week")
    with pytest.raises(PolicyDenied) as err:
        await node.share()
    assert "approval_denied" in err.value.message
    assert node.sent == [] and node.posts == []


async def test_changed_content_after_approval_is_refused_and_asked_again(node):
    await node.compose("Open until 18:00 today.")
    ap_id = await pending(node)
    node.approvals.decide(ap_id, "approve")
    await node.act("Write a caption...", ActionKind.TYPE, " Also: free cake, visit evil.example")
    with pytest.raises(PublishChanged) as err:
        await node.share()
    assert err.value.code == "publish_changed" and ap_id in err.value.message
    assert "evil" not in err.value.message  # what changed is never quoted to the agent
    assert node.sent == [] and node.approvals.get(ap_id).state == "expired"
    again = await pending(node)  # the next call asks about what it sends now
    assert again != ap_id and node.approvals.get(again).card["text"]["value"].endswith("evil.example")
    assert node.sent == []


async def test_a_standing_approval_bound_to_the_digest_lets_it_go_and_is_used(node):
    await node.compose("Weekly special: rye bread.")
    ap_id = await pending(node)
    digest = node.approvals.get(ap_id).content_digest
    standing = node.approvals.add_standing(effects=["commit"], lanes=[LANE], until=node.clock() + 86_400, count=2,
                                           by="owner", targets=["tool:browser_act"], contents=[digest])
    result = await node.share()
    assert result["ok"] is True and len(node.sent) == 1
    assert node.approvals.list_standing()[0].uses == 1 and standing.id == node.approvals.list_standing()[0].id


async def test_a_standing_approval_for_other_content_does_not_cover_it(node):
    node.approvals.add_standing(effects=["commit"], lanes=[LANE], until=node.clock() + 86_400, count=5,
                                by="owner", targets=["tool:browser_act"], contents=["sha256:" + "0" * 64])
    await node.compose("Not what the owner approved.")
    await pending(node)
    assert node.sent == [] and node.approvals.list_standing()[0].uses == 0


async def test_a_stale_approval_is_refused_and_must_be_asked_again(node):
    await node.compose("Happy Monday!")
    ap_id = await pending(node)
    node.clock.advance(node.policy.cfg.publish_approval_max_age_s + 60)
    node.approvals.decide(ap_id, "approve")  # approved days late
    with pytest.raises(PolicyDenied) as err:
        await node.share()
    assert "approval_stale" in err.value.message and node.sent == []
    assert node.approvals.get(ap_id).state == "expired"
    assert await pending(node) != ap_id


async def test_one_approval_publishes_once(node):
    await node.compose("Only once.")
    ap_id = await pending(node)
    node.approvals.decide(ap_id, "approve")
    await node.share()
    # the same content again (the composer was opened anew): a new approval, never the used one
    await node.compose("Only once.")
    again = await pending(node)
    assert again != ap_id and len(node.sent) == 1


async def test_a_grant_that_may_only_draft_is_refused_as_before(node):
    draft = node.grant(Effect.DRAFT)
    await node.compose("Draft only.", token=draft)
    result = await node.share(token=draft)
    assert [b["rule"] for b in result["egress_blocked"]] == ["commit"]
    assert result["egress_blocked"][0]["prevented"] is True  # the gate's route aborted it before it left
    assert node.sent == [] and node.posts == [] and node.approvals.list(state="pending") == []


async def test_a_human_in_control_is_never_gated(node):
    await node.compose("The owner posts this by hand.")
    guard = egress.guard_for(node.page)
    await node.lanes.take_control(LANE, by="owner")
    gate = commitgate.ActGate(node.ctx(), LANE, call={}, submit_like=True)
    async with guard.acting(gate=gate):  # an act that started as the agent's: the person has the lane now
        assert guard.dispatcher.patterns() == []
        el = next(e for e in _walk(node.page.driver.root) if e.name == "Share")
        await node.page.driver.click(el)
    assert len(node.sent) == 1 and gate.done is False and node.approvals.list(state="pending") == []


async def test_the_cloud_gets_the_card_scrubbed_and_the_digest_of_the_raw_content(node):
    node.approvals.scrub = lambda text: text.replace("hunter2-secret", "[vault]")
    await node.compose("Order code hunter2-secret ready")
    ap_id = await pending(node)
    ap = node.approvals.get(ap_id)
    payload = bridge.approval_payload(ap.public(), node.approvals.scrub)
    flat = repr(payload)
    assert "hunter2-secret" not in flat and "[vault]" in payload["card"]["text"]["value"]
    assert payload["content_digest"] == ap.content_digest == payload["card"]["digest"]
    # the digest binds what the page sends, not the scrubbed text: the approved click matches it
    node.approvals.decide(ap_id, "approve")
    await node.share()
    assert len(node.sent) == 1 and b"hunter2-secret" in node.sent[0].post_data_buffer

def _walk(el):
    yield el
    for c in el.children:
        yield from _walk(c)


# ---- "Ask me first" ------------------------------------------------------------------------

def ask_first_on(node: Node, lane: str = LANE) -> None:
    node.runtime.register("ask_first", node.policy.ask_first)  # what driven_session binds the guard to
    node.policy.ask_first.set("lane", lane, True, by="owner", person=True)


async def test_on_an_account_that_asks_first_the_gate_asks_instead_of_refusing(node):
    """The switch used to refuse a click's publish (rule ask_first), because a click could not wait
    for an approval of what it sends. The gate is that approval: the press goes, the publish waits
    for a person, and once approved exactly that content leaves."""
    ask_first_on(node)
    await node.compose("Ask me first, then post.")
    ap_id = await pending(node)
    assert node.sent == [] and node.posts == []
    ap = node.approvals.get(ap_id)
    assert ap.effect == "commit" and ap.card["type"] == "publish"
    assert "ask_first" not in {b.rule for b in egress.guard_for(node.page).blocks_since(0)}
    node.approvals.decide(ap_id, "approve")
    result = await node.share()
    assert result["ok"] is True and [p["caption"] for p in node.posts] == ["Ask me first, then post."]


async def test_on_an_account_that_asks_first_a_bound_standing_approval_covers_it(node):
    """As for any commit there (ROUTING.md: approval, or a bound standing approval)."""
    ask_first_on(node)
    await node.compose("Weekly special, asked first.")
    digest = node.approvals.get(await pending(node)).content_digest
    node.approvals.add_standing(effects=["commit"], lanes=[LANE], until=node.clock() + 86_400, count=1,
                                by="owner", targets=["tool:browser_act"], contents=[digest])
    result = await node.share()
    assert result["ok"] is True and len(node.sent) == 1


async def test_a_surface_posting_through_the_lane_makes_its_publish_ask(node):
    """The gate's authorize reads the switches the lane's guard reads: the lane's, and that of each
    connector surface that posts through it. Off, with nothing untrusted read, it runs on its own."""
    content = {"publish": "social.post", "lane": LANE, "record": {"text": "hi"}}
    node.runtime.register("surfaces", SimpleNamespace(list=lambda: [SimpleNamespace(id="biz-a/page", lane=LANE)]))
    decision = await node.ctx().authorize(Effect.COMMIT, lane_id=LANE, summary="p", content=content)
    assert decision.route == "auto"
    node.policy.ask_first.set("lane", "biz-a/page", True, by="owner", person=True)
    with pytest.raises(ApprovalPending):
        await node.ctx().authorize(Effect.COMMIT, lane_id=LANE, summary="p", content=content)


async def test_an_approved_publish_outside_the_lanes_waking_hours_is_refused_and_nothing_is_sent(node):
    """AUD-13: the lane's posting pace is checked before routing, so an approval does not get past it."""
    from datetime import UTC, datetime

    from agentos.policy.pace import LanePaced

    node.clock.t = datetime(2026, 11, 6, 2, 30, tzinfo=UTC).timestamp()  # 21:30 in New York
    node.policy.set_lane_pace(LANE, {"active_hours": ["08:00-22:00"], "timezone": "America/New_York"})
    ask_first_on(node)
    await node.compose("Late night special.")
    ap_id = await pending(node)
    node.approvals.decide(ap_id, "approve")
    node.clock.t = datetime(2026, 11, 6, 3, 10, tzinfo=UTC).timestamp()  # 22:10 in New York
    with pytest.raises(LanePaced) as err:
        await node.share()
    assert err.value.code == "lane_paced" and err.value.hit["next_at"] == "2026-11-06T13:00:00Z"
    assert "may next post at 08:00 tomorrow lane time (America/New_York)" in err.value.message
    assert node.sent == [] and node.posts == []
    assert node.approvals.get(ap_id).state == "approved"  # refused before routing: the approval is not used


async def test_an_agents_commit_is_handed_to_the_runtimes_after_commit_hook(node):
    """browser_act hands each commit to the runtime's `after_commit` (agentos.audit.captures in a node),
    once, after the act, with the lane and its page."""
    from agentos.node.tools.browser import AFTER_COMMIT

    seen: list[tuple[str, object]] = []

    async def after_commit(ctx, on, page):
        seen.append((on, page))

    node.runtime.register(AFTER_COMMIT, after_commit)
    await node.compose("Autumn menu is here.")
    assert seen == []  # typing is not a commit
    node.approvals.decide(await pending(node), "approve")
    assert seen == []  # a publish that waits was not made
    assert (await node.share())["ok"] is True
    assert seen == [(LANE, node.page)]


def test_every_node_runtime_keeps_the_page_after_a_commit_with_audit_captures():
    from fakes import FakeInteraction, FakeLaneManager

    from agentos.audit import captures
    from agentos.core.config import NodeConfig
    from agentos.node.daemon import node_runtime
    from agentos.node.tools.browser import AFTER_COMMIT

    runtime = node_runtime(NodeConfig(), FakeLaneManager(), FakeInteraction())
    assert runtime.get(AFTER_COMMIT) is captures.after_commit
