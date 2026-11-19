"""The commit gate checks who a publish acts as (AUD-01 on the gate, agentos.node.acting,
docs/design/egress-guard.md 8.10): read per site from the request (the canary's recorded shapes,
tests/unit/test_publish_normalize.py), resolved to the lane's own account, a surface through the
lane, or an identity agentos does not know; then the policy: a surface's grant scope, switch and
pace; standing approvals that never cross identities; an unknown identity that always asks and is
refused for a caller that cannot ask (a schedule, a flow running on its own)."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode

import pytest
from test_policy_support import Clock
from test_publish_normalize import (
    facebook_comment,
    facebook_post,
    instagram_comment,
    linkedin,
    linkedin_post,
    reddit_post,
)

from agentos.core.errors import ApprovalPending, PolicyDenied
from agentos.core.models import AccountRef, ActionKind, ActsAs, Effect, LaneSpec, Surface
from agentos.core.runtime import Runtime
from agentos.dev.lanes import DevLaneManager
from agentos.dev.web import DevInteraction, new_tab
from agentos.node import acting, commitgate, egress, publish
from agentos.node import tools as node_tools
from agentos.node.cardfacts import NodeCardFacts
from agentos.policy.cards import publish_card
from agentos.policy.meters import LimitReached
from agentos.policy.service import PolicyService
from agentos.runner.fakes import BASE

ME = "61596476060059"            # the canary's Facebook profile (c_user, actor_id and av on its posts)
PAGE = "100089000000001"         # biz-a's Page, managed by that profile
OTHER_PAGE = "100089000000777"   # a Page of another lane
STRANGER = "100089999999999"     # a Page no surface names

FB_LANE, IG_LANE, LI_LANE = "personal/facebook", "personal/instagram", "personal/linkedin"
SURFACES = [
    Surface(id="biz-a/facebook-page", site="facebook", lane=FB_LANE,
            acts_as=ActsAs(kind="fb_page", id=PAGE, name="biz-a")),
    Surface(id="biz-b/facebook-page", site="facebook", lane="biz-b/facebook",
            acts_as=ActsAs(kind="fb_page", id=OTHER_PAGE, name="biz-b")),
    Surface(id="biz-a/linkedin-page", site="linkedin", lane=LI_LANE,
            acts_as=ActsAs(kind="li_company", id="76543210", name="biz-a")),
    Surface(id="biz-a/instagram", site="instagram", lane=IG_LANE,
            acts_as=ActsAs(kind="ig_business", id="17841400000000001", name="@biza")),
]
FB_PIN = AccountRef(site="facebook", handle="Arlo Fenwick", user_id=ME)


def rule(rule_id: str) -> egress.EffectRule:
    return next(r for r in egress.EFFECT_RULES if r.id == rule_id)


def cap(shape) -> publish.Capture:
    url, headers, body = shape
    found = egress.classify(url, "POST", headers, body=body)
    assert found is not None
    return publish.capture(found, url, "POST", headers, body)


def resolve(shape, lane: str, pinned=None, session=None):
    return acting.resolve(cap(shape).acting, lane=lane, pinned=pinned, session=session, surfaces=SURFACES)


def as_page(shape, actor: str | None, av: str | None = None, drop_av: bool = False):
    """A recorded Facebook shape with the actor set to `actor` (None: no actor_id) and `av` too."""
    url, headers, body = shape
    form = dict(parse_qsl(body.decode(), keep_blank_values=True))
    variables = json.loads(form["variables"])
    if actor is None:
        variables["input"].pop("actor_id", None)
    else:
        variables["input"]["actor_id"] = actor
    form["variables"] = json.dumps(variables)
    if drop_av:
        form.pop("av", None)
    elif av is not None:
        form["av"] = av
    return url, headers, urlencode(form).encode()


# ---- per site shape ------------------------------------------------------------------------

def test_facebook_personal_post_and_comment_are_the_lanes_own_account():
    for shape in (facebook_post(), facebook_comment()):
        acts = resolve(shape, FB_LANE, pinned=FB_PIN, session={"site": "facebook", "user_id": ME})
        assert acts["status"] == "self" and acts["id"] == ME and acts["field"] == "variables.input.actor_id"
        assert acts["verified"] is True and acts["label"] == "Arlo Fenwick (the browser's own account)"


def test_an_unpinned_lane_takes_the_signed_in_account_as_its_own():
    acts = resolve(facebook_post(), FB_LANE, session={"site": "facebook", "user_id": ME})
    assert acts["status"] == "self"


def test_facebook_page_post_resolves_to_the_surface_through_this_lane():
    acts = resolve(as_page(facebook_post(), PAGE, av=PAGE), FB_LANE, pinned=FB_PIN)
    assert acts["status"] == "surface" and acts["surface"] == "biz-a/facebook-page" and acts["kind"] == "fb_page"
    assert acts["label"] == "biz-a (Page)" and acts["through"] == FB_LANE
    assert acts["verified"] is True  # actor_id was recorded on a real account (a profile's, not yet a Page's)
    # the older Pages experience keeps the profile in `av`: actor_id decides, av is only a fallback
    assert resolve(as_page(facebook_post(), PAGE, av=ME), FB_LANE, pinned=FB_PIN)["status"] == "surface"


def test_facebook_identities_agentos_does_not_know():
    stranger = resolve(as_page(facebook_post(), STRANGER, av=STRANGER), FB_LANE, pinned=FB_PIN)
    assert stranger["status"] == "unrecognized" and stranger["id"] == STRANGER
    assert "no surface through personal/facebook names it" in stranger["why"]
    elsewhere = resolve(as_page(facebook_post(), OTHER_PAGE), FB_LANE, pinned=FB_PIN)
    assert elsewhere["status"] == "unrecognized" and "biz-b/facebook-page" in elsewhere["why"]
    assert "posts through biz-b/facebook, not this browser" in elsewhere["why"]
    # a Page through this lane, posted from another lane, is not that lane's surface either
    assert resolve(as_page(facebook_post(), PAGE), "biz-b/facebook")["status"] == "unrecognized"
    # nothing names who it acts as, on a site where a login can post as a Page: never the lane's by default
    unread = resolve(as_page(facebook_post(), None, drop_av=True), FB_LANE, pinned=FB_PIN)
    assert unread["status"] == "unrecognized" and unread["id"] is None
    assert unread["label"] == "an identity agentos cannot read"


def test_facebook_fields_that_disagree_are_unrecognized():
    url, headers, body = as_page(facebook_post(), ME)
    form = dict(parse_qsl(body.decode(), keep_blank_values=True))
    variables = json.loads(form["variables"])
    variables["input"]["page_id"] = PAGE
    form["variables"] = json.dumps(variables)
    acts = resolve((url, headers, urlencode(form).encode()), FB_LANE, pinned=FB_PIN)
    assert acts["status"] == "unrecognized" and "disagree" in acts["why"]


def test_linkedin_member_post_company_post_and_a_stray_company():
    own = resolve(linkedin_post(), LI_LANE)  # the recorded CreatePost names no author: the member's own
    assert own["status"] == "self" and own["id"] is None and "names no company page" in own["label"]
    company = resolve(linkedin("com.linkedin.sdui.requests.sharing.CreatePost",
                               {"commentary": "Hiring", "visibility": "ANYONE",
                                "nonMemberActorUrn": "urn:li:fsd_company:76543210"}), LI_LANE)
    assert company["status"] == "surface" and company["surface"] == "biz-a/linkedin-page"
    assert company["verified"] is False  # no company post was recorded
    member = resolve(linkedin("com.linkedin.sdui.requests.sharing.CreatePost",
                              {"commentary": "Hi", "author": "urn:li:fsd_profile:ACoAAB12"}), LI_LANE)
    assert member["status"] == "self"
    stray = resolve(linkedin("com.linkedin.sdui.requests.sharing.CreatePost",
                             {"commentary": "Thanks", "attributes": [{"entity": "urn:li:organization:5550001"}]}),
                    LI_LANE)
    assert stray["status"] == "unrecognized" and "cannot tell whether it posts as it" in stray["why"]
    unknown = resolve(linkedin("com.linkedin.sdui.requests.sharing.CreatePost",
                               {"commentary": "x", "author": "urn:li:organization:999"}), LI_LANE)
    assert unknown["status"] == "unrecognized" and unknown["id"] == "urn:li:organization:999"


def test_instagram_is_its_own_login_unless_the_body_names_a_business_account():
    assert resolve(instagram_comment(), IG_LANE) is None  # names nobody: nothing changes
    url, headers, body = instagram_comment()
    form = dict(parse_qsl(body.decode(), keep_blank_values=True))
    variables = json.loads(form["variables"])
    variables["data"]["actor_id"] = "17841400000000001"
    form["variables"] = json.dumps(variables)
    shape = (url, headers, urlencode(form).encode())
    assert resolve(shape, IG_LANE)["status"] == "surface"
    form["av"] = "17841400000000001"
    variables["data"]["actor_id"] = "123"
    form["variables"] = json.dumps(variables)
    assert resolve((url, headers, urlencode(form).encode()), IG_LANE)["status"] == "unrecognized"


def test_medium_into_a_publication_is_unrecognized_and_its_own_post_is_not_read():
    url = "https://medium.com/_/graphql"
    headers = {"content-type": "application/json", "graphql-operation": "PublishPost"}
    own = json.dumps({"operationName": "PublishPost", "variables": {"postId": "abc", "title": "t"}}).encode()
    assert resolve((url, headers, own), "personal/medium") is None
    into = json.dumps({"operationName": "PublishPost", "variables": {"postId": "abc", "publicationId": "pub9"}}).encode()
    acts = resolve((url, headers, into), "personal/medium")
    assert acts["status"] == "unrecognized" and "publication pub9" in acts["why"]


def test_reddit_and_unknown_sites_change_nothing():
    assert cap(reddit_post()).acting.applies is False
    assert resolve(reddit_post(), "personal/reddit") is None


def test_every_site_that_reads_an_identity_marks_which_field_names_were_seen_on_a_real_account():
    verified = {(site, f.path) for site, spec in acting.ACTING.items() for f in spec.fields if f.verified}
    assert verified == {("facebook", "variables.input.actor_id"), ("facebook", "av")}


# ---- the card ------------------------------------------------------------------------------

class CardRuntime:
    def get(self, name):
        if name == "lanes":
            return SimpleNamespace(get=lambda lane: SimpleNamespace(spec=SimpleNamespace(account=FB_PIN)))
        if name == "card_facts":  # what a node runtime always carries (agentos.node.cardfacts)
            return NodeCardFacts()
        return None


def test_the_card_names_the_surface_and_the_login_it_posts_through():
    c = cap(as_page(facebook_post(), PAGE, av=PAGE))
    c.record["acts_as"] = acting.resolve(c.acting, lane=FB_LANE, pinned=FB_PIN, surfaces=SURFACES)
    card = publish_card(c.content("biz-a/facebook-page"), CardRuntime())
    assert card["identity_line"] == "Posts as biz-a (Page)" and card["acts_as"] == "biz-a (Page)"
    assert card["surface"] == "biz-a/facebook-page" and card["through"] == FB_LANE
    assert card["line"] == "as biz-a (Page) on Facebook, through personal/facebook"
    assert card["account"]["user_id"] == ME  # the login's pin, read from the lane it posts through
    assert "identity_warning" not in card


def test_the_card_says_plainly_when_the_identity_is_unknown():
    c = cap(as_page(facebook_post(), STRANGER))
    c.record["acts_as"] = acting.resolve(c.acting, lane=FB_LANE, pinned=FB_PIN, surfaces=SURFACES)
    card = publish_card(c.content(FB_LANE), CardRuntime())
    assert card["acts_as"] is None and card["acting"]["status"] == "unrecognized"
    assert card["identity_line"] == f"Posts as an identity agentos does not know ({STRANGER}): no surface names it"
    assert STRANGER in card["identity_warning"] and "never automatic" in card["identity_warning"]
    assert "—" not in json.dumps(card)
    assert commitgate.summary(c, FB_LANE).startswith(
        f"Publish a post on Facebook as an identity agentos does not know ({STRANGER}) through personal/facebook")


# ---- the gate end to end (the dev node, its composer posting as a Page) --------------------

LANE = "personal/social"
SURFACE = "biz-a/social-page"
DEV_PAGE = "100089000000042"
DEV_ME = "me-1"


@pytest.fixture
def social(monkeypatch):
    """The dev site, read as a site whose login can post as a Page (as Facebook)."""
    monkeypatch.setitem(acting.ACTING, "social", acting.ActingSpec(
        "social", True, "fb_page", (acting.ActingField("variables.input.actor_id", "actor"),)))


class Surfaces:
    def __init__(self, items):
        self.items = items

    def list(self, **_):
        return list(self.items)

    def find(self, surface_id):
        return next((s for s in self.items if s.id == surface_id), None)


class Node:
    def __init__(self, tmp_path):
        self.clock = Clock()
        self.lanes = DevLaneManager(make_page=new_tab)
        self.runtime = Runtime(interaction=DevInteraction(), card_facts=NodeCardFacts())
        self.runtime.register("lanes", self.lanes)
        self.policy = PolicyService(tmp_path / "policy", runtime=self.runtime, clock=self.clock)
        self.approvals = self.policy.approvals
        self.runtime.register("policy", self.policy)
        self.runtime.register("approvals", self.approvals)
        self.runtime.register("surfaces", Surfaces([
            Surface(id=SURFACE, site="social", lane=LANE, acts_as=ActsAs(kind="fb_page", id=DEV_PAGE, name="biz-a"))]))
        self.runtime.register("ask_first", self.policy.ask_first)
        self.refs: dict[str, str] = {}
        self.grants: dict[str, str] = {}

    def grant(self, workspaces: list[str], kind: str = "agent") -> str:
        g, tok = self.policy.issue(subject={"id": f"agent-{'-'.join(workspaces)}", "kind": kind},
                                   workspaces=workspaces, ceiling=Effect.COMMIT)
        self.grants[tok] = g.id
        return tok

    def ctx(self, token: str):
        return self.policy.context_for_token(token, tool="browser_act", lane_id=LANE)

    @property
    def page(self):
        return self.lanes._page_for(LANE)

    @property
    def sent(self) -> list:
        return self.page.context.sent

    def acting_as(self, actor: str) -> None:
        self.page.driver.site.state["actor"] = actor

    async def compose(self, token: str, caption: str) -> None:
        await node_tools.browser_navigate(self.ctx(token), on=LANE, url=f"{BASE}/compose")
        text = await node_tools.browser_snapshot(self.ctx(token), on=LANE)
        self.refs = {name: ref for name, ref in re.findall(r'"([^"]+)" \[ref=(\w+)\]', text)}
        await node_tools.browser_act(self.ctx(token), on=LANE, ref=self.refs["Write a caption..."],
                                     action=ActionKind.TYPE, text=caption)
        # the owner starts the grant clean: the dev page counts as untrusted text, which would make every
        # publish ask (ROUTING.md, after untrusted content) and hide what the identity decides
        self.policy.clear_taint(self.grants[token])

    async def share(self, token: str):
        return await node_tools.browser_act(self.ctx(token), on=LANE, ref=self.refs["Share"], action=ActionKind.CLICK)

    async def pending(self, token: str) -> str:
        with pytest.raises(ApprovalPending) as err:
            await self.share(token)
        return err.value.approval_id

    def commit_rows(self) -> list[dict]:
        return [e.model_dump() for e in self.policy.traces.query(kind="commit_gate")[0]]


@pytest.fixture
async def node(tmp_path, social):
    commitgate.ASKED.clear()
    n = Node(tmp_path)
    await n.lanes.create(LaneSpec(id=LANE, account=AccountRef(site="social", handle="me", user_id=DEV_ME)))
    yield n
    await n.lanes.shutdown()


async def test_a_post_as_the_lanes_own_account_runs_as_before(node):
    tok = node.grant(["personal"])
    await node.compose(tok, "Own post.")
    node.acting_as(DEV_ME)
    assert (await node.share(tok))["ok"] is True and len(node.sent) == 1


async def test_a_personal_grant_posting_as_the_page_of_another_workspace_is_refused(node):
    tok = node.grant(["personal"])
    await node.compose(tok, "As the Page.")
    node.acting_as(DEV_PAGE)
    with pytest.raises(PolicyDenied) as err:
        await node.share(tok)
    assert "out_of_scope" in err.value.message and "biz-a (Page)" in err.value.message
    assert SURFACE in err.value.message and node.sent == []


async def test_a_grant_that_also_covers_the_pages_workspace_posts_as_the_page(node):
    tok = node.grant(["personal", "biz-a"])
    await node.compose(tok, "As the Page.")
    node.acting_as(DEV_PAGE)
    assert (await node.share(tok))["ok"] is True and len(node.sent) == 1
    row = next(r for r in node.commit_rows() if r["status"] == "ok")
    assert row["args"]["acts_as"]["surface"] == SURFACE and row["args"]["acts_as"]["status"] == "surface"


async def test_the_surfaces_ask_me_first_holds_its_posts_and_names_the_page(node):
    tok = node.grant(["personal", "biz-a"])
    node.policy.ask_first.set("lane", SURFACE, True, by="owner", person=True)
    await node.compose(tok, "Page post, asked first.")
    node.acting_as(DEV_PAGE)
    ap_id = await node.pending(tok)
    ap = node.approvals.get(ap_id)
    assert ap.lane == SURFACE and ap.card["identity_line"] == "Posts as biz-a (Page)"
    assert ap.card["through"] == LANE and "as biz-a (Page)" in ap.summary and node.sent == []
    node.approvals.decide(ap_id, "approve")
    assert (await node.share(tok))["ok"] is True and len(node.sent) == 1


async def test_a_pages_switch_no_longer_holds_the_logins_own_posts(node):
    """The gate can tell who a post acts as, so a surface's switch holds that surface's posts only."""
    tok = node.grant(["personal", "biz-a"])
    node.policy.ask_first.set("lane", SURFACE, True, by="owner", person=True)
    await node.compose(tok, "Own post.")
    node.acting_as(DEV_ME)
    assert (await node.share(tok))["ok"] is True


async def test_a_standing_approval_never_covers_a_publish_as_another_identity(node):
    tok = node.grant(["personal", "biz-a"])
    node.policy.ask_first.set("lane", LANE, True, by="owner", person=True)
    node.policy.ask_first.set("lane", SURFACE, True, by="owner", person=True)
    await node.compose(tok, "Weekly special.")
    node.acting_as(DEV_ME)
    own = node.approvals.get(await node.pending(tok))
    node.approvals.add_standing(effects=["commit"], workspaces=["personal"], until=node.clock() + 86_400, count=5,
                                by="owner", targets=["tool:browser_act"], contents=[own.content_digest])
    node.acting_as(DEV_PAGE)  # the same caption, as the Page: other content, another surface
    page_ap = node.approvals.get(await node.pending(tok))
    assert page_ap.lane == SURFACE and node.approvals.list_standing()[0].uses == 0 and node.sent == []
    # and one given for the Page's post covers that post, as the Page only
    node.approvals.add_standing(effects=["commit"], lanes=[SURFACE], until=node.clock() + 86_400, count=5,
                                by="owner", targets=["tool:browser_act"], contents=[page_ap.content_digest])
    assert (await node.share(tok))["ok"] is True and len(node.sent) == 1


async def test_an_unknown_identity_always_asks_and_no_standing_approval_covers_it(node):
    tok = node.grant(["personal", "biz-a"])  # Ask me first is off everywhere: a known identity would just post
    await node.compose(tok, "As a stranger Page.")
    node.acting_as("100089999999999")
    ap_id = await node.pending(tok)
    ap = node.approvals.get(ap_id)
    assert ap.lane == LANE and "an identity agentos does not know (100089999999999)" in ap.summary
    assert ap.card["acting"]["status"] == "unrecognized" and "100089999999999" in ap.card["identity_warning"]
    node.approvals.add_standing(effects=["commit"], lanes=[LANE], until=node.clock() + 86_400, count=5,
                                by="owner", targets=["tool:browser_act"], contents=[ap.content_digest])
    assert await node.pending(tok) == ap_id and node.approvals.list_standing()[0].uses == 0 and node.sent == []
    node.approvals.decide(ap_id, "approve")  # a person who knows who that is
    assert (await node.share(tok))["ok"] is True and len(node.sent) == 1


@pytest.mark.parametrize(("actor", "takes_chat"), [(DEV_ME, True), ("100089999999999", False)])
async def test_a_chat_yes_never_covers_a_publish_as_an_unknown_identity(node, monkeypatch, actor, takes_chat):
    """A publish after untrusted text may be approved in chat (docs/CONFIRMATIONS.md), but a chat yes
    proves no person saw it, and an unknown identity is there for a person to see."""
    monkeypatch.setattr(node.policy, "clear_taint", lambda grant_id: None)  # the page's text stays on the grant
    # chat for a publish after untrusted text is off by default now; the owner turned it on here
    node.policy.confirmation.change({"publish_in_chat": True}, by="owner", role="owner")
    tok = node.grant(["personal", "biz-a"])
    await node.compose(tok, "After reading a page.")
    node.acting_as(actor)
    ap = node.approvals.get(await node.pending(tok))
    assert ("chat" in ap.methods) is takes_chat


async def test_a_schedule_or_flow_grant_is_refused_an_unknown_identity(node):
    tok = node.grant(["personal", "biz-a"], kind="flow")
    await node.compose(tok, "Scheduled post.")
    node.acting_as("100089999999999")
    with pytest.raises(PolicyDenied) as err:
        await node.share(tok)
    assert "identity_unrecognized" in err.value.message and "cannot ask a person" in err.value.message
    assert node.sent == [] and node.approvals.list(state="pending") == []
    node.acting_as(DEV_PAGE)  # a known surface is fine unattended (its switch is off)
    assert (await node.share(tok))["ok"] is True


async def test_the_account_pin_is_checked_first(node, monkeypatch):
    from agentos.node import sites

    async def other(context, which, url=None):
        return {"site": "social", "user_id": "someone-else", "source": "cookie"}

    monkeypatch.setattr(sites, "account_identity", other)
    node.page.context.cookies = lambda *a, **k: []
    tok = node.grant(["personal", "biz-a"])
    await node.compose(tok, "Wrong login.")
    node.acting_as(DEV_PAGE)
    with pytest.raises(PolicyDenied) as err:
        await node.share(tok)
    assert "wrong_account" in err.value.message and "someone-else" in err.value.message and node.sent == []


async def test_a_flow_running_on_its_own_is_refused_an_unknown_identity_and_a_known_one_goes(node):
    """The flow driver's check (commitgate.flow_identity_check) on the lane's guard: a flow's own
    publish is authorized with its run, so only who it posts as is judged, and nobody is there to ask."""
    from agentos.dev.net import publish_request

    guard = egress.guard_for(node.page)
    page = node.page
    check = commitgate.flow_identity_check(node.runtime, LANE, guard)
    assert check is not None
    async with guard.flow(page), guard.flow_identity(check):
        assert not await page.context.send(publish_request("from a flow", [], actor="100089999999999"))
        assert await page.context.send(publish_request("from a flow", [], actor=DEV_PAGE))
        assert await page.context.send(publish_request("from a flow", [], actor=DEV_ME))
    assert [b.rule for b in guard.blocks_since(0)] == ["identity_unrecognized"]
    assert guard.dispatcher.patterns() == []  # the route went with the flow
    assert len(node.sent) == 2


async def test_a_flows_identity_check_and_its_declared_commit_share_one_route(node):
    """Built apart (who a publish acts as, and the one request a connector run may commit with), both
    bind the gate's route on one lane: each still refuses what it refuses, and the route goes with both."""
    from agentos.dev.net import publish_request

    guard = egress.guard_for(node.page)
    page = node.page
    check = commitgate.flow_identity_check(node.runtime, LANE, guard)
    allowed = publish_request("from a flow", [], actor=DEV_PAGE)
    declared = egress.FlowCommit.of("connector.post", {"method": "POST", "url": allowed.url})
    async with guard.flow(page), guard.flow_identity(check):
        await guard.scope_flow(declared)
        assert await page.context.send(publish_request("from a flow", [], actor=DEV_PAGE))
        assert not await page.context.send(publish_request("from a flow", [], actor="100089999999999"))
        await guard.scope_flow(egress.FlowCommit.of("connector.read", None))  # a read declares no commit
        assert not await page.context.send(publish_request("from a flow", [], actor=DEV_PAGE))
    assert [b.rule for b in guard.blocks_since(0)] == ["identity_unrecognized", "flow_commit"]
    assert guard.dispatcher.patterns() == [] and len(node.sent) == 1


