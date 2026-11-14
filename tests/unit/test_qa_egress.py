"""QA findings in the lane egress guard, judged with fakes (no browser). The browser-level bypasses
(QA-egress-5 to QA-egress-10) are in tests/node/test_qa_egress_node.py: unit tests never start a
browser (docs/DEVELOPING.md). All ten
are fixed; the tests after the findings pin the rules of the rebuilt pre-check."""

from __future__ import annotations

from agentos.core.errors import EgressRefused
from agentos.core.models import ActionKind
from agentos.node.egress import Driver, LaneGuard
from agentos.node.routes import GUARD_PATTERN, RouteDispatcher


class FakeTargetLocator:
    """What check_action reads back from the page: the target as TARGET_JS would return it."""

    def __init__(self, target):
        self._target = target

    async def evaluate(self, expression, arg=None, timeout=None):
        return self._target


class FakePage:
    def __init__(self, target):
        self._target = target
        self.modes = []

    def locator(self, selector):
        page = self

        class Loc(FakeTargetLocator):
            async def evaluate(self, expression, arg=None, timeout=None):
                page.modes.append(arg)
                return await super().evaluate(expression, arg, timeout)

        return Loc(self._target)


class FakeRoute:
    def __init__(self):
        self.aborted = None
        self.continued = False

    async def abort(self, error_code="failed"):
        self.aborted = error_code

    async def continue_(self, **kwargs):
        self.continued = True


class FakeRequest:
    def __init__(self, url="https://site.localhost/x", method="GET", nav=True):
        self.url = url
        self.method = method
        self.headers = {}
        self.redirected_from = None
        self._nav = nav

    def is_navigation_request(self):
        return self._nav


class UnreadableRequest:
    """A request whose URL cannot be read any more (it went away while the route was pending)."""

    method = "GET"

    @property
    def headers(self):
        return {}

    @property
    def url(self):
        raise RuntimeError("the request went away")

    def is_navigation_request(self):
        return True


class FakeContext:
    def __init__(self):
        self.routes = {}

    async def route(self, pattern, handler):
        self.routes[pattern] = handler

    async def unroute(self, pattern, handler=None):
        self.routes.pop(pattern, None)

    def on(self, event, fn):
        pass

    def remove_listener(self, event, fn):
        pass


def agent(ceiling="draft"):
    return Driver(kind="agent", id="claude", grant_id="g1", lists=None, ceiling=ceiling)


async def refusal_of(guard: LaneGuard, page, action: ActionKind | str, text: str | None) -> str | None:
    try:
        await guard.check_action(page, "e1", action, text)
    except EgressRefused as e:
        return str(e)
    return None


async def test_form_submit_to_a_commit_endpoint_is_refused_under_a_read_grant():
    # QA-egress-1: check_action used to judge a form without the effect rules, so a POST to a
    # commit endpoint got no grant check before it was sent (the listener only held the lane
    # afterwards). The design's `commit` rule refuses it here now.
    guard = LaneGuard("t/x", sites=["reddit.com", "*.reddit.com"])
    guard.drive(agent(ceiling="read"))
    page = FakePage({"targets": [{"kind": "form", "url": "https://www.reddit.com/api/submit", "method": "post"}]})
    reason = await refusal_of(guard, page, ActionKind.CLICK, None)
    assert reason is not None, "a read grant lets a POST to a publish endpoint through"


async def test_javascript_link_is_refused_on_a_listed_lane():
    # QA-egress-2: host_of() reads only http, https, ws and wss, so a javascript: href was never a
    # host and the guard let it through; the code it runs can navigate anywhere. Refused now.
    guard = LaneGuard("t/x", sites=["site.localhost"])
    guard.drive(agent())
    page = FakePage({"targets": [{"kind": "link", "url": "javascript:location.href='https://evil.localhost/collect'",
                     "target": ""}]})
    reason = await refusal_of(guard, page, ActionKind.CLICK, None)
    assert reason is not None, "a javascript: link runs code that can navigate off the lane's list"


async def test_a_failing_route_guard_aborts_the_request():
    # QA-egress-3: RouteDispatcher._dispatch used to log a guard exception and go on to the owners
    # and continue_(), so a refused request left when the guard itself broke. It fails closed now.
    ctx = FakeContext()
    dispatcher = RouteDispatcher(ctx)

    async def broken_guard(route, request):
        raise RuntimeError("guard broke")

    await dispatcher.add_guard(broken_guard)
    route = FakeRoute()
    await ctx.routes[GUARD_PATTERN](route, FakeRequest(url="https://evil.localhost/collect", method="POST"))
    assert route.aborted is not None
    assert not route.continued


async def test_strict_guard_aborts_a_request_it_cannot_read():
    # QA-egress-4: LaneGuard._route_guard used to return False (not refused) when it could not read
    # url, method or headers, which let the request go out unjudged. It is aborted now.
    guard = LaneGuard("t/x", sites=["site.localhost"], strict=True)
    guard.drive(agent())
    route = FakeRoute()
    handled = await guard._route_guard(route, UnreadableRequest())
    assert handled is True
    assert route.aborted is not None


