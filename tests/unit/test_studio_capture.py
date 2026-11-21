"""Capture and the layout manifest. The plumbing runs against a fake browser; keys, the cap and a
real capture run in system Chrome through a tiny test-only `ReviewBrowser` (plain Playwright,
pages served through page.route, no network), skipped where Chrome cannot start."""

from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager

import pytest
from test_studio_store import tiny_png

from openajar.core.errors import OpenajarError
from openajar.studio.capture import build_manifest, capture, read_manifest, screens
from openajar.studio.contracts import Screen, Target, Theme
from openajar.studio.store import StudioStore

# ---- the plumbing, with a fake browser ----


class Blank(OpenajarError):
    code = "blank"


class FakePage:
    def __init__(self, seed: int):
        self.seed = seed

    async def evaluate(self, script, arg):
        assert arg["cap"] >= 1
        return {"elements": [{"key": "main", "tag": "main", "role": "main", "rect": {"x": 0, "y": 0, "w": 360, "h": 800},
                              "styles": {"padding": "16px"}, "parent": None}],
                "total": 1, "omitted": 0, "width": 360, "height": 800}

    async def screenshot(self, **kw):
        assert kw["full_page"] is True and kw["type"] == "png"
        return tiny_png(self.seed)


class FakeBrowser:
    fonts_pinned = True

    def __init__(self, fail: set[str]):
        self.fail = fail
        self.opened: list[str] = []

    @asynccontextmanager
    async def page(self, target, screen):
        self.opened.append(screen.key)
        if screen.key in self.fail:
            raise Blank("the page came back blank at this width")
        yield FakePage(screen.width)

    async def close(self):
        pass


async def test_a_failed_screen_is_reported_and_the_rest_are_kept(tmp_path):
    store = StudioStore(tmp_path)
    browser = FakeBrowser(fail={"default@768-dark"})
    cap = await capture(browser, Target(url="https://app.example/"), screens(), store=store)
    assert browser.opened == [s.key for s in screens()]
    assert [s.screen.key for s in cap.shots] == browser.opened   # never silently fewer screens
    bad = [s for s in cap.shots if not s.ok]
    assert len(bad) == 1 and bad[0].screen.key == "default@768-dark"
    assert bad[0].error == "blank: the page came back blank at this width" and bad[0].image is None
    good = [s for s in cap.shots if s.ok]
    assert len(good) == 5 and all(s.image and s.manifest for s in good)
    assert cap.fonts_pinned is True and cap.id.startswith("cap_")
    assert store.get(cap.id) == cap
    m = store.manifest(cap.id, "default@360-light")
    assert m.elements[0].key == "main" and m.complete
    store.close()


async def test_a_capture_with_every_screen_failing_still_returns_and_says_why(tmp_path):
    store = StudioStore(tmp_path)
    cap = await capture(FakeBrowser(fail={s.key for s in screens()}), Target(url="https://app.example/"),
                        screens(), store=store)
    assert len(cap.shots) == 6 and not any(s.ok for s in cap.shots)
    assert store.captures()[0].failed == 6
    store.close()


# ---- in real Chrome ----

HOST = "https://studio.fixture.example"

PAGE = """<!doctype html><html><head><style>
  body { margin: 0; font-family: sans-serif; }
  .card { padding: 16px; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,.2); margin: 12px; }
  @media (prefers-color-scheme: dark) { body { background: #111; color: #eee; } }
  .gone { display: none; }
</style></head><body>
<header><nav aria-label="Main"><a href="/">Home</a><a href="/docs">Docs</a></nav></header>
<main>
  <h1>Welcome</h1>
  <section aria-label="Plans">
    <div class="card" data-testid="plan-free" data-component="PlanCard" data-source="src/Plan.tsx:12">
      <h2>Free</h2><p>Updated <span id="clock"></span> ago</p><button>Choose</button>
    </div>
    <div class="card"><h2>Pro</h2><p>For teams</p><button>Choose</button></div>
    <div class="gone"><p>hidden</p></div>
  </section>
  <form aria-label="Sign up"><label for="em">Email</label><input id="em" type="email"></form>
</main>
<script>
  document.getElementById('clock').textContent = String(Math.floor(Math.random() * 1000)) + ' min';
  for (const el of document.querySelectorAll('div')) el.id = 'gen-' + Math.random().toString(36).slice(2);
</script>
</body></html>"""

