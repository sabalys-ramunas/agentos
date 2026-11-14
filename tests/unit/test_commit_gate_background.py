"""The commit gate on real sites (the full test of 2026-11-10, findings 10, 13, 16 to 21, 27): a
site's own background traffic is not a publish. The shapes the real node recorded go as reads; a
request no rule reads is held without a card unless it carries what the agent typed and the act's
own press sent it; an act whose own effect left says so, and never "nothing was sent". And the
other direction: a commit rule's request always waits for a person, and an unknown non-GET that
carries the agent's words is always held. Fake requests (test_egress) and the dev node (test_commit_gate)."""

from __future__ import annotations

import json
from urllib.parse import quote_plus, urlencode

import pytest
from test_commit_gate import LANE, Node, ask_first_on, pending
from test_commit_gate_digest import JSON, Ctx, Request
from test_commit_gate_guard import Gate
from test_egress import COMMITTER, FakeRequest, FakeRoute, make_guard

from agentos.core.errors import ApprovalPending, PublishUnbound
from agentos.core.models import AccountRef, LaneSpec
from agentos.dev import net as dev_net
from agentos.dev import web as dev_web
from agentos.node import commitgate, egress
from agentos.node import tools as node_tools
from agentos.node.egress import EFFECT_RULES, Refusal, typed_in, unknown_rule
from agentos.node.tools import browser as browser_tools

RULES = {r.id: r for r in EFFECT_RULES}
FORM = {"content-type": "application/x-www-form-urlencoded"}
TEXT = {"content-type": "text/plain;charset=UTF-8"}
LI = "https://www.linkedin.com"
RSC = f"{LI}/flagship-web/rsc-action/actions/server-request"
REDDIT = "https://www.reddit.com/svc/shreddit/graphql"
TYPED = "Five lessons from a year of baking"
META = {k: "1" for k in ("__aaid", "__user", "__a", "__req", "__hs", "dpr", "__ccg", "__rev", "__s", "__hsi",
                         "__comet_req", "fb_dtsg", "jazoest", "lsd", "__spin_r", "__spin_b", "__spin_t", "__crn")}


def envelope(payload: str = "AAAAbase64==") -> bytes:
    """LinkedIn's tracking envelope, every field name as the real node recorded it."""
    head = {"server": "x", "serviceName": "y", "publicAppVersion": "1", "appName": "voyager-web",
            "clientApplicationInstance": {"applicationUrn": "urn:li:x", "version": "1", "trackingId": {"fixedValue": "z"}}}
    return json.dumps({"clientContext": {"eventHeaderContext": head, "userRequestHeaderContext": {
        "theme": "light", "interfaceLocale": "en_US", "isBrowserPersistentRetryEnabled": True,
        "isFlushOnCloseBrowserTabEnabled": True, "clientDeviceType": "DESKTOP", "timeZoneOffsetMinutes": 0}},
        "requests": [{"id": "1", "payload": {"name": "PageViewEvent", "payload": payload, "isPegasusEncoded": True},
                      "clientBreadCrumbs": [], "isShadowEvent": False}]}).encode()


def rsc(op: str) -> bytes:
    return json.dumps({"requestId": op, "serverRequest": {"requestId": op, "requestedArguments": {}}}).encode()


def reddit(op: str, **variables) -> bytes:
    return json.dumps({"operation": op, "variables": variables, "csrf_token": "t"}).encode()


