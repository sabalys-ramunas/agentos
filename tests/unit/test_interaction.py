import asyncio

import pytest

from agentos.core.errors import EngineError, InvalidRequest, RefNotFound
from agentos.core.models import ActionKind, Signal
from agentos.node.human import _inside, _visible_part
from agentos.node.human_fields import arrow_steps, match_option
from agentos.node.interaction import (
    PlaywrightInteraction,
    check_url,
    classify_signal,
    diff_state,
    is_challenge_url,
    normalize_ref,
    same_origin,
)


@pytest.mark.parametrize("given, ref", [("e12", "e12"), (" [ref=e12] ", "e12"), ("ref=f2e7", "f2e7")])
def test_normalize_ref_accepts_snapshot_forms(given, ref):
    assert normalize_ref(given) == ref


@pytest.mark.parametrize("bad", ["", "12", "e", "button", "e12; drop", "#post-1", "aria-ref=e1"])
def test_normalize_ref_rejects_everything_else(bad):
    with pytest.raises(InvalidRequest):
        normalize_ref(bad)


@pytest.mark.parametrize("url", ["https://www.reddit.com/r/x/", "https://8.8.8.8/", "about:blank"])
def test_check_url_allows_web_pages(url):
    assert check_url(url) == url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:7400/mcp", "http://localhost:6100/", "http://LOCALHOST./", "http://app.localhost/",
    "http://[::1]:7400/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://2130706433/",
    "http://0x7f.1/", "http://0177.0.0.1/", "http://127.1/", "http://10.1.2.3/", "http://192.168.1.1/",
    "http://172.16.0.5/", "http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/",
])
def test_check_url_refuses_the_node_and_private_networks(url):
    with pytest.raises(InvalidRequest):
        check_url(url)


def test_check_url_allows_private_targets_when_asked():
    assert check_url("http://127.0.0.1:6120/feed.html", allow_private=True) == "http://127.0.0.1:6120/feed.html"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi",
                                 "about:config", "chrome://browser/content", "www.example.com", "https://"])
def test_check_url_rejects_non_web_schemes(url):
    with pytest.raises(InvalidRequest):
        check_url(url)


@pytest.mark.parametrize("url", [
    "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0",
    "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html",
    "https://www.google.com/recaptcha/api2/anchor?k=x",
    "https://www.recaptcha.net/recaptcha/api2/bframe?k=x",
])
def test_challenge_urls(url):
    assert is_challenge_url(url)


@pytest.mark.parametrize("url", [
    "https://www.google.com/search?q=recaptcha",
    "https://example.com/www.google.com/recaptcha/",
    "https://notgoogle.com/recaptcha/api2/anchor",
    "https://evil-hcaptcha.com.example.net/",
    "about:srcdoc",
    "",
])
def test_non_challenge_urls(url):
    assert not is_challenge_url(url)


def test_classify_signal_is_deterministic():
    assert classify_signal(loaded=False, status=429, challenge=True) is Signal.UNKNOWN
    assert classify_signal(loaded=True, status=200, challenge=True) is Signal.CAPTCHA
    assert classify_signal(loaded=True, status=403, challenge=True) is Signal.CAPTCHA
    assert classify_signal(loaded=True, status=429, challenge=False) is Signal.RATE_LIMITED
    assert classify_signal(loaded=True, status=403, challenge=False) is Signal.BLOCKED
    assert classify_signal(loaded=True, status=200, challenge=False) is Signal.OK
    assert classify_signal(loaded=True, status=None, challenge=False) is Signal.OK
    assert classify_signal(loaded=True, status=500, challenge=False) is Signal.OK


def test_diff_state_reports_only_the_targets_own_changes():
    before = {"aria-pressed": "false", "name": "Like 12"}
    after = {"aria-pressed": "true", "name": "Like 13"}
    assert diff_state(before, after) == ["aria-pressed false -> true", 'name "Like 12" -> "Like 13"']
    assert diff_state(before, dict(before)) == []


