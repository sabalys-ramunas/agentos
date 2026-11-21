"""The review browser (openajar.studio.browser) in real Chrome, headless, against pages served through
`context.route` (no network). Skips where Chrome cannot start, as test_siteapi_chrome does.

Covers: the width and each theme switch applied and read back, a state reached by steps, each
failure (not loaded, HTTP error, blank, theme not applied, state step, engine missing, time limit),
two calls not sharing cookies or storage, the pins (clock, random numbers, motion), the font report.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time

import pytest

from openajar.core import catalog
from openajar.core.errors import EngineError
from openajar.studio.browser import (
    BlankPage,
    EngineMissing,
    HttpErrorPage,
    Limits,
    PageNotLoaded,
    ReviewPool,
    StateNotReached,
    StudioTimeout,
    ThemeNotApplied,
    engines,
    settle,
)
from openajar.studio.contracts import EngineName, Screen, Step, Target, ThemeSwitch

pw = pytest.importorskip("playwright.async_api")

HOST = "https://studio.example"

STYLE = "<style>body{background:#fff;color:#111;font-family:sans-serif}</style>"
PAGES: dict[str, tuple[int, str]] = {
    "/media": (200, """<style>body{background:#fff} @media (prefers-color-scheme: dark){body{background:#000}}</style>
        <h1>Media</h1>"""),
    "/no-dark": (200, STYLE + "<h1>Light only</h1>"),
    "/attr": (200, """<style>body{background:#fff} [data-theme=dark] body{background:#000}</style><h1>Attr</h1>"""),
    "/attr-fights": (200, """<h1>Attr</h1><script>
        addEventListener('load', () => document.documentElement.setAttribute('data-theme', 'light'))</script>"""),
    "/class": (200, """<style>body{background:#fff} .dark body{background:#000}</style><h1>Class</h1>"""),
    "/storage": (200, """<script>if (localStorage.getItem('theme') === 'night')
        document.documentElement.classList.add('night')</script>
        <style>body{background:#fff} .night body{background:#000}</style><h1>Storage</h1>"""),
    "/menu": (200, STYLE + """<button id="open" onclick="document.getElementById('m').hidden=false">Menu</button>
        <div id="m" role="dialog" hidden><a href="#">Settings</a></div>"""),
    "/late": (200, STYLE + "<div id=r></div><script>setTimeout(() => { document.getElementById('r').textContent = "
              "'Rendered late'; }, 700)</script>"),
    "/blank": (200, "<html><body><div></div><script>/* the app did not render */</script></body></html>"),
    "/hidden": (200, "<div style='visibility:hidden'>Loading</div><p style='opacity:0'>x</p>"),
    "/error": (500, "<h1>Internal Server Error</h1><pre>TypeError: x is undefined</pre>"),
    "/missing": (404, "<h1>Not found</h1>"),
    "/cookie": (200, """<h1>Cookie</h1><script>document.cookie = 'sid=abc; path=/';
        localStorage.setItem('seen', '1')</script>"""),
    "/pins": (200, """<style>@keyframes in{from{opacity:0}to{opacity:1}}
        #a{animation:in 5s forwards} #b{transition:opacity 5s} </style>
        <h1 id="a">Animated</h1><p id="b">Text</p><input id="i" value="x">"""),
    "/fonts": (200, """<p style="font-family:'NoSuchFontOpenajar', serif">Fallback</p>
        <p style="font-family:serif">Generic</p>"""),
    "/stream": (200, STYLE + "<h1>Live</h1><script>new EventSource('/events')</script>"),
    # a phone sheet that fills itself when its slide-in transition ends (transitionend)
    "/sheet": (200, STYLE + """<style>#sheet{position:fixed;left:0;right:0;bottom:0;transform:translateY(100%);
        transition:transform .3s} #sheet.open{transform:none}</style>
        <h1>Inbox</h1><button id="compose">Write</button><div id="sheet" role="dialog" aria-label="Write mail"></div>
        <script>const sh = document.getElementById('sheet');
        document.getElementById('compose').onclick = () => sh.classList.add('open');
        sh.addEventListener('transitionend', () => { sh.innerHTML = '<textarea aria-label="Message"></textarea>'; });
        </script>"""),
    # a button a sticky bar lies over at phone width
    "/covered": (200, STYLE + """<style>#bar{position:fixed;left:0;right:0;top:0;height:120px;background:#eee}</style>
        <div id="bar">Tabs</div><button id="go" style="margin-top:40px">Go</button>"""),
    "/poll": (200, STYLE + "<h1>Waiting</h1><script>fetch('/hang')</script>"),
}


async def _serve(route) -> None:
    url = route.request.url
    if not url.startswith(HOST):
        await route.abort()
        return
    path = url[len(HOST):].split("?")[0] or "/"
    if path == "/down":
        await route.abort("connectionrefused")
        return
    if path in ("/events", "/hang"):   # a stream (or a request) the server never ends
        with contextlib.suppress(Exception):
            await asyncio.sleep(30)
        return
    if path == "/slow":
        with contextlib.suppress(Exception):  # the context may be closed while it waits
            await asyncio.sleep(5)
            await route.fulfill(status=200, content_type="text/html", body="<h1>late</h1>")
        return
    status, body = PAGES.get(path, (404, "<h1>no page</h1>"))
    await route.fulfill(status=status, content_type="text/html", body=body)


async def _routes(context) -> None:
    await context.route("**/*", _serve)


FAST = Limits(network_quiet_s=0.05, network_cap_s=1.0, fonts_cap_s=2.0, step_timeout_s=1.5, load_cap_s=2.0, close_cap_s=0.3,
              first_paint_s=0.3, dev_first_paint_s=0.3)


async def _pool_or_skip(**kw) -> ReviewPool:
    pool = ReviewPool(chromium="chrome", on_context=_routes, limits=kw.pop("limits", FAST), **kw)
    try:
        await pool._browser(EngineName.CHROMIUM)
    except (EngineMissing, EngineError) as exc:
        await pool.close()
        pytest.skip(f"no Chrome: {exc}")
    return pool


@pytest.fixture
async def pool():
    pool = await _pool_or_skip()
    yield pool
    await pool.close()


def target(path: str, **kw) -> Target:
    return Target(url=HOST + path, **kw)


BG = "() => getComputedStyle(document.body).backgroundColor"


# ---- width and theme, applied and read back ----

async def test_the_width_scale_and_media_theme_apply_and_read_back(pool):
    async with pool.page(target("/media"), Screen(width=360, height=640, scale=2, theme="dark")) as page:
        shown = await page.evaluate("() => [innerWidth, innerHeight, devicePixelRatio]")
        assert shown == [360, 640, 2]
        assert await page.evaluate(BG) == "rgb(0, 0, 0)"
        report = pool.report(page)
    assert report["viewport"] == {"width": 360, "height": 640} and report["scale"] == 2
    assert report["theme"]["applied"] is True and report["theme"]["media"] == "dark"
    assert report["screen"] == "default@360-dark" and report["engine"] == "chromium"
    assert report["flavor"] == "chrome" and report["engine_version"]
    async with pool.page(target("/media"), Screen(width=1280)) as page:
        assert await page.evaluate("() => innerWidth") == 1280
        assert await page.evaluate(BG) == "rgb(255, 255, 255)"
        assert pool.report(page)["theme"]["applied"] is True


@pytest.mark.parametrize(("path", "switch"), [
    ("/attr", ThemeSwitch(how="attr", name="data-theme", light="light", dark="dark")),
    ("/class", ThemeSwitch(how="class")),
    ("/storage", ThemeSwitch(how="storage", name="theme", light="day", dark="night")),
])
async def test_each_theme_switch_applies_and_reads_back(pool, path, switch):
    for theme, bg in (("dark", "rgb(0, 0, 0)"), ("light", "rgb(255, 255, 255)")):
        async with pool.page(target(path, theme_switch=switch), Screen(width=768, theme=theme)) as page:
            assert await page.evaluate(BG) == bg, (path, theme)
            report = pool.report(page)["theme"]
            assert report["applied"] is True and report["how"] == switch.how and report["wanted"] == theme


async def test_a_media_dark_theme_on_a_page_without_dark_styles_fails(pool):
    with pytest.raises(ThemeNotApplied) as err:
        async with pool.page(target("/no-dark"), Screen(width=360, theme="dark")):
            pass
    assert "no styles for prefers-color-scheme: dark" in err.value.message
    assert err.value.code == "studio_theme_not_applied" and err.value.details["how"] == "media"
    async with pool.page(target("/no-dark"), Screen(width=360, theme="light")) as page:  # light is fine
        assert pool.report(page)["theme"]["applied"] is True


async def test_a_page_that_overrides_the_theme_attribute_fails_with_what_it_read(pool):
    switch = ThemeSwitch(how="attr", name="data-theme", light="light", dark="dark")
    with pytest.raises(ThemeNotApplied) as err:
        async with pool.page(target("/attr-fights", theme_switch=switch), Screen(width=360, theme="dark")):
            pass
    assert err.value.details["read"] == "light" and "'data-theme'" in err.value.message


# ---- states ----

async def test_a_state_is_reached_by_its_steps(pool):
    t = target("/menu", states={"menu": [Step(do="click", selector="#open"),
                                         Step(do="wait_for", selector="[role=dialog]")]})
    async with pool.page(t, Screen(width=360, state="menu")) as page:
        assert await page.is_visible("[role=dialog]")
        assert pool.report(page)["screen"] == "menu@360-light"
    async with pool.page(t, Screen(width=360)) as page:  # default: no steps, a fresh load
        assert not await page.is_visible("[role=dialog]")


async def test_a_failed_step_names_the_state_and_the_step(pool):
    t = target("/menu", states={"menu": [Step(do="click", selector="#open"), Step(do="click", selector="#nope")]})
    with pytest.raises(StateNotReached) as err:
        async with pool.page(t, Screen(width=360, state="menu")):
            pass
    assert err.value.code == "studio_state_failed"
    assert "state 'menu' step 2 of 2 (click #nope)" in err.value.message
    assert err.value.details == {"state": "menu", "step": 2, "do": "click", "selector": "#nope"}


async def test_a_sheet_that_fills_itself_when_its_transition_ends_opens(pool):
    t = target("/sheet", states={"compose": [Step(do="click", selector="#compose"),
                                             Step(do="wait_for", selector="#sheet textarea")]})
    async with pool.page(t, Screen(width=375, state="compose")) as page:
        assert await page.is_visible("#sheet textarea")


async def test_a_failed_step_says_whether_the_click_landed_and_what_was_in_the_way(pool):
    t = target("/covered", states={"go": [Step(do="click", selector="#go")]})
    with pytest.raises(StateNotReached) as err:
        async with pool.page(t, Screen(width=375, state="go")):
            pass
    msg = err.value.message
    assert "The click did not land" in msg and "under div#bar" in msg and "which would take the click" in msg
    t = target("/sheet", states={"compose": [Step(do="click", selector="#compose"),
                                             Step(do="wait_for", selector="#modal textarea")]})
    with pytest.raises(StateNotReached) as err:
        async with pool.page(t, Screen(width=375, state="compose")):
            pass
    msg = err.value.message
    assert "'#modal textarea' matches nothing" in msg
    assert "step 1 (click #compose) landed and the page changed" in msg


async def test_wait_for_that_never_shows_is_a_state_failure(pool):
    with pytest.raises(StateNotReached, match="wait_for '#later'"):
        async with pool.page(target("/menu", wait_for="#later"), Screen(width=360)):
            pass


# ---- faithful failures ----

async def test_a_refused_connection_is_page_not_loaded(pool):
    with pytest.raises(PageNotLoaded) as err:
        async with pool.page(target("/down"), Screen(width=360)):
            pass
    assert "did not load" in err.value.message and "dev server" in err.value.message
    assert err.value.details["url"] == HOST + "/down"


@pytest.mark.parametrize(("path", "status"), [("/error", 500), ("/missing", 404)])
async def test_an_http_error_page_is_a_failure_with_its_status(pool, path, status):
    with pytest.raises(HttpErrorPage) as err:
        async with pool.page(target(path), Screen(width=360)):
            pass
    assert err.value.details["status"] == status and f"HTTP {status}" in err.value.message


@pytest.mark.parametrize("path", ["/blank", "/hidden"])
async def test_a_page_with_nothing_visible_is_blank(pool, path):
    with pytest.raises(BlankPage) as err:
        async with pool.page(target(path), Screen(width=360)):
            pass
    assert err.value.code == "studio_blank_page" and "nothing rendered within 0.3 s" in err.value.message
    assert "pass wait_for" not in err.value.message and "wait_for set to a selector" in err.value.message


async def test_a_page_that_renders_late_is_waited_for_and_checked():
    """A slow app renders after it settled (a Vite dev app on a small VM, QA 2026-11-15 P1-2): the first
    paint is waited for, not reported as a blank page."""
    pool = await _pool_or_skip(limits=dataclasses.replace(FAST, first_paint_s=5.0))
    try:
        async with pool.page(target("/late"), Screen(width=360)) as page:
            assert "Rendered late" in await page.inner_text("body")
            assert any("checked once it rendered" in n for n in pool.report(page)["notes"])
    finally:
        await pool.close()


async def test_a_page_that_never_answers_runs_out_of_the_call_time():
    pool = await _pool_or_skip(limits=Limits(call_timeout_s=1.0, nav_timeout_s=30, close_cap_s=0.3))
    try:
        started = time.monotonic()
        with pytest.raises(StudioTimeout) as err:
            async with pool.page(target("/slow"), Screen(width=360)):
                pass
        assert time.monotonic() - started < 3
        assert err.value.code == "studio_timeout" and "loading the url" in err.value.message
        assert pool.open_pages == 0
    finally:
        await pool.close()


async def test_pages_over_the_cap_wait_and_then_time_out():
    pool = await _pool_or_skip(limits=Limits(max_pages=1, call_timeout_s=1.0, network_quiet_s=0.05, close_cap_s=0.3))
    try:
        async with pool.page(target("/media"), Screen(width=360)) as first:
            assert pool.open_pages == 1
            with pytest.raises(StudioTimeout, match="all 1 review pages stayed busy"):
                async with pool.page(target("/media"), Screen(width=360)):
                    pass
            assert await first.evaluate("() => 1") == 1
        async with pool.page(target("/media"), Screen(width=360)):  # the slot is free again
            pass
    finally:
        await pool.close()


async def test_a_missing_engine_says_the_command_that_installs_it(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))  # an empty folder: no bundled build
    pool = ReviewPool(chromium="bundled", on_context=_routes, limits=FAST)
    try:
        for engine in ("chromium", "webkit"):
            with pytest.raises(EngineMissing) as err:
                async with pool.page(target("/media", engine=engine), Screen(width=360)):
                    pass
            assert err.value.code == "unavailable" and err.value.message.startswith("engine_missing: ")
            assert f"-m playwright install {engine}" in err.value.message
            assert err.value.details == {"engine": engine, "install": engines.install_command(engine)}
            assert catalog.mcp_lines("unavailable", err.value.message)[0] == \
                f"fix: {catalog.entry('unavailable').reasons['engine_missing']}"
        assert pool.open_pages == 0
    finally:
        await pool.close()