# What the real node's gate log held on 2026-11-10 (names and shapes only), as it was sent
BACKGROUND = {
    "linkedin beacon (envelope)": (f"{LI}/to11ybWSpBFqEq9mx", JSON, envelope()),
    "linkedin beacon (another build)": (f"{LI}/tapiIT3oVvopi2Lp", JSON, envelope()),
    "linkedin beacon (text/plain)": (f"{LI}/E1J9Xpqg3_38diMca95", TEXT, b"opaque sensor data 1f2e3d"),
    "linkedin pagination": (f"{LI}/flagship-web/rsc-action/actions/pagination", JSON,
                            json.dumps({"pagerId": "feed", "clientArguments": {"payload": {"startIndex": 10}}}).encode()),
    "linkedin app-config": (f"{LI}/flagship-web/rsc-action/actions/app-config", JSON, b"{}"),
    "linkedin presence": (f"{LI}/voyager/api/messaging/dash/presenceStatuses", JSON, json.dumps({"ids": ["a"]}).encode()),
    "linkedin feed render": (f"{LI}/flagship-web/feed/", JSON, json.dumps(
        {"requestedArguments": {"payload": {}, "states": [], "screenId": "s"}, "isPrefetch": True}).encode()),
    "linkedin profile render": (f"{LI}/flagship-web/in/some-member/", JSON, json.dumps(
        {"requestedArguments": {"payload": {"vanityName": "some-member"}}, "isPrefetch": False}).encode()),
    "linkedin counter": (RSC, JSON, rsc("com.linkedin.sdui.update.incrementCounterMetric")),
    "linkedin tracking pixels": (RSC, JSON, rsc("com.linkedin.sdui.impl.homenav.requests.getThirdPartyTrackingPixels")),
    "linkedin hover card": (RSC, JSON, rsc("com.linkedin.sdui.requests.feed.actorPreviewCard")),
    "reddit validation": (REDDIT, JSON, reddit("ValidateCreatePostInput", input={"title": TYPED})),
    "reddit trophies": (REDDIT, JSON, reddit("TrophyCategories")),
    "reddit achievements": (REDDIT, JSON, reddit("DeleteAchievementNotifications", ids=["1"])),
    "reddit token": ("https://www.reddit.com/svc/shreddit/token", JSON, json.dumps({"csrf_token": "t"}).encode()),
    "facebook relay": ("https://www.facebook.com/ajax/relay-ef/", FORM, urlencode(META).encode()),
    "facebook webworker": ("https://www.facebook.com/static_resources/webworker/rsrc/", FORM, urlencode(META).encode()),
    "facebook banzai": ("https://www.facebook.com/ajax/bnzai", FORM, b"ts=1&q=%5B%5D&post_0=x"),
    "meta login sync": ("https://www.facebook.com/instagram/login_sync/update/", FORM,
                        urlencode({**META, "encrypted_data": "enc", "session_key": "k"}).encode()),
    "instagram sync": ("https://www.instagram.com/sync/instagram/", FORM, urlencode({**META, "encrypted_data": "e"}).encode()),
    "instagram sso users": ("https://www.instagram.com/api/v1/web/fxcal/ig_sso_users/", FORM, b"fb_dtsg=t&jazoest=1"),
    "facebook query": ("https://www.facebook.com/api/graphql/", {**FORM, "x-fb-friendly-name": "FeedComposerCometRootQuery"},
                       urlencode({**META, "fb_api_req_friendly_name": "FeedComposerCometRootQuery", "doc_id": "1"}).encode()),
}


async def route_one(guard, ctx, url: str, headers: dict, body: bytes, gate=None) -> tuple[FakeRoute, Gate]:
    gate = gate or Gate()
    async with guard.acting(gate=gate):
        [(_, handler)] = ctx.routes
        request = FakeRequest(url, "POST", headers=headers, body=body)
        ctx.emit("request", request)
        route = FakeRoute()
        await handler(route, request)
    return route, gate


@pytest.mark.parametrize("name", sorted(BACKGROUND))
async def test_the_background_traffic_the_real_node_recorded_goes_as_a_read(tmp_path, name):
    url, headers, body = BACKGROUND[name]
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    route, gate = await route_one(guard, ctx, url, headers, body)
    assert route.continued == 1 and not route.aborted and gate.seen == [], name


@pytest.mark.parametrize(("name", "carry"), [
    ("linkedin beacon (envelope)", lambda: envelope(payload=TYPED)),
    ("linkedin beacon (text/plain)", lambda: f"x={TYPED}".encode()),
    ("linkedin presence", lambda: json.dumps({"ids": [TYPED]}).encode()),
    ("facebook relay", lambda: urlencode({**META, "__req": TYPED}).encode()),
    ("meta login sync", lambda: urlencode({**META, "encrypted_data": TYPED}).encode()),
    ("reddit token", lambda: json.dumps({"csrf_token": TYPED}).encode()),
])
async def test_a_background_read_that_carries_the_agents_words_is_held(tmp_path, name, carry):
    """The safety side of the new reads: one that carries what the agent typed is gated as unknown."""
    url, headers, _ = BACKGROUND[name]
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.note_typed(TYPED)
    route, gate = await route_one(guard, ctx, url, headers, carry())
    assert route.aborted and len(gate.seen) == 1 and gate.seen[0].endswith(".unknown"), name