def test_diff_state_formats_values_and_missing_keys():
    assert diff_state({"value": ""}, {"value": "Ada"}) == ['value "" -> "Ada"']
    assert diff_state({}, {"aria-expanded": "true"}) == ["aria-expanded (none) -> true"]
    long = "x" * 100
    (line,) = diff_state({"value": ""}, {"value": long})
    assert line.endswith('..."') and len(line) < 90
    assert diff_state({"checked": "false", "zeta": "1"}, {"checked": "true", "zeta": "2"}) == \
        ["checked false -> true", "zeta 1 -> 2"]


def test_match_option_prefers_exact_then_folded_labels():
    options = [{"label": "Choose one"}, {"label": "Green"}, {"label": "green  tea"}]
    assert match_option(options, "Green") == 1
    assert match_option(options, " GREEN TEA ") == 2
    assert match_option(options, "Purple") is None


def test_arrow_steps_skip_disabled_options():
    options = [{"label": "a"}, {"label": "b", "disabled": True}, {"label": "c"}, {"label": "d"}]
    assert arrow_steps(options, 0, 3) == 2
    assert arrow_steps(options, 3, 0) == -2
    assert arrow_steps(options, -1, 2) == 2  # nothing selected: the first ArrowDown lands on "a"
    with pytest.raises(InvalidRequest):
        arrow_steps(options, 0, 1)


def test_visible_part_clips_the_box_to_the_viewport():
    assert _visible_part({"x": 10, "y": -50, "width": 100, "height": 80}, 800, 600) == \
        {"x": 10, "y": 0, "width": 100, "height": 30}
    assert _visible_part({"x": 10, "y": 700, "width": 100, "height": 80}, 800, 600) is None
    assert _visible_part({"x": 10, "y": 599, "width": 100, "height": 80}, 800, 600) is None  # 1 px shows
    assert _visible_part(None, 800, 600) is None


def test_inside():
    box = {"x": 10, "y": 10, "width": 20, "height": 20}
    assert _inside(box, 15, 29) and not _inside(box, 31, 15) and not _inside(None, 0, 0)


class _FakeLocator:
    async def count(self):
        return 0


class _FakePage:
    """Just enough of a Playwright page for the paths that never touch the DOM."""

    def __init__(self, url="about:blank"):
        self.url = url
        self.selectors: list[str] = []

    def on(self, event, handler):
        pass

    def locator(self, selector):
        self.selectors.append(selector)
        return _FakeLocator()


async def test_signal_is_unknown_before_anything_loads():
    assert await PlaywrightInteraction().signal(_FakePage()) is Signal.UNKNOWN


async def test_act_validates_before_touching_the_page():
    ia, page = PlaywrightInteraction(), _FakePage("https://example.com/")
    with pytest.raises(InvalidRequest):
        await ia.act(page, "e1", "drag")
    with pytest.raises(InvalidRequest):
        await ia.act(page, "e1", ActionKind.TYPE)
    with pytest.raises(InvalidRequest):
        await ia.act(page, "not-a-ref", ActionKind.CLICK)
    assert page.selectors == []


async def test_act_on_an_unknown_ref_raises_ref_not_found():
    page = _FakePage("https://example.com/")
    with pytest.raises(RefNotFound):
        await PlaywrightInteraction().act(page, "[ref=e5]", "click")
    assert page.selectors == ["aria-ref=e5"]


async def test_navigate_refuses_local_files():
    with pytest.raises(InvalidRequest):
        await PlaywrightInteraction().navigate(_FakePage(), "file:///root/agentos/poc/secrets.json")


# ---- navigate: follow the page's own link mid-visit ---------------------------------------------

class _Request:
    def __init__(self, frame, url="", failure=None, *, nav=True, redirected_from=None):
        self.frame, self.url, self.failure, self.nav = frame, url, failure, nav
        self.redirected_from = redirected_from

    def is_navigation_request(self):
        return self.nav


class _Response:
    def __init__(self, request, url, status=200):
        self.request, self.frame, self.url, self.status = request, request.frame, url, status