# ---- isolation ----

async def test_two_calls_share_no_cookies_or_storage(pool):
    async with pool.page(target("/cookie"), Screen(width=360)) as page:
        assert [c["name"] for c in await page.context.cookies()] == ["sid"]
        assert await page.evaluate("() => localStorage.getItem('seen')") == "1"
        first = page
    assert first.is_closed()
    async with pool.page(target("/media"), Screen(width=360)) as page:
        assert await page.context.cookies() == []
        assert await page.evaluate("() => [document.cookie, localStorage.getItem('seen')]") == ["", None]
        assert page.context is not first.context


async def test_the_context_is_closed_when_the_caller_fails(pool):
    with pytest.raises(RuntimeError):
        async with pool.page(target("/media"), Screen(width=360)) as page:
            kept = page
            raise RuntimeError("the caller broke")
    assert kept.is_closed() and pool.open_pages == 0


async def test_a_page_held_too_long_is_closed_under_the_caller():
    pool = await _pool_or_skip(limits=Limits(hold_timeout_s=0.3, network_quiet_s=0.05, close_cap_s=0.3))
    try:
        async with pool.page(target("/media"), Screen(width=360)) as page:
            await asyncio.sleep(0.6)
            assert page.is_closed()
            assert any("held too long" in note for note in pool.report(page)["notes"])
    finally:
        await pool.close()