@pytest.mark.parametrize(("url", "headers", "body"), [
    (f"{LI}/to11ybWSpBFqEq9mx", JSON, json.dumps({"clientContext": {}, "commentary": "hello"}).encode()),  # another field
    (f"{LI}/to11ybWSpBFqEq9mx", JSON, rsc("com.linkedin.sdui.requests.sharing.CreatePost")),  # an operation
    (f"{LI}/notifications", TEXT, b"x"),            # a word path, not a rotating one
    (f"{LI}/e1j9xpqg3_38dimca95", TEXT, b"x"),      # one case only: not the rotating shape
    (f"{LI}/a/b/E1J9Xpqg3_38diMca95", TEXT, b"x"),  # more than one segment
    (f"{LI}/flagship-web/feed/", JSON, json.dumps({"requestedArguments": {}, "commentary": "hi"}).encode()),
    ("https://www.reddit.com/svc/shreddit/token", JSON, json.dumps({"csrf_token": "t", "title": "x"}).encode()),
    (REDDIT, JSON, json.dumps([{"operation": "DeleteAchievementNotifications"}, {"operation": "CreatePostV2"}]).encode()),
    (RSC, JSON, rsc("com.linkedin.sdui.infra.register.cooloff.activity")),  # not plainly a read: still unknown
])
async def test_what_only_looks_like_the_recorded_reads_is_still_held(tmp_path, url, headers, body):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    route, gate = await route_one(guard, ctx, url, headers, body)
    assert route.aborted and gate.seen and gate.seen[0].endswith(".unknown")


@pytest.mark.parametrize(("rule", "url", "headers", "body"), [
    ("linkedin.post", RSC, JSON, rsc("com.linkedin.sdui.requests.sharing.CreatePost")),
    ("linkedin.comment", RSC, JSON, rsc("com.linkedin.sdui.comments.createComment")),
    ("reddit.post", REDDIT, JSON, reddit("CreatePost", input={"title": "x"})),
    ("facebook.post", "https://www.facebook.com/api/graphql/", {**FORM, "x-fb-friendly-name": "ComposerStoryCreateMutation"},
     urlencode({"fb_api_req_friendly_name": "ComposerStoryCreateMutation", "variables": "{}"}).encode()),
])
async def test_every_commit_rule_still_goes_to_the_gate(tmp_path, rule, url, headers, body):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    route, gate = await route_one(guard, ctx, url, headers, body)
    assert route.aborted and gate.seen == [rule]


# ---- the gate: a card only for what a person can judge ---------------------------------------

def act_gate(ctx, *, submit: bool, active: bool = True, typed=()) -> commitgate.ActGate:
    g = commitgate.ActGate(ctx, "test/linkedin", call={"ref": "e1", "action": "click", "text": None},
                           submit_like=submit)
    g.typed = lambda body: typed_in(list(typed), body)
    g.active = active
    return g


UNKNOWN_URL = f"{LI}/voyager/api/some/new/endpoint"


async def test_an_unknown_request_without_the_agents_words_is_held_and_nobody_is_asked():
    ctx = Ctx()
    g = act_gate(ctx, submit=True, typed=[TYPED])
    rule = unknown_rule(EFFECT_RULES, "www.linkedin.com", UNKNOWN_URL, "POST")
    refusal = await g.hold(rule, Request(UNKNOWN_URL, JSON, b'{"a": 1}'), UNKNOWN_URL, "POST")
    assert isinstance(refusal, Refusal) and refusal.rule == "unrecognised" and "held and not sent" in refusal.reason
    assert ctx.asked == [] and g.outcome is None and g.held[0]["request"].startswith("POST www.linkedin.com/")
    g.raise_outcome()  # nothing to raise: the act's result lists the hold in egress_blocked