class _Handle:
    def __init__(self, element):
        self.element, self.disposed = element, False

    def as_element(self):
        return self.element

    async def dispose(self):
        self.disposed = True


class _Link:
    """A link as _LINK_FACTS_JS reads it. By default it leads to whatever the caller asks about;
    `href`, `target` and `baseTarget` set what it really says (and can change mid-click)."""

    def __init__(self, **facts):
        self.disposed = False
        self.facts = {"connected": True, "href": None, "target": None, "baseTarget": None,
                      "download": False, "onclick": False, **facts}

    async def evaluate(self, js, target):
        return {**self.facts, "want": target, "href": self.facts["href"] or target}

    async def dispose(self):
        self.disposed = True


class _SitePage:
    """A page showing a site, with listeners, a main frame and a goto that records its target."""

    def __init__(self, url, link=None, goto_fails=None):
        self.url = url
        self.main_frame = object()
        self.link = link
        self.goto_fails = goto_fails
        self.handlers: dict[str, list] = {}
        self.gotos: list[str] = []
        self.looked_up: list[str] = []

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers[event].remove(handler)

    def emit(self, event, *args):
        for handler in list(self.handlers.get(event, [])):
            handler(*args)

    async def evaluate_handle(self, js, target):
        self.looked_up.append(target)
        return _Handle(self.link)

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        if self.goto_fails:
            raise RuntimeError(self.goto_fails)
        self.url = url
        return _Response(_Request(self.main_frame, url), url, 201)

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def title(self):
        return f"title of {self.url}"

    def starts(self, url, redirected_from=None):
        """A navigation request of the main frame (not yet committed)."""
        request = _Request(self.main_frame, url, redirected_from=redirected_from)
        self.emit("request", request)
        return request

    def commits(self, request, status=200):
        self.emit("response", _Response(request, request.url, status))
        self.emit("requestfinished", request)
        self.url = request.url
        self.emit("framenavigated", self.main_frame)

    def lands(self, url, status=200):
        """What a click on a link does: the navigation request, its response, the commit."""
        self.commits(self.starts(url), status)

    def pushes(self, url):
        """A script changes the address in the same document (history.pushState)."""
        self.url = url
        self.emit("framenavigated", self.main_frame)


@pytest.fixture
def quiet_report(monkeypatch):
    async def report(self, page):
        class _Report:
            def public(self):
                return {}
        return _Report()
    monkeypatch.setattr(PlaywrightInteraction, "report", report)


class _Clicked(list):
    """The links clicked; `pressed`: those the press reached (the before-press check let it)."""

    def __init__(self):
        super().__init__()
        self.pressed = []


def _clicks(monkeypatch, effect, *, before=None):
    """human.click as it behaves around the press: `before` runs during the scroll and glide (the
    page's chance to change things), then the before-press check, then the press's `effect`."""
    from agentos.node import human

    clicked = _Clicked()
    pressed = clicked.pressed

    async def click(page, target):
        clicked.append(target)
        if before is not None:
            await before(page)
        await human._check_before_press()
        pressed.append(target)
        await effect(page)
    monkeypatch.setattr("agentos.node.human.click", click)
    return clicked


async def test_navigate_follows_a_visible_link_to_the_same_site(monkeypatch, quiet_report):
    link = _Link()
    page = _SitePage("https://site.example/feed", link=link)

    async def lands(p):
        p.lands("https://site.example/feed?page=2")
    clicked = _clicks(monkeypatch, lands)
    result = await PlaywrightInteraction().navigate(page, "https://site.example/feed?page=2", follow_link=True)
    assert clicked == [link] and clicked.pressed == [link] and page.gotos == []
    assert (result.url, result.status, result.title) == ("https://site.example/feed?page=2", 200,
                                                         "title of https://site.example/feed?page=2")
    assert link.disposed
    # the follow's own listeners are gone; what is left is the status tracker's
    assert page.handlers["framenavigated"] == []
    assert len(page.handlers["request"]) == len(page.handlers["requestfailed"]) == 1