# ---- the pins ----

async def test_clock_random_motion_and_caret_are_pinned(pool):
    reads = []
    for _ in range(2):
        async with pool.page(target("/pins"), Screen(width=360)) as page:
            reads.append(await page.evaluate("""() => ({
                now: Date.now(), random: [Math.random(), Math.random()],
                reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
                opacity: getComputedStyle(document.getElementById('a')).opacity,
                transition: getComputedStyle(document.getElementById('b')).transitionDuration,
                caret: getComputedStyle(document.getElementById('i')).caretColor,
                running: document.getAnimations().filter(a => a.playState === 'running').length })"""))
            report = pool.report(page)
    pinned = settle.PINNED_TIME.timestamp() * 1000
    for read in reads:
        assert pinned <= read["now"] < pinned + 30_000
        assert read["reduced"] is True and read["opacity"] == "1" and read["transition"] == "0.001s"
        assert read["caret"] == "rgba(0, 0, 0, 0)" and read["running"] == 0
    assert reads[0]["random"] == reads[1]["random"]
    assert report["clock"] == "2026-01-15T10:30:00Z" and report["fonts_pinned"] is False
    assert "no font set is bundled" in report["fonts"]


async def test_the_report_names_fonts_that_fell_back(pool):
    async with pool.page(target("/fonts"), Screen(width=360)) as page:
        report = pool.report(page)
    assert report["fonts_fell_back"] == [{"family": "NoSuchFontOpenajar", "used": "serif", "elements": 1}]
    assert any("NoSuchFontOpenajar -> serif" in note for note in report["notes"])