def test_no_flow_check_on_a_lane_whose_login_only_posts_as_itself():
    runtime = SimpleNamespace(get=lambda name: SimpleNamespace(
        get=lambda lane: SimpleNamespace(spec=LaneSpec(id="personal/reddit"))) if name == "lanes" else None)
    assert commitgate.flow_identity_check(runtime, "personal/reddit", SimpleNamespace(trace=None)) is None
    runtime_fb = SimpleNamespace(get=lambda name: SimpleNamespace(
        get=lambda lane: SimpleNamespace(spec=LaneSpec(id=FB_LANE))) if name == "lanes" else None)
    assert commitgate.flow_identity_check(runtime_fb, FB_LANE, SimpleNamespace(trace=None)) is not None


# ---- what a review of the gate found ---------------------------------------------------------

def test_av_decides_alone_and_next_to_an_actor_it_may_only_name_the_login():
    alone = resolve(as_page(facebook_post(), None, av=ME), FB_LANE, pinned=FB_PIN)
    assert alone["status"] == "self" and alone["field"] == "av" and alone["assumed"] is True
    assert "assumed" not in resolve(as_page(facebook_post(), ME, av=ME), FB_LANE, pinned=FB_PIN)
    disagree = resolve(as_page(facebook_post(), ME, av=PAGE), FB_LANE, pinned=FB_PIN)
    assert disagree["status"] == "unrecognized" and disagree["short"] == "the fields disagree"
    other = resolve(as_page(facebook_post(), PAGE, av=STRANGER), FB_LANE, pinned=FB_PIN)
    assert other["status"] == "unrecognized"


