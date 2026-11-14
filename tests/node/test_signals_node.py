"""Site signals on a real Camoufox (run on the node), against local fixture pages.

Every page and vendor frame is served by route interception under the hostname the rule is about
(www.instagram.com, client-api.arkoselabs.com, ...), from tests/fixtures/site/signals/. No request
leaves the browser: unknown URLs are aborted, every fixture links a data: favicon, and the test
context's proxy is a local trap that records any request that escaped interception (none may).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from agentos.core.models import AccountRef, ActionKind, LaneSpec, Signal
from agentos.node.interaction import PlaywrightInteraction, bind_lane, take_report
from agentos.node.sites import account_identity
from agentos.node.snapshot import find_refs

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "site" / "signals"
TRAP_PORT = int(os.environ.get("AGENTOS_TEST_TRAP_PORT")
                or int(os.environ.get("AGENTOS_TEST_VIEWER_BASE", "6250")) + 1)

IG = "https://www.instagram.com"
SHOP = "https://shop.signals.test"

# url -> (fixture file, HTTP status)
PAGES: dict[str, tuple[str, int]] = {
    f"{IG}/": ("plain-lt.html", 200),
    f"{IG}/explore/": ("checkpoint-lt.html", 200),
    f"{IG}/challenge/?next=/": ("checkpoint-lt.html", 200),
    f"{IG}/challenge/action/en/": ("checkpoint-en.html", 200),
    f"{IG}/accounts/login/": ("login.html", 200),
    "https://www.facebook.com/checkpoint/1501092823525282/": ("checkpoint-lt.html", 200),
    "https://www.facebook.com/": ("plain-lt.html", 200),
    "https://www.linkedin.com/checkpoint/challenge/AgFixture": ("checkpoint-en.html", 200),
    "https://www.linkedin.com/checkpoint/challenge/arkose": ("arkose.html", 200),
    "https://www.linkedin.com/feed/": ("plain-lt.html", 999),
    "https://accounts.google.com/v3/signin/challenge/ipp/consent": ("checkpoint-lt.html", 200),
    "https://x.com/account/access": ("checkpoint-en.html", 200),
    "https://www.reddit.com/r/fixture/": ("plain-lt.html", 429),
    "https://www.reddit.com/r/blocked/": ("plain-lt.html", 403),
    f"{SHOP}/plain": ("plain-lt.html", 200),
    f"{SHOP}/arkose": ("arkose.html", 200),
    f"{SHOP}/datadome": ("datadome.html", 403),
    f"{SHOP}/datadome-blocked": ("datadome-blocked.html", 403),
    f"{SHOP}/human": ("human.html", 403),
    f"{SHOP}/cloudflare": ("cloudflare.html", 403),
    f"{SHOP}/turnstile": ("turnstile-shadow.html", 200),
    f"{SHOP}/passive": ("passive.html", 200),
}
VENDOR_HOSTS = ("arkoselabs.com", "funcaptcha.com", "captcha-delivery.com", "datadome.co", "px-cdn.net",
                "px-cloud.net", "perimeterx.net", "hcaptcha.com", "challenges.cloudflare.com", "google.com",
                "gstatic.com")
TRAP_CHECK = "http://trap-check.signals.test/"


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


class LeakTrap:
    """A local stand-in for a proxy: records the first line of every request sent to it and answers
    nothing. The test context uses it as its proxy, so a request that escaped interception lands here
    instead of on a real site."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.server: asyncio.AbstractServer | None = None

    async def start(self, port: int) -> None:
        self.server = await asyncio.start_server(self._on_client, "127.0.0.1", port)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            self.seen.append(line.decode("latin-1").strip())
        except (TimeoutError, ConnectionError):
            self.seen.append("(connection)")
        finally:
            writer.close()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


async def _serve(route: Any) -> None:
    request = route.request
    url = request.url.split("#", 1)[0]
    if url in PAGES:
        name, status = PAGES[url]
        headers = {"Retry-After": "30"} if status == 429 else {}
        await route.fulfill(status=status, path=str(FIXTURES / name), content_type="text/html; charset=utf-8",
                            headers=headers)
        return
    if url == TRAP_CHECK:
        await route.continue_()  # the trap's own sanity check, to a .test name through the local trap
        return
    host = url.split("/", 3)[2].split(":")[0].lower() if "://" in url else ""
    path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
    if path.startswith("/signals/watch.js"):
        await route.fulfill(path=str(FIXTURES / "watch.js"), content_type="text/javascript")
    elif path.startswith("/cdn-cgi/") or (request.resource_type == "script"
                                          and any(_host_is(host, h) for h in VENDOR_HOSTS)):
        await route.fulfill(body="/* fixture vendor script */", content_type="text/javascript")
    elif any(_host_is(host, h) for h in VENDOR_HOSTS):
        await route.fulfill(path=str(FIXTURES / "vendor-frame.html"), content_type="text/html; charset=utf-8")
    else:
        await route.abort()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def trap() -> AsyncIterator[LeakTrap]:
    t = LeakTrap()
    await t.start(TRAP_PORT)
    try:
        yield t
    finally:
        await t.close()
    assert t.seen == [], f"requests escaped route interception: {t.seen}"