async def test_navigate_clicks_nothing_unless_asked(monkeypatch, quiet_report):
    page = _SitePage("https://site.example/feed", link=_Link())
    clicked = _clicks(monkeypatch, lambda p: None)
    await PlaywrightInteraction().navigate(page, "https://site.example/feed?page=2")
    assert clicked == [] and page.gotos == ["https://site.example/feed?page=2"]


@pytest.mark.parametrize("current, target", [
    ("https://site.example/feed", "https://other.example/feed?page=2"),       # another site
    ("https://site.example/feed", "http://site.example/feed?page=2"),         # another scheme
    ("https://site.example/feed", "https://site.example:8443/feed?page=2"),   # another port
    ("https://site.example/feed#top", "https://site.example/feed#comments"),  # the same page
    ("about:blank", "https://site.example/"),                                 # the start of a visit
])
async def test_navigate_goes_directly_when_no_link_could_lead_there(monkeypatch, quiet_report, current, target):
    page = _SitePage(current, link=_Link())
    clicked = _clicks(monkeypatch, lambda p: None)
    result = await PlaywrightInteraction().navigate(page, target, follow_link=True)
    assert page.looked_up == [] and clicked == [] and page.gotos == [target]
    assert result.status == 201


async def test_navigate_goes_directly_when_the_page_has_no_such_link(monkeypatch, quiet_report):
    page = _SitePage("https://site.example/feed", link=None)
    clicked = _clicks(monkeypatch, lambda p: None)
    await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    assert page.looked_up == ["https://site.example/about"] and clicked == []
    assert page.gotos == ["https://site.example/about"]


async def test_a_link_whose_click_starts_nothing_falls_back_to_goto(monkeypatch, quiet_report):
    monkeypatch.setattr("agentos.node.interaction.navigation.LINK_START_S", 0.05)
    page = _SitePage("https://site.example/feed", link=_Link())

    async def nothing(p):
        return None
    clicked = _clicks(monkeypatch, nothing)
    result = await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    assert len(clicked) == 1 and page.gotos == ["https://site.example/about"] and result.status == 201
    assert len(page.handlers["request"]) == 1 and page.handlers["framenavigated"] == []


async def test_a_covered_link_falls_back_to_goto(monkeypatch, quiet_report):
    from agentos.node.human import TargetCovered

    page = _SitePage("https://site.example/feed", link=_Link())

    async def covered(p):
        raise TargetCovered("a banner covers it")
    clicked = _clicks(monkeypatch, lambda p: None, before=covered)
    await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    assert clicked.pressed == [] and page.gotos == ["https://site.example/about"]


async def test_a_followed_link_whose_navigation_fails_raises_like_goto(monkeypatch, quiet_report):
    page = _SitePage("https://site.example/feed", link=_Link())

    async def fails(p):
        request = _Request(p.main_frame, "https://site.example/about", failure="NS_ERROR_ABORT")
        p.emit("request", request)
        p.emit("requestfailed", request)
    _clicks(monkeypatch, fails)
    with pytest.raises(EngineError, match="navigation to https://site.example/about failed: NS_ERROR_ABORT"):
        await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    assert page.gotos == []


async def test_a_link_whose_href_changes_before_the_press_is_not_pressed(monkeypatch, quiet_report):
    """Page scripts have the scroll, the pause and the glide to point the link elsewhere: the check
    right before the mouse down sees it, and the address is opened directly instead."""
    link = _Link()
    page = _SitePage("https://site.example/feed", link=link)

    async def repoint(p):
        link.facts["href"] = "https://site.example/logout"

    async def lands(p):
        p.lands("https://site.example/logout")
    clicked = _clicks(monkeypatch, lands, before=repoint)
    result = await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    assert clicked == [link] and clicked.pressed == []
    assert page.gotos == ["https://site.example/about"] and result.url == "https://site.example/about"