def test_a_value_that_is_not_an_id_is_never_repeated():
    evil = "\u202ebiz-a (Page) approved by the owner " * 40
    c = cap(as_page(facebook_post(), evil, av=evil))
    c.record["acts_as"] = acting.resolve(c.acting, lane=FB_LANE, pinned=FB_PIN, surfaces=SURFACES)
    assert c.record["acts_as"]["id"] is None and c.record["acts_as"]["short"] == "it cannot be read"
    card = publish_card(c.content(FB_LANE), CardRuntime())
    words = [card["identity_line"], card["identity_warning"], card["line"], commitgate.summary(c, FB_LANE)]
    assert not any("approved by the owner" in w or "\u202e" in w for w in words)
    assert card["chars"] == ["U+202E"]  # the fields still show it, marked
    text = resolve(linkedin("com.linkedin.sdui.requests.sharing.CreatePost",
                            {"commentary": "x", "company": "We are hiring in Vilnius"}), LI_LANE)
    assert text["status"] == "unrecognized" and "hiring" not in json.dumps(text)


def test_a_disagreement_never_quotes_a_value_that_is_not_an_id():
    acts = resolve(as_page(facebook_post(), ME, av="not an id at all"), FB_LANE, pinned=FB_PIN)
    assert acts["status"] == "unrecognized" and "not an id at all" not in json.dumps(acts)