async def test_a_text_plain_beacon_is_never_publish_unbound():
    """Finding 16: an unreadable body no rule reads was `publish_unbound ... nothing was sent`."""
    ctx = Ctx()
    g = act_gate(ctx, submit=True, typed=[TYPED])
    url = f"{LI}/abc"
    rule = unknown_rule(EFFECT_RULES, "www.linkedin.com", url, "POST")
    refusal = await g.hold(rule, Request(url, TEXT, b"\x00binary"), url, "POST")
    assert refusal.rule == "unrecognised" and g.outcome is None and ctx.asked == []


@pytest.mark.parametrize("body", [
    json.dumps({"commentary": {"text": TYPED}}).encode(),                         # JSON
    urlencode({"text": TYPED}).encode(),                                           # a form
    json.dumps({"doc": json.dumps({"t": TYPED})}).encode(),                        # JSON in a JSON field
])
async def test_an_unknown_request_that_carries_the_typed_text_on_a_press_asks_a_person(body):
    ctx = Ctx()
    g = act_gate(ctx, submit=True, typed=[TYPED])
    rule = unknown_rule(EFFECT_RULES, "www.linkedin.com", UNKNOWN_URL, "POST")
    refusal = await g.hold(rule, Request(UNKNOWN_URL, JSON if body[:1] == b"{" else FORM, body), UNKNOWN_URL, "POST")
    assert isinstance(refusal, Refusal) and len(ctx.asked) == 1 and isinstance(g.outcome, ApprovalPending)
    with pytest.raises(ApprovalPending):
        g.raise_outcome()


@pytest.mark.parametrize(("submit", "active"), [(False, True), (True, False)])
async def test_the_agents_words_sent_while_typing_or_between_acts_are_held_without_a_card(submit, active):
    """An autosave or a validation while the agent types, or a page's timer between acts: held, never sent."""
    ctx = Ctx()
    g = act_gate(ctx, submit=submit, active=active, typed=[TYPED])
    rule = unknown_rule(EFFECT_RULES, "www.linkedin.com", UNKNOWN_URL, "POST")
    refusal = await g.hold(rule, Request(UNKNOWN_URL, JSON, json.dumps({"draft": TYPED}).encode()), UNKNOWN_URL, "POST")
    assert refusal.rule == "unrecognised" and "carries text the agent typed" in refusal.reason
    assert ctx.asked == [] and g.outcome is None and g.held[0]["typed"] is True


@pytest.mark.parametrize(("submit", "active"), [(True, True), (False, True), (True, False)])
async def test_a_commit_rules_request_always_asks(submit, active):
    """Nothing that matches a commit rule leaves unapproved, whoever typed what and whenever it is sent."""
    ctx = Ctx()
    g = act_gate(ctx, submit=submit, active=active)
    body = reddit("CreatePost", input={"title": "t", "content": {"markdown": "m"}})
    refusal = await g.hold(RULES["reddit.post"], Request(REDDIT, JSON, body), REDDIT, "POST")
    assert isinstance(refusal, Refusal) and len(ctx.asked) == 1 and isinstance(g.outcome, ApprovalPending)


async def test_what_left_is_said_when_a_later_publish_waits():
    """Never "nothing was sent" when something was: a like left, then a post was held."""
    ctx = Ctx()
    g = act_gate(ctx, submit=True)
    g.left(RULES["linkedin.like"])
    await g.hold(RULES["reddit.post"], Request(REDDIT, JSON, reddit("CreatePost", input={"title": "t"})), REDDIT, "POST")
    with pytest.raises(ApprovalPending) as e:
        g.raise_outcome()
    text = str(e.value)
    assert "also sent a like (linkedin.like)" in text and "nothing was sent" not in text
    g2 = act_gate(Ctx(), submit=True)
    g2.left(RULES["linkedin.like"])
    g2.outcome = PublishUnbound("this click would publish (x) but y; nothing was sent")
    with pytest.raises(PublishUnbound) as e2:
        g2.raise_outcome()
    assert "that request was not sent" in str(e2.value) and "nothing was sent" not in str(e2.value)


