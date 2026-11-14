"""The diff that names the cause, against ground truth: fixture page pairs with one planted change
each (tests/fixtures/studio/diff/pairs.json) and an unchanged page with a clock and a random id.
Pages are captured in system Chrome through a tiny test-only `ReviewBrowser` (plain Playwright,
pages served through page.route, no network), skipped where Chrome cannot start."""

from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agentos.studio.capture import capture, screens
from agentos.studio.contracts import Screen, Target
from agentos.studio.diff import diff_captures, noise_mask
from agentos.studio.store import StudioStore
from agentos.studio.tokens import from_css

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "studio" / "diff"
PAIRS = json.loads((FIX / "pairs.json").read_text())
BASE = (FIX / "base.html").read_text()
HOST = "https://studio-diff.fixture.example"
SCREEN = "default@1280-light"


def page_for(*names: str) -> str:
    html = BASE
    for name in names:
        for old, new in PAIRS[name]["replace"]:
            assert html.count(old) == 1, f"{name}: {old!r} must be in base.html once"
            html = html.replace(old, new)
    return html


class ChromeReviewBrowser:
    """Satisfies ReviewBrowser for tests only: system Chrome, headless, pages from a dict."""

    fonts_pinned = False

    def __init__(self, browser, pages: dict[str, tuple[str, str]]):
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
                hit = self.pages.get(url[len(HOST):].split("?")[0]) if url.startswith(HOST) else None
                if hit is None:
                    await route.fulfill(status=404, body="") if url.startswith(HOST) else await route.abort()
                else:
                    await route.fulfill(status=200, content_type=hit[0], body=hit[1])

            await page.route("**/*", serve)
            resp = await page.goto(target.url)
            assert resp is not None and resp.status < 400
            await page.evaluate("document.fonts.ready.then(() => true)")
            yield page
        finally:
            await ctx.close()

    async def close(self):
        await self.browser.close()


@pytest.fixture
async def chrome():
    pw = pytest.importorskip("playwright.async_api")
    pytest.importorskip("PIL.Image")
    async with pw.async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:  # noqa: BLE001 - no Chrome here
            pytest.skip(f"no Chrome: {e}")
        pages = {"/photo-a.svg": ("image/svg+xml", (FIX / "photo-a.svg").read_text()),
                 "/photo-b.svg": ("image/svg+xml", (FIX / "photo-b.svg").read_text()),
                 "/": ("text/html", page_for()), "/noise-color": ("text/html", page_for("noise", "color"))}
        for name in PAIRS:
            if not name.startswith("_"):
                pages[f"/{name}"] = ("text/html", page_for(name))
        yield ChromeReviewBrowser(browser, pages)
        await browser.close()


@pytest.fixture
def store(tmp_path):
    s = StudioStore(tmp_path)
    yield s
    s.close()


TOKENS = from_css((FIX / "tokens.css").read_text())


async def _cap(chrome, store, path: str, widths=(1280,)):
    return await capture(chrome, Target(url=HOST + path), screens(widths=widths, themes=["light"]), store=store)


def _screen(result, key=SCREEN):
    return next(s for s in result.screens if s.screen == key)


PLANTED = [n for n in PAIRS if not n.startswith("_") and n != "noise"]


@pytest.mark.parametrize("name", PLANTED)
async def test_a_planted_change_is_named_with_its_cause_and_what_it_moved(chrome, store, name):
    truth = PAIRS[name]
    before = await _cap(chrome, store, "/")
    after = await _cap(chrome, store, f"/{name}")
    result = diff_captures(store, before.id, after.id, tokens=TOKENS)
    sd = _screen(result)
    assert sd.status == "changed", sd
    text = " ".join(sd.lines)
    for part in truth["line"]:
        assert part in text, f"{name}: {part!r} not in {sd.lines}"
    causes = [c for c in sd.changes if c.caused_by is None]
    if truth["cause"] is None:
        assert causes == [] and sd.changes == [], sd.changes   # nothing in the layout is invented
        assert len(sd.unexplained) == 1 and sd.unexplained[0].evidence
        return
    assert len(causes) == 1, f"{name}: one planted change, one cause: {[(c.kind, c.key) for c in causes]}"
    cause = causes[0]
    assert cause.kind == truth["cause"]["kind"]
    if "key" in truth["cause"]:
        assert cause.key == truth["cause"]["key"]
    else:
        assert cause.key.startswith(truth["cause"]["key_starts"]), cause.key
    assert cause.moved_with == truth["moved"]
    assert sd.changes[0] is cause                               # causes first
    assert all(c.caused_by == cause.key for c in sd.changes[1:])
    assert sd.unexplained == [], f"{name}: every changed pixel has a name: {sd.unexplained}"
    assert len(sd.lines) == 1                                  # one sentence per cause