@pytest_asyncio.fixture(loop_scope="session")
async def spage(browser: Any, trap: LeakTrap) -> AsyncIterator[Any]:
    context = await browser.new_context(proxy={"server": f"http://127.0.0.1:{TRAP_PORT}"})
    await context.route("**/*", _serve)
    page = await context.new_page()
    try:
        yield page
    finally:
        await context.close()


@pytest.fixture
def ia() -> PlaywrightInteraction:
    return PlaywrightInteraction()


async def _watch(page: Any) -> dict[str, Any]:
    """Our own observer, in Camoufox's isolated world (the page cannot see it), plus the page's state."""
    await page.wait_for_load_state("load")  # the page's own leak detector is armed on load
    await page.evaluate("""() => {
      window.__signalsTestMutations = [];
      new MutationObserver((records) => {
        for (const r of records) window.__signalsTestMutations.push(r.type + ':' + (r.attributeName || ''));
      }).observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
    }""")
    return await _state(page)


async def _state(page: Any) -> dict[str, Any]:
    return await page.evaluate("() => ({html: document.documentElement.outerHTML, cookie: document.cookie, "
                               "leak: document.body.dataset.leak || ''})")


async def _assert_untouched(page: Any, before: dict[str, Any]) -> None:
    await page.wait_for_timeout(400)  # a few ticks of the page's own leak detector
    after = await _state(page)
    assert after == before
    assert await page.evaluate("window.__signalsTestMutations") == []


# ---- the trap itself ------------------------------------------------------------------------------

async def test_the_trap_catches_a_request_that_is_not_intercepted(spage, trap):
    with pytest.raises(Exception):  # noqa: B017 - the trap answers nothing, so the load fails
        await spage.goto(TRAP_CHECK, timeout=10_000)
    for _ in range(50):
        if trap.seen:
            break
        await asyncio.sleep(0.1)
    assert trap.seen and "trap-check.signals.test" in trap.seen[0], trap.seen
    trap.seen.clear()


# ---- checkpoints, by URL, in any language ----------------------------------------------------------

@pytest.mark.parametrize("url, detail", [
    (f"{IG}/challenge/?next=/", "instagram challenge URL"),
    (f"{IG}/challenge/action/en/", "instagram challenge URL"),
    ("https://www.facebook.com/checkpoint/1501092823525282/", "facebook checkpoint URL"),
    ("https://www.linkedin.com/checkpoint/challenge/AgFixture", "linkedin checkpoint challenge URL"),
    ("https://accounts.google.com/v3/signin/challenge/ipp/consent", "google verification challenge URL"),
    ("https://x.com/account/access", "x locked account URL"),
])
async def test_checkpoint_urls(spage, ia, url, detail):
    result = await ia.navigate(spage, url)
    assert result.status == 200
    assert (result.signal, result.signal_detail) == (Signal.CHECKPOINT, detail)
    before = await _watch(spage)
    report = await ia.report(spage)
    assert (report.signal, report.detail) == (Signal.CHECKPOINT, detail)
    snap = await ia.snapshot(spage)
    assert (snap.signal, snap.signal_detail) == (Signal.CHECKPOINT, detail)
    await _assert_untouched(spage, before)


async def test_the_same_localized_page_is_ok_off_the_checkpoint_url(spage, ia):
    """explore/ serves the Lithuanian checkpoint page; the plain page is full of scary words. Both are ok
    when the URL is ordinary and the session is there: text never decides."""
    await spage.context.add_cookies([
        {"name": "sessionid", "value": "fixture", "domain": ".instagram.com", "path": "/", "secure": True},
        {"name": "ds_user_id", "value": "4567890123", "domain": ".instagram.com", "path": "/", "secure": True},
    ])
    bind_lane(spage, LaneSpec(id="biz-a/instagram"))
    for url in (f"{IG}/explore/", f"{IG}/"):
        result = await ia.navigate(spage, url)
        assert (result.signal, result.signal_detail) == (Signal.OK, None), url
        assert result.account == {"site": "instagram", "user_id": "4567890123", "source": "cookie ds_user_id",
                                  "status": "unpinned"}


async def test_act_and_snapshot_carry_the_signal(spage, ia):
    await ia.navigate(spage, f"{IG}/challenge/?next=/")
    snap = await ia.snapshot(spage)
    refs = find_refs(snap.text, role="button")
    assert refs, snap.text
    result = await ia.act(spage, refs[0], ActionKind.CLICK)
    assert result.ok and result.changed
    assert (result.signal, result.signal_detail) == (Signal.CHECKPOINT, "instagram challenge URL")
    assert take_report(spage).signal is Signal.CHECKPOINT
    assert take_report(spage) is None