# ---- the rebuilt pre-check ----

EVIL_FORM = {"targets": [{"kind": "form", "url": "https://evil.localhost/collect", "method": "post"}]}


async def test_newlines_in_type_and_fill_and_a_select_are_checked_as_an_enter():
    guard = LaneGuard("t/x", sites=["site.localhost"])
    guard.drive(agent())
    page = FakePage(EVIL_FORM)
    for action, text in ((ActionKind.TYPE, "x\n"), (ActionKind.FILL, "a\rb"), (ActionKind.SELECT, "Two"),
                         (ActionKind.PRESS, "Shift+Enter")):
        assert await refusal_of(guard, page, action, text) is not None, action
    assert page.modes == ["key"] * 4
    for action, text in ((ActionKind.TYPE, "hello"), (ActionKind.FILL, "a b"), (ActionKind.PRESS, "Tab"),
                         (ActionKind.HOVER, None), (ActionKind.SCROLL, None)):
        assert await refusal_of(guard, page, action, text) is None, action
    assert len(page.modes) == 4  # nothing else read the page
    await refusal_of(guard, page, ActionKind.CLICK, None)
    assert page.modes[-1] == "click"


async def test_a_tab_before_a_newline_is_refused_without_reading_the_page():
    guard = LaneGuard("t/x", sites=["site.localhost"])
    guard.drive(agent())
    page = FakePage({"targets": []})
    reason = await refusal_of(guard, page, ActionKind.TYPE, "user\tpass\n")
    assert reason is not None and "tab" in reason and page.modes == []
    assert await refusal_of(guard, page, ActionKind.TYPE, "line\n\tindented") is None


async def test_what_cannot_be_judged_is_refused_on_a_listed_lane():
    guard = LaneGuard("t/x", sites=["site.localhost"])
    guard.drive(agent())
    for target in ({"kind": "link", "url": None}, {"kind": "link", "url": {}},
                   {"kind": "form", "url": None, "method": "post"},
                   {"kind": "unreadable", "why": "a frame the guard cannot read"},
                   {"kind": "link", "url": "data:text/html,<script>location='https://evil.localhost'</script>"},
                   {"kind": "form", "url": "javascript:void(0)", "method": "get"},
                   {"kind": "link", "url": "blob:https://site.localhost/1"},
                   {"kind": "link", "url": "mailto:a@evil.localhost"}):
        reason = await refusal_of(guard, FakePage({"targets": [target]}), ActionKind.CLICK, None)
        assert reason is not None, target
    blocks = guard.blocks_since(0)
    assert {b.rule for b in blocks} == {"unreadable", "scheme"} and all(b.prevented for b in blocks)
    for target in ({"kind": "link", "url": "about:blank"}, {"kind": "link", "url": "https://site.localhost/a"},
                   {"kind": "form", "url": "https://evil.localhost/x", "method": "dialog"}):
        assert await refusal_of(guard, FakePage({"targets": [target]}), ActionKind.CLICK, None) is None, target


async def test_a_grant_list_alone_turns_the_check_on():
    guard = LaneGuard("t/x", sites=[])
    guard.drive(Driver(kind="agent", id="claude", grant_id="g1", lists=(("site.localhost",),), ceiling="draft"))
    page = FakePage({"targets": [{"kind": "link", "url": "https://evil.localhost/x"}]})
    reason = await refusal_of(guard, page, ActionKind.CLICK, None)
    assert reason is not None and "egress list" in reason


async def test_no_site_list_and_no_grant_list_read_nothing_and_refuse_nothing():
    # Lanes with no site list, driven by grants with no egress list, behave exactly as before: the
    # pre-check reads nothing (commits are still judged by the listener, after the fact).
    guard = LaneGuard("t/x", sites=[])
    guard.drive(agent(ceiling="read"))
    page = FakePage({"targets": [{"kind": "link", "url": "javascript:alert(1)"},
                                 {"kind": "form", "url": "https://www.reddit.com/api/submit", "method": "post"}]})
    for action, text in ((ActionKind.CLICK, None), (ActionKind.TYPE, "a\tb\n"), (ActionKind.PRESS, "Enter")):
        assert await refusal_of(guard, page, action, text) is None
    assert page.modes == [] and guard.blocks_since(0) == []


async def test_a_commit_endpoint_passes_under_a_commit_grant():
    guard = LaneGuard("t/x", sites=["reddit.com", "*.reddit.com"])
    guard.drive(agent(ceiling="commit"))
    page = FakePage({"targets": [{"kind": "form", "url": "https://www.reddit.com/api/submit", "method": "post"}]})
    assert await refusal_of(guard, page, ActionKind.CLICK, None) is None