IDS = ("<!doctype html><html><body><p>Request <span>f0d573d9-4d48-42c1-a24f-5ee7f3acee6a</span></p>"
       "<p>Build 9f3c2ab on main</p></body></html>")

HUGE = "<!doctype html><html><body><ul>" + "".join(f"<li>Row {i}</li>" for i in range(5000)) + "</ul></body></html>"


class ChromeReviewBrowser:
    """Satisfies ReviewBrowser for tests only: system Chrome, headless, pages from a dict."""

    fonts_pinned = False

    def __init__(self, browser, pages: dict[str, str]):
        self.browser = browser
        self.pages = pages

    @asynccontextmanager
    async def page(self, target: Target, screen: Screen):
        ctx = await self.browser.new_context(viewport={"width": screen.width, "height": screen.height},
                                             device_scale_factor=screen.scale, color_scheme=screen.theme.value,
                                             reduced_motion="reduce", locale=target.locale,
                                             timezone_id=target.timezone)
        try:
            page = await ctx.new_page()

            async def serve(route):
                url = route.request.url
                body = self.pages.get(url[len(HOST):].split("?")[0]) if url.startswith(HOST) else None
                if body is None:
                    await route.fulfill(status=404, body="") if url.startswith(HOST) else await route.abort()
                else:
                    await route.fulfill(status=200, content_type="text/html", body=body)

            await page.route("**/*", serve)
            resp = await page.goto(target.url)
            if resp is None or resp.status >= 400:
                raise Blank(f"the page answered {resp.status if resp else 'nothing'}")
            for step in target.states.get(screen.state, []):
                if step.do == "click":
                    await page.click(step.selector, timeout=500)
            yield page
        finally:
            await ctx.close()

    async def close(self):
        await self.browser.close()


@pytest.fixture
async def chrome():
    pw = pytest.importorskip("playwright.async_api")
    async with pw.async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:  # noqa: BLE001 - no Chrome here
            pytest.skip(f"no Chrome: {e}")
        rb = ChromeReviewBrowser(browser, {"/": PAGE, "/huge": HUGE, "/ids": IDS})
        yield rb
        await browser.close()


async def _manifest(rb: ChromeReviewBrowser, path: str, width: int = 1280, **kw):
    async with rb.page(Target(url=HOST + path), Screen(width=width)) as page:
        return await read_manifest(page, **kw)


async def test_manifest_keys_are_the_same_on_two_loads(chrome):
    a = await _manifest(chrome, "/")
    b = await _manifest(chrome, "/")
    assert [e.key for e in a.elements] == [e.key for e in b.elements]
    assert [e.parent for e in a.elements] == [e.parent for e in b.elements]
    assert len({e.key for e in a.elements}) == len(a.elements)   # every key once