# ---- other engines: what is installed here ----

@pytest.mark.parametrize("engine", ["webkit", "firefox"])
async def test_colour_scheme_and_reduced_motion_on_other_engines(engine, monkeypatch):
    """Settles whether colour scheme emulation and reduced motion behave as on Chromium. Skips when
    the engine is not installed, or is installed and does not start here."""
    if not engines.detect()[engine].get("present"):
        pytest.skip(f"{engine} is not installed")
    monkeypatch.setattr(engines, "LAUNCH_TIMEOUT_S", 8)
    pool = ReviewPool(on_context=_routes, limits=Limits(network_quiet_s=0.05, call_timeout_s=30, close_cap_s=0.3))
    try:
        try:
            await pool._browser(EngineName(engine))
        except (EngineMissing, EngineError) as exc:
            pytest.skip(f"{engine} does not start here: {exc.message[:200]}")
        for theme, bg in (("dark", "rgb(0, 0, 0)"), ("light", "rgb(255, 255, 255)")):
            async with pool.page(target("/media", engine=engine), Screen(width=360, theme=theme)) as page:
                assert await page.evaluate(BG) == bg
                assert await page.evaluate("() => matchMedia('(prefers-reduced-motion: reduce)').matches")
                assert await page.evaluate("() => innerWidth") == 360
                assert pool.report(page)["engine"] == engine
        async with pool.page(target("/pins", engine=engine), Screen(width=360)) as page:
            assert await page.evaluate("() => getComputedStyle(document.getElementById('a')).opacity") == "1"
            assert await page.evaluate("() => Date.now()") >= settle.PINNED_TIME.timestamp() * 1000
    finally:
        await pool.close()


async def test_a_live_event_stream_does_not_keep_the_network_busy(pool):
    async with pool.page(target("/stream"), Screen(width=360)) as page:
        report = pool.report(page)
    assert report["network"] == "quiet", report["notes"]
    assert not any("still busy" in n for n in report["notes"])
    async with pool.page(target("/poll"), Screen(width=360)) as page:
        report = pool.report(page)
    assert report["network"] == "busy" and any("still busy" in n for n in report["notes"])