# ---- vendor challenges: challenging, not merely loaded ---------------------------------------------

@pytest.mark.parametrize("url, status, signal, detail", [
    (f"{SHOP}/arkose", 200, Signal.CAPTCHA, "Arkose (FunCaptcha) frame"),
    ("https://www.linkedin.com/checkpoint/challenge/arkose", 200, Signal.CAPTCHA, "Arkose (FunCaptcha) frame"),
    (f"{SHOP}/datadome", 403, Signal.CAPTCHA, "DataDome frame"),
    (f"{SHOP}/datadome-blocked", 403, Signal.BLOCKED, "DataDome block frame"),
    (f"{SHOP}/human", 403, Signal.CAPTCHA, "HUMAN (PerimeterX) challenge"),
    (f"{SHOP}/cloudflare", 403, Signal.CAPTCHA, "Cloudflare challenge page"),
    (f"{SHOP}/turnstile", 200, Signal.CAPTCHA, "Cloudflare Turnstile frame"),
    (f"{SHOP}/passive", 200, Signal.OK, None),
    (f"{SHOP}/plain", 200, Signal.OK, None),
])
async def test_vendor_challenges(spage, ia, url, status, signal, detail):
    result = await ia.navigate(spage, url)
    assert result.status == status
    await spage.wait_for_load_state("load")
    before = await _watch(spage)
    report = await ia.report(spage)
    assert (report.signal, report.detail) == (signal, detail)
    await _assert_untouched(spage, before)


# ---- status, session cookies, account -------------------------------------------------------------

@pytest.mark.parametrize("url, status, signal, detail", [
    ("https://www.reddit.com/r/fixture/", 429, Signal.RATE_LIMITED, "HTTP 429"),
    ("https://www.reddit.com/r/blocked/", 403, Signal.BLOCKED, "HTTP 403"),
    ("https://www.linkedin.com/feed/", 999, Signal.BLOCKED, "linkedin HTTP 999"),
])
async def test_statuses(spage, ia, url, status, signal, detail):
    result = await ia.navigate(spage, url)
    assert (result.status, result.signal, result.signal_detail) == (status, signal, detail)


async def test_a_missing_session_cookie_is_logged_out_for_the_lane_that_needs_it(spage, ia):
    before_bind = await ia.navigate(spage, f"{IG}/")
    assert before_bind.signal is Signal.OK  # nothing says this context should be signed in
    bind_lane(spage, LaneSpec(id="biz-a/instagram"))
    result = await ia.navigate(spage, f"{IG}/")
    assert (result.signal, result.signal_detail) == (Signal.LOGGED_OUT, "instagram session cookie sessionid missing")
    login = await ia.navigate(spage, f"{IG}/accounts/login/")
    assert (login.signal, login.signal_detail) == (Signal.LOGGED_OUT, "instagram login URL")
    before = await _watch(spage)
    assert (await ia.report(spage)).signal is Signal.LOGGED_OUT
    await _assert_untouched(spage, before)
    other = await ia.navigate(spage, "https://www.facebook.com/")
    assert other.signal is Signal.OK  # an instagram lane is not expected on facebook


async def test_the_session_read_is_http_only_and_invisible_to_the_page(spage, ia):
    await spage.context.add_cookies([
        {"name": "sessionid", "value": "fixture", "domain": ".instagram.com", "path": "/", "secure": True,
         "httpOnly": True},
        {"name": "ds_user_id", "value": "555", "domain": ".instagram.com", "path": "/", "secure": True},
    ])
    bind_lane(spage, LaneSpec(id="biz-a/instagram"))
    result = await ia.navigate(spage, f"{IG}/")
    assert result.signal is Signal.OK  # httpOnly sessionid is seen through the context, not document.cookie
    assert "sessionid" not in await spage.evaluate("document.cookie")
    assert await account_identity(spage.context, "instagram") == {"site": "instagram", "user_id": "555",
                                                                  "source": "cookie ds_user_id"}


async def test_wrong_account_is_reported(spage, ia):
    await spage.context.add_cookies([
        {"name": "sessionid", "value": "fixture", "domain": ".instagram.com", "path": "/", "secure": True},
        {"name": "ds_user_id", "value": "555", "domain": ".instagram.com", "path": "/", "secure": True},
    ])
    bind_lane(spage, LaneSpec(id="biz-a/instagram",
                              account=AccountRef(site="instagram", handle="@acme.coffee", user_id="444")))
    result = await ia.navigate(spage, f"{IG}/")
    assert result.signal is Signal.OK
    assert result.account["status"] == "wrong_account" and result.account["user_id"] == "555"
    assert result.signal_detail == "wrong account: instagram user 555 is signed in, the lane expects @acme.coffee (444)"
    assert take_report(spage).wrong_account
    bind_lane(spage, LaneSpec(id="biz-a/instagram", account=AccountRef(site="instagram", user_id="555")))
    assert (await ia.navigate(spage, f"{IG}/")).account["status"] == "match"