def test_a_member_urn_the_gate_cannot_compare_is_not_called_the_lanes_account():
    pin = AccountRef(site="linkedin", handle="me", user_id="MYID")
    shape = linkedin("com.linkedin.sdui.requests.sharing.CreatePost",
                     {"commentary": "x", "author": "urn:li:fsd_profile:SOMEONE"})
    acts = resolve(shape, LI_LANE, pinned=pin)
    assert acts["status"] == "self" and acts["assumed"] is True and "lane's own" not in acts["label"]
    mine = linkedin("com.linkedin.sdui.requests.sharing.CreatePost",
                    {"commentary": "x", "author": "urn:li:fsd_profile:MYID"})
    assert resolve(mine, LI_LANE, pinned=pin)["label"] == "me (the browser's own account)"


@pytest.mark.parametrize(("acts", "line"), [
    ({"status": "unrecognized", "id": "1", "short": "the fields disagree"},
     "Posts as an identity agentos does not know (1): the fields disagree"),
    ({"status": "unrecognized", "id": None, "short": "the request names none"},
     "Posts as an identity agentos does not know: the request names none"),
])
def test_the_identity_line_gives_the_real_reason(acts, line):
    assert acting.card_line(acts) == line


async def test_a_page_post_counts_against_the_limits_of_the_lane_it_goes_through(node):
    tok = node.grant(["personal", "biz-a"])
    node.policy.set_lane_limits(LANE, {"commits_per_day": 1})
    await node.compose(tok, "First.")
    node.acting_as(DEV_PAGE)
    assert (await node.share(tok))["ok"] is True
    await node.compose(tok, "Second.")
    node.acting_as(DEV_PAGE)
    with pytest.raises(LimitReached):
        await node.share(tok)
    assert len(node.sent) == 1
    await node.compose(tok, "Third, as the login.")  # one lane, one limit, whoever it posts as
    node.acting_as(DEV_ME)
    with pytest.raises(LimitReached):
        await node.share(tok)