async def test_manifest_keys_read_as_test_id_role_and_name_or_dom_path(chrome):
    m = await _manifest(chrome, "/")
    by = {e.key: e for e in m.elements}
    assert "banner > navigation \"Main\" > link \"Home\"" in by
    assert "main > heading \"Welcome\"" in by
    card = by["testid=plan-free"]
    assert card.parent == "main > region \"Plans\"" and card.component.name == "PlanCard"
    assert card.component.source == "src/Plan.tsx:12" and card.component.via == "attr"
    assert card.styles["padding"] == "16px" and card.styles["border-radius"] == "8px"
    assert "box-shadow" in card.styles and "color" not in card.styles   # no own text: no text styles
    assert "testid=plan-free > button \"Choose\"" in by
    assert "main > region \"Plans\" > button \"Choose\"" in by
    para = by['testid=plan-free > p[1] ~"Updated ago"']   # the random number is not in the key
    assert para.text.startswith("Updated") and para.styles["font-size"] == "16px"
    pro = by['main > region "Plans" > heading "Pro"']   # a role and name skip the unkeyed card around it
    assert pro.tag == "h2" and pro.parent == 'main > region "Plans" > div[2]'
    assert by['main > form "Sign up" > textbox "Email"'].tag == "input"
    assert not any(e.text == "hidden" for e in m.elements)
    assert m.complete and m.total > len(m.elements)


async def test_a_random_id_in_the_text_is_not_in_the_key(chrome):
    # a word with a digit is a count, a time or an id: a uuid in the key made each load a new element, so
    # ui_diff's noise mask (keys learned from two loads) missed it on the third (seen in a full test run)
    keys = [e.key for e in (await _manifest(chrome, "/ids")).elements]
    assert 'p[1] ~"Request"' in keys and 'p[1]/span[1]' in keys and 'p[2] ~"Build on main"' in keys, keys
    assert not any(c in k for k in keys for c in ("f0d573d9", "acee", "ee f", "9f3c2ab")), keys


async def test_manifest_cap_keeps_it_bounded_and_counts_the_rest(chrome):
    m = await _manifest(chrome, "/huge", cap=100)
    assert len(m.elements) == 100 and m.cap == 100
    assert m.total == 5001 and m.omitted == 5001 - 100 and not m.complete
    full = await _manifest(chrome, "/huge", cap=10_000)
    assert len(full.elements) == 5001 and full.omitted == 0
    assert full.elements[3].key == 'list > li[3] ~"Row"' and full.elements[4].key == 'list > li[4] ~"Row"'


async def test_build_manifest_returns_the_elements(chrome):
    async with chrome.page(Target(url=HOST + "/"), Screen(width=360)) as page:
        els = await build_manifest(page)
    assert els and els[0].key.startswith("banner")


async def test_a_real_capture_keeps_images_and_manifests_and_a_failed_screen(chrome, tmp_path):
    store = StudioStore(tmp_path)
    target = Target(url=HOST + "/", states={"menu": [{"do": "click", "selector": "#nothing-here"}]})
    todo = screens(widths=[360, 1280], themes=["light", "dark"]) + screens(widths=[360], themes=["light"], states=["menu"])
    cap = await capture(chrome, target, todo, store=store)
    assert [s.ok for s in cap.shots] == [True, True, True, True, False]
    assert "TimeoutError" in cap.shots[4].error or "timeout" in cap.shots[4].error.lower()
    assert cap.engine_version and "chromium" in cap.engine_version
    png = store.shot_image(cap.id, "default@360-light")
    assert png.startswith(b"\x89PNG")
    light = store.blob(cap.shots[0].image)
    dark = store.blob(cap.shots[1].image)
    assert light != dark   # the theme reached the page
    m = store.manifest(cap.id, "default@1280-dark")
    assert any(e.key == "testid=plan-free" for e in m.elements)
    assert json.loads(store.blob(cap.shots[2].manifest))["cap"] > 0
    pil = pytest.importorskip("PIL.Image")
    small = store.shot_image(cap.id, "default@1280-light", max_width=300)
    with pil.open(io.BytesIO(small)) as im:
        assert im.width == 300
    card = next(e for e in m.elements if e.key == "testid=plan-free")
    crop = store.crop(cap.shots[3].image, card.rect, pad=4, capture_id=cap.id)
    with pil.open(io.BytesIO(store.blob(crop))) as im:
        assert abs(im.width - (card.rect.w + 8)) <= 2
    assert Theme.DARK.value in cap.shots[1].screen.key
    store.close()