def test_typed_text_is_found_the_ways_a_page_sends_it():
    assert typed_in([TYPED], json.dumps({"a": TYPED}).encode()) == TYPED
    assert typed_in([TYPED], json.dumps({"a": json.dumps({"t": TYPED})}).encode()) == TYPED
    assert typed_in([TYPED], f"text={quote_plus(TYPED)}".encode()) == TYPED
    assert typed_in(["Žalgiris už vartų"], json.dumps({"a": "Žalgiris už vartų"}).encode()) is not None
    assert typed_in(["first line\nsecond line here"], json.dumps({"p": ["first line", "x"]}).encode()) == "first line"
    assert typed_in(["hi"], json.dumps({"a": "this is high"}).encode()) is None  # short: a whole value only
    assert typed_in(["hi"], json.dumps({"a": "hi"}).encode()) == "hi"
    assert typed_in(["hi"], b"x=1&c=hi") == "hi"
    assert typed_in([TYPED], b"unrelated") is None and typed_in([], b"x") is None and typed_in([TYPED], None) is None


def test_the_guard_keeps_what_was_typed_while_its_browser_lives(tmp_path):
    """No lifetime any more (review of the fix wave, finding 3): a post typed now and sent hours later
    is still the agent's. A blank type adds no piece."""
    guard, _, _, clock = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.note_typed(TYPED)
    guard.note_typed("   ")
    assert guard.typed_texts() == [TYPED]
    clock.now += 6 * 3600
    assert guard.typed_texts() == [TYPED]
    assert guard.carries_body(json.dumps({"a": TYPED}).encode()) == TYPED
    assert TYPED not in repr(guard.typed)  # never the text, also in a log line or a traceback


# ---- the dev node end to end -----------------------------------------------------------------

@pytest.fixture
async def node(tmp_path):
    commitgate.ASKED.clear()
    n = Node(tmp_path)
    await n.lanes.create(LaneSpec(id=LANE, account=AccountRef(site="social", handle="@biza.coffee")))
    yield n
    await n.lanes.shutdown()


def with_telemetry(monkeypatch, url: str = "https://social.example/api/telemetry") -> None:
    """The composer's Share also sends the site's own telemetry first, as LinkedIn's does."""
    original = dev_web.DevDriver.click

    async def click(self, element):
        if element.key == dev_web.PUBLISH_KEY and self.net is not None:
            await self.net.send(dev_net.DevRequest(url, "POST", headers={"content-type": "application/json"},
                                                   body=json.dumps({"event": "share_clicked"}).encode()))
        await original(self, element)

    monkeypatch.setattr(dev_web.DevDriver, "click", click)


async def test_the_pages_telemetry_is_held_without_a_card_and_the_post_still_asks(node, monkeypatch):
    with_telemetry(monkeypatch)
    await node.compose("Autumn menu is here.")
    ap_id = await pending(node)
    asked = [a for a in node.approvals.list(state="pending")]
    assert [a.id for a in asked] == [ap_id]  # the telemetry made no card in Needs you
    node.approvals.decide(ap_id, "approve")
    result = await node.share()
    assert result["ok"] is True and [p["caption"] for p in node.posts] == ["Autumn menu is here."]
    held = [b for b in result["egress_blocked"] if b["rule"] == "unrecognised"]
    assert held and "/api/telemetry" in held[0]["reason"] and held[0]["prevented"] is True
    assert all(s.url != "https://social.example/api/telemetry" for s in node.sent)  # held, never sent
    assert node.approvals.list(state="pending") == []


async def test_a_publish_no_rule_names_that_carries_the_typed_words_asks_and_says_what_is_known(node, monkeypatch):
    original = dev_net.publish_request

    def renamed(caption, media, *, actor="biz-a"):
        request = original(caption, media, actor=actor)
        request.post_data_buffer = request.post_data_buffer.replace(b'"CreatePost"', b'"CreatePostV2"')
        return request

    monkeypatch.setattr(dev_net, "publish_request", renamed)
    await node.compose("A brand new endpoint")
    ap_id = await pending(node)
    ap = node.approvals.get(ap_id)
    assert node.sent == [] and node.posts == []
    assert ap.summary.startswith("Send a request agentos does not recognise to Social through biz-a/social")
    assert '"A brand new endpoint"' in ap.summary and ap.card["deny_safe"] is True
    assert "It carries text the agent typed as its text" in ap.card["explain"]