async def test_a_surfaces_switch_holds_a_post_the_gate_only_assumed_is_the_logins(node, monkeypatch):
    monkeypatch.setitem(acting.ACTING, "social", acting.ActingSpec(
        "social", True, "fb_page", (acting.ActingField("variables.input.not_listed", "actor"),), default_self=True))
    tok = node.grant(["personal", "biz-a"])
    node.policy.ask_first.set("lane", SURFACE, True, by="owner", person=True)
    await node.compose(tok, "A company post through a field the table does not list.")
    node.acting_as(DEV_PAGE)
    await node.pending(tok)
    assert node.sent == []


async def test_an_account_surface_with_its_own_id_is_authorized_as_that_surface(node):
    surface = "biz-a/social-own"
    node.runtime.get("surfaces").items.append(Surface(
        id=surface, site="social", lane=LANE, acts_as=ActsAs(kind="account", user_id=DEV_ME, name="biz-a")))
    tok = node.grant(["personal"])
    await node.compose(tok, "Outside the grant.")
    node.acting_as(DEV_ME)
    with pytest.raises(PolicyDenied) as err:
        await node.share(tok)
    assert "out_of_scope" in err.value.message and node.sent == []
    both = node.grant(["personal", "biz-a"])
    node.policy.ask_first.set("lane", surface, True, by="owner", person=True)
    await node.compose(both, "Its switch holds it.")
    node.acting_as(DEV_ME)
    assert node.approvals.get(await node.pending(both)).lane == surface and node.sent == []