@pytest.mark.parametrize("change", [
    {"baseTarget": "_blank"},                    # a <base target> added: the link opens a new tab
    {"target": "popup"},
    {"download": True},
    {"onclick": True},
    {"connected": False},
])
async def test_a_link_that_stops_opening_here_before_the_press_is_not_pressed(monkeypatch, quiet_report, change):
    link = _Link()
    page = _SitePage("https://site.example/feed", link=link)

    async def alter(p):
        link.facts.update(change)
    clicked = _clicks(monkeypatch, lambda p: None, before=alter)
    await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    assert clicked.pressed == [] and page.gotos == ["https://site.example/about"]


async def test_a_link_under_a_base_target_blank_is_never_clicked(monkeypatch, quiet_report):
    page = _SitePage("https://site.example/feed", link=_Link(baseTarget="_blank"))
    clicked = _clicks(monkeypatch, lambda p: None)
    await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    assert clicked == [] and page.gotos == ["https://site.example/about"]


async def test_a_click_that_goes_elsewhere_is_not_followed_and_the_goto_comes_after_it(monkeypatch, quiet_report):
    page = _SitePage("https://site.example/feed", link=_Link())
    order = []

    async def elsewhere(p):
        p.lands("https://site.example/interstitial")
        order.append("landed elsewhere")
    _clicks(monkeypatch, elsewhere)
    real_goto = page.goto

    async def goto(url, **kwargs):
        order.append("goto")
        return await real_goto(url, **kwargs)
    page.goto = goto
    result = await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    assert order == ["landed elsewhere", "goto"]
    assert page.gotos == ["https://site.example/about"] and result.url == "https://site.example/about"


async def test_a_goto_that_fails_after_the_link_went_elsewhere_says_where_the_page_is(monkeypatch, quiet_report):
    page = _SitePage("https://site.example/feed", link=_Link(), goto_fails="NS_ERROR_NET_RESET")

    async def elsewhere(p):
        p.lands("https://site.example/interstitial?token=s3cret")
    _clicks(monkeypatch, elsewhere)
    with pytest.raises(EngineError) as raised:
        await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    message = str(raised.value)
    assert "NS_ERROR_NET_RESET" in message and "https://site.example/interstitial" in message
    assert "s3cret" not in message


async def test_only_the_commit_of_the_link_s_navigation_counts(monkeypatch, quiet_report):
    """A pushState while the navigation is in flight, and a redirect hop, are not its commit; the
    followed navigation lands where its last redirect points."""
    page = _SitePage("https://site.example/feed", link=_Link())
    events = []

    async def redirected(p):
        first = p.starts("https://site.example/about")
        p.pushes("https://site.example/feed?tab=2")  # the old document tidies its address
        hop = p.starts("https://site.example/about/", redirected_from=first)

        async def later():
            await asyncio.sleep(0.05)
            events.append("commit")
            p.commits(hop)
        asyncio.ensure_future(later())

    async def loaded(*args, **kwargs):
        events.append("loaded")
    page.wait_for_load_state = loaded
    _clicks(monkeypatch, redirected)
    result = await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    assert events == ["commit", "loaded"] and page.gotos == []
    assert result.url == "https://site.example/about/" and result.status == 200


async def test_a_script_router_showing_the_target_counts_as_followed(monkeypatch, quiet_report):
    page = _SitePage("https://site.example/feed", link=_Link())

    async def routed(p):
        p.pushes("https://site.example/about")
    _clicks(monkeypatch, routed)
    result = await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    assert page.gotos == [] and result.url == "https://site.example/about"


async def test_a_late_navigation_is_waited_for_not_raced_by_a_goto(monkeypatch, quiet_report):
    """The click's script fetches first and navigates after LINK_START_S: while its request is in
    flight the network is not quiet, so the navigation is caught as the link's, with no goto."""
    monkeypatch.setattr("agentos.node.interaction.navigation.LINK_START_S", 0.05)
    page = _SitePage("https://site.example/feed", link=_Link())

    async def fetch_then_navigate(p):
        fetch = _Request(p.main_frame, "https://site.example/api/track", nav=False)
        p.emit("request", fetch)

        async def later():
            await asyncio.sleep(0.3)
            p.emit("requestfinished", fetch)
            p.lands("https://site.example/about")
        asyncio.ensure_future(later())
    _clicks(monkeypatch, fetch_then_navigate)
    result = await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    assert page.gotos == [] and result.url == "https://site.example/about" and result.status == 200


