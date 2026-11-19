"""The lane egress guard (agentos.node.egress) and the route dispatcher (agentos.node.routes),
with fake requests, frames and contexts. The real browser is in tests/node/test_egress_node.py."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agentos.core.errors import EgressRefused
from agentos.core.models import ActionKind, Effect, LaneSpec, validate_sites
from agentos.node import egress
from agentos.node.egress import (
    EFFECT_RULES,
    SUGGESTED_SITES,
    Driver,
    EgressTrace,
    LaneGuard,
    classify,
    judge,
)
from agentos.node.routes import RouteDispatcher, glob_match

SITES = ("instagram.com", "*.instagram.com", "*.cdninstagram.com")
AGENT = Driver(kind="agent", id="claude", grant_id="g1", lists=(("*.instagram.com", "instagram.com"),),
               ceiling="draft")
OPEN_AGENT = Driver(kind="agent", id="claude", grant_id="g0")  # a grant that sets no list and no ceiling
COMMITTER = Driver(kind="agent", id="claude", grant_id="g2", lists=None, ceiling="commit")
OWNER = Driver(kind="human", id="owner")


# ---- fakes ---------------------------------------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeFrame:
    def __init__(self, url: str = "https://www.instagram.com/", parent=None, fail: bool = False):
        self.url = url
        self.parent_frame = parent
        self.evaluated: list[str] = []
        self.fail = fail

    async def evaluate(self, js: str, *args):
        if self.fail:
            raise RuntimeError("frame detached")
        self.evaluated.append(js)


class FakeRequest:
    def __init__(self, url: str, method: str = "GET", *, nav: bool = False, frame=None, hop=None,
                 headers=None, body: bytes | None = None, no_frame: bool = False):
        self.url = url
        self.method = method
        self._nav = nav
        self._frame = frame or FakeFrame()
        self.redirected_from = hop
        self.headers = headers or {}
        self.post_data_buffer = body
        self._no_frame = no_frame
        self.resource_type = "document" if nav else "fetch"

    def is_navigation_request(self) -> bool:
        return self._nav

    @property
    def frame(self):
        if self._no_frame:
            raise RuntimeError("Frame for this navigation request is not available")
        return self._frame


class FakePage:
    def __init__(self, url: str = "https://www.instagram.com/", opener=None):
        self.url = url
        self.main_frame = FakeFrame(url)
        self.main_frame.page = self
        self._opener = opener
        self.closed = False
        self.handlers: dict[str, list] = {}
        self.back = 0
        self.reloads = 0
        self.reload_fails = False

    def on(self, event: str, fn) -> None:
        self.handlers.setdefault(event, []).append(fn)

    async def opener(self):
        return self._opener

    async def close(self) -> None:
        self.closed = True

    async def go_back(self, timeout=None):
        self.back += 1
        return object()

    async def reload(self, **kw):
        if self.reload_fails:
            raise RuntimeError("Navigation failed")
        self.reloads += 1
        return object()


class FakeContext:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.pages: list[FakePage] = []
        self.routes: list[tuple[str, object]] = []
        self.unrouted: list[str] = []

    def on(self, event: str, fn) -> None:
        self.handlers.setdefault(event, []).append(fn)

    def remove_listener(self, event: str, fn) -> None:
        self.handlers.get(event, []).remove(fn)

    def emit(self, event: str, arg) -> None:
        for fn in list(self.handlers.get(event, [])):
            fn(arg)

    async def route(self, pattern: str, handler) -> None:
        self.routes.append((pattern, handler))

    async def unroute(self, pattern: str, handler=None) -> None:
        self.unrouted.append(pattern)
        self.routes = [(p, h) for p, h in self.routes if not (p == pattern and (handler is None or h is handler))]

    async def cookies(self, urls=None):
        return []


class FakeRoute:
    def __init__(self) -> None:
        self.aborted: str | None = None
        self.continued = 0
        self.fulfilled = 0

    async def abort(self, code: str = "failed") -> None:
        self.aborted = code

    async def continue_(self, **kw) -> None:
        self.continued += 1

    async def fulfill(self, **kw) -> None:
        self.fulfilled += 1


def make_guard(tmp_path, *, sites=SITES, driver: Driver | None = AGENT, strict: bool = False):
    clock = Clock()
    guard = LaneGuard("biz-a/instagram", sites=sites, strict=strict, trace=EgressTrace(tmp_path / "trace.jsonl"),
                      clock=clock)
    events: list[tuple[str, str, dict]] = []
    guard.events = lambda lane, kind, detail: events.append((lane, kind, detail))
    ctx = FakeContext()
    guard.attach(ctx, RouteDispatcher(ctx))
    guard.drive(driver)
    return guard, ctx, events, clock


class FailingDispatcher:
    """A route dispatcher the browser refuses: no route can be bound on the lane."""

    async def add_guard(self, guard, pattern=None):
        raise RuntimeError("the browser refused the route")

    async def remove_guard(self, guard, pattern=None):
        return None


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ---- site lists ----------------------------------------------------------------------------

def test_site_lists_are_normalized_and_star_is_refused():
    assert validate_sites([" Instagram.COM", "*.instagram.com", "instagram.com"]) == ["instagram.com",
                                                                                     "*.instagram.com"]
    with pytest.raises(ValueError, match="empty list already means"):
        validate_sites(["*"])
    with pytest.raises(ValueError, match="bad site"):
        validate_sites(["insta gram.com"])
    assert LaneSpec(id="biz-a/instagram", sites=["https://www.instagram.com/feed"]).sites == ["www.instagram.com"]
    with pytest.raises(ValueError):
        LaneSpec(id="biz-a/instagram", sites=["evil..example"])


def test_a_lane_stored_before_site_lists_has_none():
    spec = LaneSpec.model_validate_json('{"id": "personal/reddit", "proxy_label": "lt1"}')
    assert spec.sites == [] and spec.egress_strict is False


def test_suggested_lists_are_valid_and_effect_rule_ids_are_unique():
    assert set(SUGGESTED_SITES) == {"instagram", "facebook", "reddit", "linkedin", "medium"}
    for sites in SUGGESTED_SITES.values():
        assert validate_sites(list(sites)) == list(sites)
    assert len({rule.id for rule in EFFECT_RULES}) == len(EFFECT_RULES)


# ---- verdicts ------------------------------------------------------------------------------

def test_no_lists_and_an_open_grant_block_nothing():
    """A lane made before the guard, driven by a grant that sets no list: exactly as before."""
    for driver in (None, OPEN_AGENT, OWNER):
        assert judge("https://evil.example/?q=dm", "GET", navigation=True, sites=(), driver=driver, human=False) is None
        assert judge("https://evil.example/x", "POST", navigation=False, sites=(), driver=driver, human=False) is None
        rule = classify("https://www.instagram.com/api/v1/media/configure/", "POST")
        assert judge("https://www.instagram.com/api/v1/media/configure/", "POST", navigation=False, sites=(),
                     driver=driver, human=False, effect=rule) is None


def test_the_lane_list_binds_every_driver_but_never_a_human_in_control():
    for driver in (AGENT, OWNER, None):
        refusal = judge("https://evil.example/", "GET", navigation=True, sites=SITES, driver=driver, human=False)
        assert refusal is not None and refusal.rule == "lane_sites" and "evil.example" in refusal.reason
    # a person who took control decides where to go (sign-in providers redirect through hosts no list predicts)
    assert judge("https://evil.example/", "GET", navigation=True, sites=SITES, driver=AGENT, human=True) is None
    assert judge("https://www.instagram.com/p/1/", "GET", navigation=True, sites=SITES, driver=AGENT,
                 human=False) is None
    assert judge("https://badinstagram.com/", "GET", navigation=True, sites=SITES, driver=None, human=False)


def test_the_grant_list_applies_only_while_an_agent_drives():
    sites = (*SITES, "*.fbcdn.net")
    refusal = judge("https://scontent.fbcdn.net/x.jpg", "GET", navigation=True, sites=sites, driver=AGENT, human=False)
    assert refusal is not None and refusal.rule == "grant_egress"
    assert judge("https://scontent.fbcdn.net/x.jpg", "GET", navigation=True, sites=sites, driver=AGENT,
                 human=True) is None  # taken over: the lane's list alone
    assert judge("https://scontent.fbcdn.net/x.jpg", "GET", navigation=True, sites=sites, driver=OWNER,
                 human=False) is None


def test_subresource_gets_pass_and_non_gets_stay_inside_both_lists():
    assert judge("https://tracker.example/pixel.gif?d=secret", "GET", navigation=False, sites=SITES, driver=AGENT,
                 human=False) is None
    refusal = judge("https://evil.example/collect", "POST", navigation=False, sites=SITES, driver=AGENT, human=False)
    assert refusal is not None and refusal.rule == "non_get"
    assert judge("https://evil.example/collect", "POST", navigation=False, sites=SITES, driver=AGENT,
                 human=True) is None
    assert judge("https://www.instagram.com/ajax/bz", "POST", navigation=False, sites=SITES, driver=AGENT,
                 human=False) is None


def test_effect_rules_decide_commits_not_the_method():
    graphql = classify("https://www.instagram.com/graphql/query", "POST")
    assert graphql is not None and graphql.effect == "read"
    configure = classify("https://i.instagram.com/api/v1/media/configure_sidecar/", "POST")
    assert configure is not None and configure.effect == "commit"
    assert classify("https://www.instagram.com/api/v1/media/configure/", "GET") is None
    assert classify("https://www.example.com/api/v1/media/configure/", "POST") is None
    fb = "https://www.facebook.com/api/graphql/"
    assert classify(fb, "POST", {"X-FB-Friendly-Name": "ComposerStoryCreateMutation"}).effect == "commit"
    assert classify(fb, "POST", {"X-FB-Friendly-Name": "CometNewsFeedPaginationQuery"}).effect == "read"
    assert classify(fb, "POST").effect == "read"

    url = "https://www.instagram.com/api/v1/media/configure/"
    refusal = judge(url, "POST", navigation=False, sites=SITES, driver=AGENT, human=False, effect=configure)
    assert refusal is not None and refusal.rule == "commit" and "up to draft" in refusal.reason
    assert judge(url, "POST", navigation=False, sites=SITES, driver=COMMITTER, human=False, effect=configure) is None
    assert judge(url, "POST", navigation=False, sites=SITES, driver=AGENT, human=True, effect=configure) is None
    like = classify("https://www.instagram.com/api/v1/web/likes/17/like/", "POST")
    reader = Driver(kind="agent", id="r", ceiling="read")
    assert judge(like.host, "POST", navigation=False, sites=(), driver=reader, human=False, effect=like) is None
    refusal = judge("https://www.instagram.com/api/v1/web/likes/17/like/", "POST", navigation=False, sites=(),
                    driver=reader, human=False, effect=like)
    assert refusal is not None and refusal.rule == "write"


def test_driver_from_a_grant_keeps_every_block_list_and_the_ceiling():
    grant = SimpleNamespace(id="g9", ceiling=Effect.DRAFT, blocks=[
        SimpleNamespace(egress=None, domains=["instagram.com", "*.instagram.com"]),
        SimpleNamespace(egress=None, domains=None),
        SimpleNamespace(egress=["www.instagram.com"], domains=["*.instagram.com"]),
    ])
    ctx = SimpleNamespace(principal=SimpleNamespace(kind="agent", id="claude"), grant=grant)
    d = Driver.from_context(ctx)
    assert d.lists == (("instagram.com", "*.instagram.com"), ("www.instagram.com",))
    assert d.allows_host("www.instagram.com") and not d.allows_host("i.instagram.com")
    assert d.allows("draft") and not d.allows("commit")
    owner = Driver.from_context(SimpleNamespace(principal=SimpleNamespace(kind="agent", id="o"), grant=grant,
                                                in_person=True))
    assert owner.human
    bare = Driver.from_context(SimpleNamespace(principal=SimpleNamespace(kind="agent", id="m1")))
    assert bare.lists is None and bare.ceiling is None and bare.allows("irreversible")


# ---- the listener --------------------------------------------------------------------------

async def test_a_refused_navigation_is_stopped_traced_and_rate_limited(tmp_path):
    guard, ctx, events, clock = make_guard(tmp_path)
    frame = FakeFrame()
    ctx.emit("request", FakeRequest("https://evil.example/leak?d=secret-dm", nav=True, frame=frame))
    await settle()
    assert frame.evaluated == ["window.stop()"]
    [block] = guard.blocks_since(0)
    assert (block.host, block.rule, block.kind, block.prevented) == ("evil.example", "lane_sites", "navigation", False)
    rows = guard.trace.read()
    assert rows[0]["host"] == "evil.example" and rows[0]["path"] == "/leak"
    assert "secret-dm" not in (tmp_path / "trace.jsonl").read_text()
    assert [e[1] for e in events] == ["egress_blocked"]

    for _ in range(5):
        ctx.emit("request", FakeRequest("https://evil.example/leak", nav=True))
    await settle()
    assert len(events) == 1 and len(guard.trace.read()) == 1  # rate-limited
    clock.now += egress.LOG_EVERY_S + 1
    ctx.emit("request", FakeRequest("https://evil.example/leak", nav=True))
    assert len(events) == 2 and events[1][2]["repeats"] == 5
    assert len(guard.blocks_since(0)) == 7  # every block still reaches the tool result


async def test_redirect_hops_subframes_and_popups(tmp_path):
    guard, ctx, _events, _ = make_guard(tmp_path)
    first = FakeRequest("https://www.instagram.com/out", nav=True)
    hop = FakeRequest("https://evil.example/landing", nav=True, hop=first)
    ctx.emit("request", hop)
    sub = FakeRequest("https://evil.example/frame", nav=True, frame=FakeFrame(parent=FakeFrame()))
    ctx.emit("request", sub)
    ctx.emit("request", FakeRequest("https://evil.example/popup", nav=True, no_frame=True))
    popup = FakePage(url="about:blank", opener=FakePage())
    own_tab = FakePage(url="about:blank", opener=None)
    ctx.emit("page", popup)
    ctx.emit("page", own_tab)
    await settle()
    assert [b.kind for b in guard.blocks_since(0)] == ["redirect", "subframe", "popup"]
    assert hop._frame.evaluated == ["window.stop()"] and sub._frame.evaluated == ["window.stop()"]
    assert popup.closed and not own_tab.closed


async def test_a_committed_refused_page_goes_back(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path)
    page = FakePage()
    ctx.emit("page", page)
    main = FakeFrame("https://evil.example/")
    for fn in page.handlers["framenavigated"]:
        fn(main)
    for fn in page.handlers["framenavigated"]:
        fn(FakeFrame("https://evil.example/frame", parent=main))  # a subframe: not this check's job
    await settle()
    assert page.back == 1 and [b.kind for b in guard.blocks_since(0)] == ["committed"]


async def test_observed_commits_are_traced_without_the_body(tmp_path):
    guard, ctx, events, _ = make_guard(tmp_path, driver=COMMITTER)
    body = b'{"caption": "a private caption"}'
    ctx.emit("request", FakeRequest("https://i.instagram.com/api/v1/media/configure/?token=t0k3n", "POST",
                                    body=body))
    [row] = guard.trace.read()
    assert row["kind"] == "observed_commit" and row["size"] == len(body) and row["verified_rule"] is False
    assert (row["host"], row["path"], row["method"]) == ("i.instagram.com", "/api/v1/media/configure/", "POST")
    text = (tmp_path / "trace.jsonl").read_text()
    assert "private caption" not in text and "t0k3n" not in text
    assert guard.blocks_since(0) == [] and events == []


async def test_a_commit_outside_the_grant_holds_the_lane(tmp_path):
    guard, ctx, _events, _ = make_guard(tmp_path)
    held: list = []
    guard.on_violation = lambda block: held.append(block)
    ctx.emit("request", FakeRequest("https://i.instagram.com/api/v1/media/configure/", "POST"))
    await settle()
    assert [b.rule for b in held] == ["commit"]
    assert guard.stats["unblocked"] == 1 and guard.blocks_since(0)[0].prevented is False


async def test_a_human_in_control_is_never_blocked_and_off_list_hosts_are_noted(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(*SITES, "*.fbcdn.net"))
    guard.human_in_control = lambda: True
    noted = []
    guard.trace = type("T", (), {"write": lambda self, row: noted.append(row)})()
    ctx.emit("request", FakeRequest("https://scontent.fbcdn.net/x", nav=True))
    ctx.emit("request", FakeRequest("https://i.instagram.com/api/v1/media/configure/", "POST"))
    ctx.emit("request", FakeRequest("https://accounts.google.lt/accounts/SetSID", nav=True))
    assert guard.blocks_since(0) == []
    assert [(r["kind"], r["host"]) for r in noted if r["kind"] == "noted"] == [("noted", "accounts.google.lt")]


async def test_site_list_changes_from_the_store_take_effect(tmp_path):
    guard, ctx, _, clock = make_guard(tmp_path, sites=(), driver=OPEN_AGENT)
    stored = {"sites": [], "strict": False}
    guard.settings = lambda: (stored["sites"], stored["strict"])
    ctx.emit("request", FakeRequest("https://evil.example/", nav=True))
    stored["sites"] = ["*.instagram.com"]
    clock.now += egress.SITES_TTL_S + 0.1
    ctx.emit("request", FakeRequest("https://evil.example/", nav=True))
    assert len(guard.blocks_since(0)) == 1 and guard.sites == ("*.instagram.com",)


# ---- before an agent acts ------------------------------------------------------------------

class TargetPage:
    def __init__(self, target):
        self.target = target
        self.read = 0

    def locator(self, selector: str):
        page = self

        class Loc:
            async def evaluate(self, js, arg, timeout=None):
                page.read += 1
                return page.target

        assert selector.startswith("aria-ref=")
        return Loc()


async def test_clicks_and_enter_are_checked_before_anything_is_sent(tmp_path):
    guard, _, _events, _ = make_guard(tmp_path)
    page = TargetPage({"targets": [{"kind": "link", "url": "https://evil.example/?q=dm", "target": "_blank"}]})
    with pytest.raises(EgressRefused, match="evil.example is not on this browser's site list.*nothing was sent"):
        await guard.check_action(page, "e12", ActionKind.CLICK, None)
    [block] = guard.blocks_since(0)
    assert block.kind == "action" and block.prevented
    form = TargetPage({"targets": [{"kind": "form", "url": "https://evil.example/collect", "method": "post"}]})
    with pytest.raises(EgressRefused):
        await guard.check_action(form, "e3", ActionKind.PRESS, "Enter")
    assert guard.blocks_since(0)[-1].method == "POST"
    inside = TargetPage({"targets": [{"kind": "link", "url": "https://www.instagram.com/p/1/"}]})
    await guard.check_action(inside, "e4", ActionKind.CLICK, None)
    for action, text in ((ActionKind.HOVER, None), (ActionKind.TYPE, "hello"), (ActionKind.PRESS, "Tab")):
        await guard.check_action(page, "e12", action, text)
    assert page.read == 1  # only the click read the target
    unguarded, *_ = make_guard(tmp_path, sites=(), driver=OPEN_AGENT)
    open_page = TargetPage({"targets": [{"kind": "link", "url": "https://evil.example/"}]})
    await unguarded.check_action(open_page, "e1", ActionKind.CLICK, None)
    assert open_page.read == 0  # nothing to enforce: nothing read


def test_check_url_refuses_with_the_host_and_the_rule(tmp_path):
    guard, *_ = make_guard(tmp_path)
    with pytest.raises(EgressRefused) as exc:
        guard.check_url("https://evil.example/x")
    assert exc.value.code == "egress_refused" and "evil.example" in exc.value.message
    guard.check_url("https://www.instagram.com/")


# ---- strict lanes: the route only while an agent acts --------------------------------------

async def test_strict_route_exists_only_while_an_agent_acts(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, strict=True)
    assert ctx.routes == []
    async with guard.acting() as mark:
        assert [p for p, _ in ctx.routes] == ["**/*"]
        _, handler = ctx.routes[0]
        refused, fine = FakeRoute(), FakeRoute()
        await handler(refused, FakeRequest("https://evil.example/collect", "POST"))
        await handler(fine, FakeRequest("https://www.instagram.com/feed", "GET"))
        # the listener leaves routed requests to the route (no double record)
        ctx.emit("request", FakeRequest("https://evil.example/collect", "POST"))
    assert ctx.routes == [] and ctx.unrouted == ["**/*"]
    assert refused.aborted == "blockedbyclient" and fine.continued == 1 and fine.aborted is None
    [block] = guard.blocks_since(mark)
    assert block.prevented and block.rule == "non_get"
    async with guard.acting(route=False):
        assert ctx.routes == []
    guard.human_in_control = lambda: True
    async with guard.acting():
        assert ctx.routes == []


async def test_a_non_strict_lane_never_routes(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path)
    async with guard.acting():
        ctx.emit("request", FakeRequest("https://evil.example/", nav=True))
    assert ctx.routes == [] and len(guard.blocks_since(0)) == 1


# ---- commits are metered while an agent acts -----------------------------------------------

class FakeMeter:
    def __init__(self, *, over: str | None = None, capped: bool = False) -> None:
        self.over = over
        self.is_capped = capped
        self.charged: list[tuple[str, bool]] = []

    def charge(self, rule, *, sent: bool) -> str | None:
        self.charged.append((rule.id, sent))
        return self.over

    def capped(self) -> bool:
        return self.is_capped


CONFIGURE = "https://i.instagram.com/api/v1/media/configure/"


async def test_a_publish_during_an_act_is_charged_as_a_commit_and_nothing_else_is(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, driver=COMMITTER)
    meter = FakeMeter()
    ctx.emit("request", FakeRequest(CONFIGURE, "POST"))  # outside an act: the page's own traffic
    async with guard.acting(meter=meter):
        assert ctx.routes == []  # under the limit, a lane that is not strict never routes
        ctx.emit("request", FakeRequest(CONFIGURE, "POST"))
        ctx.emit("request", FakeRequest("https://i.instagram.com/rupload_igphoto/x", "POST"))  # an upload: a draft
        ctx.emit("request", FakeRequest("https://www.instagram.com/graphql/query", "POST"))  # a read
    ctx.emit("request", FakeRequest(CONFIGURE, "POST"))
    guard.human_in_control = lambda: True
    async with guard.acting(meter=meter):
        ctx.emit("request", FakeRequest(CONFIGURE, "POST"))  # a person in control: not metered
    assert meter.charged == [("instagram.post", True)]
    assert guard.blocks_since(0) == []


async def test_a_publish_that_left_past_the_limit_is_counted_recorded_and_holds_the_lane(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, driver=COMMITTER)
    held: list = []
    guard.on_violation = lambda block: held.append(block)
    meter = FakeMeter(over="Lane biz-a/instagram reached its limit of 1 commit a day.")
    async with guard.acting(meter=meter) as mark:
        ctx.emit("request", FakeRequest(CONFIGURE, "POST"))
    await settle()
    [block] = guard.blocks_since(mark)
    assert (block.rule, block.prevented) == ("limit_reached", False)
    assert block.reason == ("this publishes (instagram.post): Lane biz-a/instagram reached its limit of "
                            "1 commit a day")
    assert meter.charged == [("instagram.post", True)] and [b.rule for b in held] == ["limit_reached"]


async def test_a_publish_outside_the_lanes_pace_is_refused_as_lane_paced(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, driver=COMMITTER)
    meter = FakeMeter(over="Lane biz-a/instagram posted 10 min ago and keeps at least 45 minutes between posts.",
                      capped=True)
    meter.code = "lane_paced"
    async with guard.acting(meter=meter) as mark:
        _, handler = ctx.routes[0]
        publish = FakeRoute()
        await handler(publish, FakeRequest(CONFIGURE, "POST"))
    [block] = guard.blocks_since(mark)
    assert (block.rule, block.prevented) == ("lane_paced", True) and publish.aborted == "blockedbyclient"


async def test_a_persons_own_publish_is_recorded_on_the_lanes_pace_and_an_agents_is_not(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, driver=COMMITTER)
    recorded: list[str] = []
    guard.on_human_commit = lambda: recorded.append("person")
    async with guard.acting(meter=FakeMeter()):
        ctx.emit("request", FakeRequest(CONFIGURE, "POST"))  # the agent's: charged through its meter instead
    assert recorded == []
    guard.human_in_control = lambda: True
    ctx.emit("request", FakeRequest(CONFIGURE, "POST"))
    await settle()
    assert recorded == ["person"] and guard.blocks_since(0) == []


async def test_past_a_commit_limit_the_act_routes_and_aborts_the_publish_before_it_leaves(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, driver=COMMITTER)
    meter = FakeMeter(over="Lane biz-a/instagram reached its limit of 1 commit a day.", capped=True)
    async with guard.acting(meter=meter) as mark:
        assert [p for p, _ in ctx.routes] == ["**/*"]
        _, handler = ctx.routes[0]
        publish, upload = FakeRoute(), FakeRoute()
        await handler(publish, FakeRequest(CONFIGURE, "POST"))
        await handler(upload, FakeRequest("https://i.instagram.com/rupload_igphoto/x", "POST"))
    assert ctx.routes == []
    assert publish.aborted == "blockedbyclient" and upload.continued == 1
    [block] = guard.blocks_since(mark)
    assert (block.rule, block.prevented) == ("limit_reached", True)
    assert meter.charged == [("instagram.post", False)]  # held: nothing debited


# ---- the route dispatcher ------------------------------------------------------------------

def test_glob_matching():
    assert glob_match("https://signed.example/**", "https://signed.example/a/b?c=1")
    assert not glob_match("https://signed.example/**", "https://signed.example.evil/a")
    assert glob_match("**/*", "https://x.example/")
    assert not glob_match("https://x.example/*", "https://x.example/a/b")


async def test_the_dispatcher_runs_guards_first_and_owners_in_order():
    ctx = FakeContext()
    routes = RouteDispatcher(ctx)
    seen: list[str] = []

    async def signer(route, request):
        seen.append("signer:" + request.url)
        if "signed" not in request.url:
            await route.fallback()
            return
        await route.fulfill(status=200)

    await routes.route("https://signed.example/**", signer)
    assert routes.patterns() == ["https://signed.example/**"]

    async def guard(route, request):
        seen.append("guard")
        if "evil" in request.url:
            await route.abort("aborted")
            return True
        return False

    await routes.add_guard(guard)
    assert sorted(routes.patterns()) == ["**/*", "https://signed.example/**"]
    handler = dict(ctx.routes)["**/*"]
    signed, refused, other = FakeRoute(), FakeRoute(), FakeRoute()
    await handler(signed, FakeRequest("https://signed.example/a"))
    await handler(refused, FakeRequest("https://evil.example/x"))
    await handler(other, FakeRequest("https://other.example/"))
    assert seen == ["guard", "signer:https://signed.example/a", "guard", "guard"]
    assert signed.fulfilled == 1 and refused.aborted == "aborted" and other.continued == 1
    await routes.remove_guard(guard)
    assert routes.patterns() == ["https://signed.example/**"]
    await routes.unroute("https://signed.example/**", signer)
    assert routes.patterns() == [] and ctx.routes == []


async def test_the_lane_signer_installs_through_the_dispatcher(tmp_path):
    from agentos.webbotauth import keys, settings
    from agentos.webbotauth.route import LaneSigner
    from agentos.webbotauth.settings import LaneIdentity

    lane = "biz-a/instagram"
    ident = LaneIdentity(lane_id=lane, signature_agent="https://agent.example/.well-known/http-message-signatures-directory")
    settings.add_host(ident, "https://signed.example", reason="test", by="owner:cli")
    signer = LaneSigner(keys.create_key(tmp_path / "secrets", lane), ident, proxied=True)
    ctx = FakeContext()
    routes = RouteDispatcher(ctx)
    patterns = await signer.install(routes)
    assert patterns == routes.patterns() == [p for p, _ in ctx.routes]
    assert ctx.handlers.get("request")  # the redirect detector listens on the context
    await signer.stop("test")
    assert ctx.routes == []


def test_trace_rows_never_carry_urls_or_bodies(tmp_path):
    trace = EgressTrace(tmp_path / "t.jsonl")
    trace.write({"kind": "blocked", "host": "a.example", "url": "https://a.example/?t=1", "body": "x", "query": "t=1"})
    row = json.loads((tmp_path / "t.jsonl").read_text())
    assert set(row) == {"kind", "host", "at"}