async def agent_drive(guard) -> None:
    guard.drive(egress.Driver(kind="agent", id="a", grant_id="g", ceiling="commit"))
    await guard._begin_drive(commitgate.ActGate(SimpleNamespace(), LANE, call={}, submit_like=True), None)


async def test_a_drive_that_ends_under_a_flow_leaves_its_route_to_the_flow(node):
    from agentos.dev.net import publish_request

    guard, page = egress.guard_for(node.page), node.page
    check = commitgate.flow_identity_check(node.runtime, LANE, guard)
    await agent_drive(guard)
    async with guard.flow(page), guard.flow_identity(check):
        guard.end_drive("the claim ended")
        await guard._settle_removal()
        assert len(guard.dispatcher.patterns()) == 1
        assert not await page.context.send(publish_request("x", [], actor="100089999999999"))
    assert guard.dispatcher.patterns() == [] and node.sent == []


async def test_a_drive_that_begins_under_a_flow_takes_its_route_over(node):
    guard, page = egress.guard_for(node.page), node.page
    check = commitgate.flow_identity_check(node.runtime, LANE, guard)
    async with guard.flow(page), guard.flow_identity(check):
        await agent_drive(guard)
        assert len(guard.dispatcher.patterns()) == 1
    assert len(guard.dispatcher.patterns()) == 1 and guard.driving()  # the drive's now
    guard.end_drive("done")
    await guard._settle_removal()
    assert guard.dispatcher.patterns() == []