async def test_navigate_still_refuses_local_files_from_a_site(monkeypatch, quiet_report):
    page = _SitePage("https://site.example/feed", link=_Link())
    with pytest.raises(InvalidRequest):
        await PlaywrightInteraction().navigate(page, "file:///etc/passwd", follow_link=True)
    assert page.looked_up == [] and page.gotos == []


def test_same_origin():
    assert same_origin("https://Site.example/a", "https://site.example:443/b?c#d")
    assert not same_origin("https://site.example/", "http://site.example/")
    assert not same_origin("about:blank", "about:blank")
    assert not same_origin("https://site.example/", "https://www.site.example/")


_HERE = {"want": "https://site.example/about", "connected": True, "href": "https://site.example/about",
         "target": None, "baseTarget": None, "download": False, "onclick": False}


@pytest.mark.parametrize("change, leads", [
    ({}, True),
    ({"target": "_self"}, True),
    ({"target": " _TOP "}, True),
    ({"target": ""}, True),
    ({"target": "", "baseTarget": "_blank"}, True),   # the link's own (empty) target wins over the base's
    ({"baseTarget": "_blank"}, False),
    ({"baseTarget": "_self"}, True),
    ({"target": "_blank"}, False),
    ({"target": "_parent"}, False),
    ({"href": "https://site.example/logout"}, False),
    ({"href": "https://site.example/about?x=1"}, False),
    ({"href": "https://site.example/about#team"}, False),  # another address: only an exact link is clicked
    ({"download": True}, False),
    ({"onclick": True}, False),
    ({"connected": False}, False),
    ({"want": None}, False),
])
def test_link_leads_to(change, leads):
    from agentos.node.interaction.navigation import _link_leads_to

    assert _link_leads_to({**_HERE, **change}) == ("https://site.example/about" if leads else None)
    assert _link_leads_to(None) is None


def test_a_link_with_the_asked_for_fragment_leads_there():
    from agentos.node.interaction.navigation import _link_leads_to

    here = "https://site.example/about#team"
    assert _link_leads_to({**_HERE, "want": here, "href": here}) == here
    assert _link_leads_to({**_HERE, "want": here}) is None


async def test_a_link_to_the_right_page_but_another_fragment_is_not_pressed(monkeypatch, quiet_report):
    page = _SitePage("https://site.example/feed", link=_Link(href="https://site.example/about#team"))
    clicked = _clicks(monkeypatch, lambda p: None)
    await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True)
    assert clicked.pressed == [] and page.gotos == ["https://site.example/about"]


async def test_a_link_with_a_fragment_lands_as_the_page_without_it(monkeypatch, quiet_report):
    """The fragment decides which link, not which navigation: the request carries none."""
    page = _SitePage("https://site.example/feed", link=_Link())

    async def lands(p):
        p.commits(p.starts("https://site.example/about"))
    _clicks(monkeypatch, lands)
    await PlaywrightInteraction().navigate(page, "https://site.example/about#team", follow_link=True)
    assert page.gotos == []


class _Clicks(list):
    """An on_click that remembers each call."""

    def __call__(self):
        self.append("click")


async def test_on_click_runs_right_before_the_press(monkeypatch, quiet_report):
    page = _SitePage("https://site.example/feed", link=_Link())
    told = _Clicks()
    seen = []

    async def lands(p):
        seen.append(list(told))
        p.lands("https://site.example/about")
    _clicks(monkeypatch, lands)
    await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True, on_click=told)
    assert told == ["click"] and seen == [["click"]]