async def test_a_like_that_left_is_reported_as_sent(node, monkeypatch):
    """A click whose own write rule left is ok, whatever background request was held with it."""
    from agentos.dev.net import UPLOAD_URL

    original = dev_web.DevDriver.click

    async def click(self, element):
        if element.key == dev_web.PUBLISH_KEY and self.net is not None:
            await self.net.send(dev_net.DevRequest(UPLOAD_URL, "POST", headers={"content-type": "image/png"}, body=b"png"))
            await self.net.send(dev_net.DevRequest("https://social.example/api/log", "POST", headers=JSON, body=b"{}"))
            return
        await original(self, element)

    monkeypatch.setattr(dev_web.DevDriver, "click", click)
    await node.compose("not posted")
    result = await node.share()
    assert result["ok"] is True and any(s.url == UPLOAD_URL for s in node.sent)
    assert [b["rule"] for b in result["egress_blocked"]] == ["unrecognised"]
    assert node.approvals.list(state="pending") == []


# ---- Ask me first on a site without rules (finding 10) ---------------------------------------

FORM_POST = {"action": "https://httpbin.org/post", "method": "post", "fields": [
    {"label": "Name", "role": "textbox", "name": "custname", "changed": True, "value": "Ann", "was": ""}]}


async def test_with_ask_me_first_a_form_post_to_a_host_without_rules_waits_for_a_person(node, monkeypatch):
    async def form(ctx, on, ref, action, text):
        return dict(FORM_POST), "httpbin.org"

    async def unchanged(*args, **kw):
        return None

    monkeypatch.setattr(browser_tools, "form_before_submit", form)
    monkeypatch.setattr(browser_tools, "check_form_unchanged", unchanged)  # the dev page has no form to read again
    await node.compose("x")
    ask_first_on(node)
    with pytest.raises(ApprovalPending) as e:
        await node.act("Write a caption...")
    ap = node.approvals.get(e.value.approval_id)
    assert ap.effect == "commit" and ap.summary == "Submit a form to httpbin.org through biz-a/social (Ask me first is on)"
    assert "httpbin.org" in json.dumps(ap.card) and node.sent == []  # the form card: where it goes, what it sends
    node.approvals.decide(ap.id, "approve")
    result = await node.act("Write a caption...")  # the same call, once approved: the click goes
    assert result["ok"] is True and node.approvals.get(ap.id).state == "consumed"


async def test_without_ask_me_first_the_same_form_post_is_a_draft_as_before(node, monkeypatch):
    async def form(ctx, on, ref, action, text):
        return dict(FORM_POST), "httpbin.org"

    async def unchanged(*args, **kw):
        return None

    monkeypatch.setattr(browser_tools, "form_before_submit", form)
    monkeypatch.setattr(browser_tools, "check_form_unchanged", unchanged)
    await node.compose("x")
    result = await node.act("Write a caption...")
    assert result["ok"] is True


def test_which_form_posts_ask_me_first_holds(tmp_path):
    guard, _, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    page = "https://httpbin.org/forms/post"
    assert node_tools.ungated_post_host(guard, page, FORM_POST) == "httpbin.org"
    assert node_tools.ungated_post_host(guard, page, {**FORM_POST, "method": "get"}) is None
    assert node_tools.ungated_post_host(guard, page, {**FORM_POST, "action": "https://www.reddit.com/api/x"}) is None
    assert node_tools.ungated_post_host(guard, page, {**FORM_POST, "action": ""}) == "httpbin.org"  # its own page
    assert node_tools.ungated_post_host(guard, "about:blank", {**FORM_POST, "action": "javascript:void(0)"}) \
        == "an address the node cannot read"
    guard.open_login("httpbin.org")  # the vault's login window: a login is no publish
    assert node_tools.ungated_post_host(guard, page, FORM_POST) is None
    assert node_tools.ungated_post_host(guard, page, None) is None


# ---- LinkedIn's bot-defence frame (finding 17) -----------------------------------------------

def test_linkedins_bot_defence_frame_loads_on_its_suggested_list():
    sites = egress.SUGGESTED_SITES["linkedin"]
    assert "frame:li.protechts.net" in sites
    assert egress.judge("https://li.protechts.net/x", "GET", navigation=True, sites=sites, driver=COMMITTER,
                        human=False, frame=True) is None
    assert egress.judge("https://li.protechts.net/x", "GET", navigation=True, sites=sites, driver=COMMITTER,
                        human=False, frame=False) is not None  # never as a page
    assert egress.unreviewed_sites(sites) == []