async def test_the_route_stays_until_the_last_flow_lets_go(node):
    guard = egress.guard_for(node.page)
    check = commitgate.flow_identity_check(node.runtime, LANE, guard)
    first, second = guard.flow_identity(check), guard.flow_identity(check)
    await first.__aenter__()
    await second.__aenter__()
    await first.__aexit__(None, None, None)
    assert len(guard.dispatcher.patterns()) == 1
    await second.__aexit__(None, None, None)
    assert guard.dispatcher.patterns() == []


def flow_runtime(spec, surfaces=()):
    def get(name):
        if name == "lanes":
            return SimpleNamespace(get=lambda lane: SimpleNamespace(spec=spec))
        return SimpleNamespace(list=lambda: list(surfaces)) if name == "surfaces" else None
    return SimpleNamespace(get=get)


async def test_a_flows_publish_with_a_body_the_gate_cannot_read_is_refused():
    trace = []
    guard = SimpleNamespace(trace=SimpleNamespace(write=trace.append), _context=None)
    check = commitgate.flow_identity_check(flow_runtime(LaneSpec(id=FB_LANE, account=FB_PIN)), FB_LANE, guard)
    request = SimpleNamespace(post_data_buffer=b"\x00\x01", headers={"content-type": "application/x-evil-words"})
    refusal = await check(rule("facebook.post"), request, "https://www.facebook.com/api/graphql/", "POST")
    assert refusal.rule == "identity_unrecognized" and "cannot read its body" in refusal.reason
    assert trace[0]["flow"] is True and "evil" not in json.dumps(trace)