async def test_on_click_is_silent_when_nothing_was_pressed(monkeypatch, quiet_report):
    link = _Link()
    page = _SitePage("https://site.example/feed", link=link)
    told = _Clicks()

    async def repoint(p):
        link.facts["href"] = "https://site.example/logout"
    _clicks(monkeypatch, lambda p: None, before=repoint)
    await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True, on_click=told)
    assert told == [] and page.gotos == ["https://site.example/about"]


async def test_a_click_that_goes_elsewhere_or_fails_is_still_told(monkeypatch, quiet_report):
    """The page's handlers ran either way: a goto after the press does not undo the click."""
    page = _SitePage("https://site.example/feed", link=_Link())
    told = _Clicks()

    async def elsewhere(p):
        p.lands("https://site.example/interstitial")
    _clicks(monkeypatch, elsewhere)
    await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True, on_click=told)
    assert told == ["click"] and page.gotos == ["https://site.example/about"]

    page = _SitePage("https://site.example/feed", link=_Link())
    told = _Clicks()

    async def fails(p):
        request = p.starts("https://site.example/about")
        request.failure = "NS_ERROR_ABORT"
        p.emit("requestfailed", request)
    _clicks(monkeypatch, fails)
    with pytest.raises(EngineError):
        await PlaywrightInteraction().navigate(page, "https://site.example/about", follow_link=True, on_click=told)
    assert told == ["click"]


class Select:
    """A closed dropdown that takes the keys but, like the macOS native menu, does not move on
    them; typing its opening letters is what moves it."""

    def __init__(self, labels, index=0, arrows_work=False):
        self.options = [{"label": label} for label in labels]
        self.index, self.arrows_work, self.typed = index, arrows_work, ""
        self.focused = False

    async def evaluate(self, js):
        if "tag:" in js:
            return {"tag": "select", "multiple": False, "size": 0, "selectedIndex": self.index,
                    "options": self.options}
        if "selectedIndex" in js:
            return self.index
        if "matches(':focus')" in js:
            return self.focused
        return None

    async def focus(self):
        self.focused = True


class SelectPage:
    def __init__(self, select):
        self.select, self.keys = select, []

    async def wait_for_timeout(self, ms):
        pass


def run_select(monkeypatch, select, label, arrows_work):
    from agentos.node import human_fields
    human_presses = []
    if not arrows_work:
        async def arrows(page, key):  # the key arrives, the menu does not move
            human_presses.append(key)
        async def typed(page, ch):  # type-ahead moves to the first option starting with what is in
            page.select.typed += ch
            for i, opt in enumerate(page.select.options):
                if opt["label"].lower().startswith(page.select.typed.lower()):
                    page.select.index = i
                    return
    else:
        async def arrows(page, key):
            human_presses.append(key)
            page.select.index += {"ArrowDown": 1, "ArrowUp": -1}.get(key, 0)
        async def typed(page, ch):
            page.select.typed += ch
    monkeypatch.setattr(human_fields, "press", arrows)
    monkeypatch.setattr(human_fields, "click", lambda page, target: asyncio.sleep(0))
    monkeypatch.setattr(human_fields, "_type_into", lambda page, target, text: typed(page, text[0]))
    page = SelectPage(select)
    asyncio.run(human_fields.select_option(page, select, label))
    return page, human_presses


def test_a_dropdown_that_ignores_the_arrows_gets_its_option_typed(monkeypatch):
    select = Select(["Choose one", "Australia", "Germany", "Ireland"])
    page, presses = run_select(monkeypatch, select, "Germany", arrows_work=False)
    assert select.index == 2 and page.select.typed == "G"
    assert presses  # the arrows were tried first


def test_arrows_alone_finish_when_the_menu_moves(monkeypatch):
    select = Select(["Choose one", "Australia", "Germany"])
    _, presses = run_select(monkeypatch, select, "Germany", arrows_work=True)
    assert select.index == 2 and presses == ["ArrowDown", "ArrowDown", "Enter"]


def test_an_option_already_selected_takes_no_input(monkeypatch):
    select = Select(["Choose one", "Germany"], index=1)
    page, presses = run_select(monkeypatch, select, "Germany", arrows_work=False)
    assert presses == [] and page.select.typed == ""