async def test_tokens_name_the_values_before_and_after(chrome, store):
    before = await _cap(chrome, store, "/")
    after = await _cap(chrome, store, "/color")
    sd = _screen(diff_captures(store, before.id, after.id, tokens=TOKENS))
    cause = sd.changes[0]
    assert cause.tokens == {"background-color": ["color.blue.600", "color.blue.700"]}
    assert cause.before["styles"] == {"background-color": "rgb(37, 99, 235)"}
    plain = _screen(diff_captures(store, before.id, after.id))
    assert plain.changes[0].tokens == {}
    assert plain.lines == ["Button 'Save' background-color #2563eb -> #1d4ed8."]


async def test_an_unchanged_page_with_a_clock_and_a_random_id_is_the_same_under_its_noise_mask(chrome, store):
    a = await _cap(chrome, store, "/noise")
    b = await _cap(chrome, store, "/noise")
    c = await _cap(chrome, store, "/noise")
    raw = _screen(diff_captures(store, a.id, c.id))
    assert raw.status == "changed"      # without a mask the clock is a change, honestly reported
    mask = noise_mask(store, a.id, b.id)
    assert mask.screens[SCREEN].rects and mask.screens[SCREEN].keys
    sd = _screen(diff_captures(store, a.id, c.id, mask=mask))
    assert sd.status == "same", sd
    assert sd.changes == [] and sd.unexplained == [] and sd.lines == [] and sd.pixel_share == 0
    # the mask hides the noise, not a real change elsewhere on the page
    d = await _cap(chrome, store, "/noise-color")
    real = _screen(diff_captures(store, a.id, d.id, mask=mask))
    assert real.status == "changed" and [ch.kind for ch in real.changes] == ["restyled"]


async def test_the_same_page_twice_is_the_same_without_a_mask(chrome, store):
    a = await _cap(chrome, store, "/", widths=(360, 1280))
    b = await _cap(chrome, store, "/", widths=(360, 1280))
    result = diff_captures(store, a.id, b.id)
    assert [s.status for s in result.screens] == ["same", "same"]
    assert result.id == diff_captures(store, a.id, b.id).id   # the same input, the same id


async def test_the_wrap_is_named_at_a_narrow_width_too(chrome, store):
    before = await _cap(chrome, store, "/", widths=(360,))
    after = await _cap(chrome, store, "/wrap", widths=(360,))
    sd = _screen(diff_captures(store, before.id, after.id), "default@360-light")
    cause = sd.changes[0]
    assert cause.kind == "text" and cause.caused_by is None and cause.moved_with == 17
    assert "wrapped from" in sd.lines[0] and sd.unexplained == []


async def test_an_unexplained_region_has_a_side_by_side_crop(chrome, store):
    pil = pytest.importorskip("PIL.Image")
    before = await _cap(chrome, store, "/")
    after = await _cap(chrome, store, "/image")
    sd = _screen(diff_captures(store, before.id, after.id))
    region = sd.unexplained[0]
    photo = next(e for e in store.manifest(after.id, SCREEN).elements if e.name == "Team photo").rect
    r = region.rect   # inside the photo, not the page
    assert photo.x - 8 <= r.x and photo.y - 8 <= r.y and r.x + r.w <= photo.x + photo.w + 8
    assert r.y + r.h <= photo.y + photo.h + 8 and r.w * r.h >= photo.w * photo.h / 4
    assert 0 < region.share < 0.1
    with pil.open(io.BytesIO(store.blob(region.evidence))) as im:
        assert im.width > 2 * region.rect.w   # before and after, side by side