def test_a_lane_nothing_describes_is_checked_and_its_surfaces_count():
    guard = SimpleNamespace(trace=None)
    assert commitgate.flow_identity_check(SimpleNamespace(get=lambda name: None), FB_LANE, guard) is not None
    assert commitgate.flow_identity_check(flow_runtime(LaneSpec(id="biz-a/main")), "biz-a/main", guard) is not None
    page = Surface(id="biz-a/facebook-page", site="facebook", lane="personal/reddit",
                   acts_as=ActsAs(kind="fb_page", id=PAGE, name="biz-a"))
    named_reddit = flow_runtime(LaneSpec(id="personal/reddit"), [page])
    assert commitgate.flow_identity_check(named_reddit, "personal/reddit", guard) is not None
    listed = flow_runtime(LaneSpec(id="personal/reddit", sites=["facebook.com"]))
    assert commitgate.flow_identity_check(listed, "personal/reddit", guard) is not None


async def test_the_gate_log_lists_the_unknown_identities_seen(node, tmp_path):
    from agentos.dev.net import publish_request
    from agentos.node import gateshapes

    guard = egress.guard_for(node.page)
    guard.trace = egress.EgressTrace(tmp_path / egress.TRACE_NAME)
    check = commitgate.flow_identity_check(node.runtime, LANE, guard)
    async with guard.flow(node.page), guard.flow_identity(check):
        for _ in range(2):
            await node.page.context.send(publish_request("x", [], actor="100089999999999"))
    tok = node.grant(["personal", "biz-a"])  # and an agent's publish the gate asked about
    await node.compose(tok, "As a stranger Page.")
    node.acting_as("100089999999998")
    await node.pending(tok)
    [agent, flow] = sorted(gateshapes.identities(gateshapes.trace_rows(tmp_path)), key=lambda e: e["count"])
    assert flow["id"] == "100089999999999" and flow["count"] == 2 and flow["flow"] is True
    assert flow["field"] == "variables.input.actor_id" and flow["rules"] == ["social.post"]
    assert agent["id"] == "100089999999998" and agent["flow"] is False and "no surface" in agent["why"]
    assert "As a stranger" not in json.dumps([agent, flow])  # ids and field names only
