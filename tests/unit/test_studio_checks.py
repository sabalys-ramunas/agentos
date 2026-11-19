"""Ground truth for the measured UI checks, in real Chrome, from local files only.

Every check has fixture pages with the fault planted (tests/fixtures/studio/checks/faults) and
near-miss pages that must stay clean (.../clean): an off-canvas drawer, a deliberate ellipsis
with a title, a sticky header, an open modal, decorative overflow, screen-reader-only text and
more. A fault page must give its check and nothing else; a clean page must give nothing.
Skips where Chrome cannot start, like tests/unit/test_siteapi_chrome.py.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
import pytest_asyncio

from agentos.studio.checks import IDS, delta, merge, run_checks
from agentos.studio.checks.scripts import script
from agentos.studio.contracts import CheckContext, Confidence, EngineName, Screen, Severity, Theme

pw = pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.asyncio(loop_scope="module")

EM_DASH = chr(0x2014)
FIX = Path(__file__).parent.parent / "fixtures" / "studio" / "checks"
HOST = "https://studio.test"
CLEAN = sorted(p.stem for p in (FIX / "clean").glob("*.html"))
# Pages that change after load: give them time to do it before measuring.
SETTLE_MS = {"layout_shift": 700, "long_task": 900, "health_console": 300}


def _png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


IMAGES = {
    "img/small.png": _png(100, 100, (40, 120, 200)),
    "img/big.png": _png(1600, 1200, (200, 180, 90)),
    "img/hero.png": _png(600, 450, (90, 140, 170)),
    "img/dark.png": _png(600, 450, (20, 24, 40)),
    "img/retina.png": _png(400, 300, (120, 90, 60)),
}
TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser():
    async with pw.async_playwright() as p:
        try:
            b = await p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:  # noqa: BLE001 - no Chrome here
            pytest.skip(f"no Chrome: {e}")
        yield b
        await b.close()


async def _serve(route):
    url = route.request.url
    if not url.startswith(HOST):
        await route.abort()
        return
    path = url[len(HOST):].split("?")[0].lstrip("/")
    if path in IMAGES:
        await route.fulfill(status=200, content_type="image/png", body=IMAGES[path])
        return
    file = FIX / path
    if file.is_file():
        await route.fulfill(status=200, content_type=TYPES.get(file.suffix, "application/octet-stream"),
                            body=file.read_bytes())
    else:
        await route.fulfill(status=404, content_type="text/plain", body="not found")


class Opened:
    def __init__(self, browser):
        self.browser = browser
        self.contexts = []

    async def __call__(self, name: str, width: int = 360, theme: Theme = Theme.LIGHT, height: int = 800):
        ctx = await self.browser.new_context(viewport={"width": width, "height": height},
                                             color_scheme=theme.value, reduced_motion="reduce")
        self.contexts.append(ctx)
        page = await ctx.new_page()
        await page.route("**/*", _serve)
        await page.goto(f"{HOST}/{name}.html", wait_until="load")
        await page.evaluate("document.fonts.ready.then(() => true)")
        await page.wait_for_timeout(SETTLE_MS.get(name.rsplit("/", 1)[-1], 150))
        return page, Screen(width=width, height=height, theme=theme)

    async def close(self):
        for c in self.contexts:
            await c.close()


@pytest_asyncio.fixture(loop_scope="module")
async def opened(browser):
    o = Opened(browser)
    yield o
    await o.close()


# The one page whose planted fault is a long task: it blocks for 500 ms of wall-clock time, however
# fast the machine is.
LONG_TASK_PAGES = ("faults/long_task",)


def measures_the_machine(f, url: str) -> bool:
    """A long task on a page that plants none. Every page here does little or no work of its own, so
    its long tasks are the browser's work as a new tab loads (first style, layout and paint, and the
    layout an inline script forces, as showModal does): about 90 ms on a fast laptop, two tasks with the
    longest 363 ms on a slow macOS runner. They measure the machine, not the page."""
    return f.check == "health.long_tasks" and not any(url.endswith(f"/{p}.html") for p in LONG_TASK_PAGES)


async def measure(page, ctx, **kw):
    """run_checks, without the findings that measure the machine. Every other check has to come out
    exactly as each test says, and health.long_tasks still runs on every page (its coverage says so)."""
    out = await run_checks(page, ctx, **kw)
    url = getattr(page, "url", "")
    return out.model_copy(update={"findings": [f for f in out.findings if not measures_the_machine(f, url)]})


async def check(opened, name, width=360, theme=Theme.LIGHT, **kw):
    page, screen = await opened(name, width, theme)
    return await measure(page, CheckContext(screen=screen, **kw))


def by_check(out):
    res: dict[str, list] = {}
    for f in out.findings:
        res.setdefault(f.check, []).append(f)
    return res


# --- every planted fault is found, and nothing else on its page --------------------------------

FAULTS = {
    "page_overflow": {"layout.page_overflow"},
    "page_overflow_word": {"layout.page_overflow"},
    "clipped": {"layout.clipped"},
    "clipped_surface": {"layout.clipped"},
    "truncated_ellipsis": {"text.truncated"},
    "truncated_cut": {"text.truncated"},
    "truncated_clamp": {"text.truncated"},
    "truncated_readmore": {"text.truncated"},
    "occluded": {"layout.occluded"},
    "occluded_button": {"layout.occluded"},
    "text_overlap": {"layout.text_overlap"},
    "contrast": {"contrast.text"},
    "contrast_gradient": {"contrast.text"},
    "contrast_opacity": {"contrast.text"},
    "contrast_ghost": {"contrast.text"},
    "contrast_small_backdrop": {"contrast.text"},
    "target": {"target.size"},
    "target_links": {"target.size"},
    "names": {"name.missing"},
    "health_console": {"health.console"},
    "health_requests": {"health.requests"},
    "health_fonts": {"health.fonts"},
    "health_images": {"health.images"},
    "health_lazy": {"health.images"},
    "layout_shift": {"health.layout_shift"},
    "long_task": {"health.long_tasks"},
    "occluded_scroll": {"layout.occluded"},
    "ignored_covers": {"layout.occluded"},
    "ignore_selector": {"contrast.text"},
    "grouped": {"contrast.text"},
    "rows": {"contrast.text"},
    "truncated_cut_title": {"text.truncated"},
}


@pytest.mark.parametrize("name", sorted(FAULTS))
async def test_a_planted_fault_is_found_and_nothing_else(opened, name):
    out = await check(opened, f"faults/{name}")
    # long tasks depend on how fast this machine is (a busy CI runner has them on any page): not a planted fault
    findings = [f for f in out.findings if f.check != "health.long_tasks" or f.check in FAULTS[name]]
    found = {f.check for f in findings}
    assert found == FAULTS[name], [(f.check, f.where.selector, f.measured) for f in out.findings]
    for f in findings:
        assert f.fix and EM_DASH not in f.fix
        assert f.seen_at[0].screen == "default@360-light"


async def test_page_overflow_names_the_one_element_that_causes_it(opened):
    out = await check(opened, "faults/page_overflow")
    [f] = by_check(out)["layout.page_overflow"]
    assert f.component and f.component.name == "Hero" and f.component.source == "src/Hero.tsx:12"
    assert f.measured["overflow_px"] > 200 and f.measured["viewport"] == 360
    assert f.where.selector.endswith("div")
    word = by_check(await check(opened, "faults/page_overflow_word"))["layout.page_overflow"][0]
    assert word.where.selector.endswith("p")


async def test_truncation_reports_what_is_shown_and_the_share_hidden(opened):
    [f] = by_check(await check(opened, "faults/truncated_ellipsis"))["text.truncated"]
    assert f.measured["kind"] == "ellipsis" and f.severity == Severity.MINOR
    assert f.measured["shown"].startswith("Quarterly") and 0.2 < f.measured["hidden_share"] < 0.9
    assert f.measured["visible_chars"] < f.measured["total_chars"]
    [cut] = by_check(await check(opened, "faults/truncated_cut"))["text.truncated"]
    assert cut.measured["kind"] == "cut" and cut.severity == Severity.MAJOR and not cut.measured["reachable"]
    [clamp] = by_check(await check(opened, "faults/truncated_clamp"))["text.truncated"]
    assert clamp.measured["kind"] == "clamp" and clamp.component.name == "ProductCard"


async def test_occlusion_names_the_cover(opened):
    [f] = by_check(await check(opened, "faults/occluded"))["layout.occluded"]
    assert f.where.selector == "#price" and "div" in f.measured["cover"] and f.measured["kind"] == "text"
    [b] = by_check(await check(opened, "faults/occluded_button"))["layout.occluded"]
    assert b.where.role == "button" and b.measured["kind"] == "control" and "a" in b.measured["cover"]


async def test_contrast_uses_the_real_background(opened):
    [f] = by_check(await check(opened, "faults/contrast"))["contrast.text"]
    assert f.measured["route"] == "dom" and 2.2 < f.measured["ratio"] < 2.4
    assert f.measured["fg"] == "#aaaaaa" and f.measured["bg"] == "#ffffff" and f.measured["apca_lc"] > 0
    assert "#" in f.fix
    [g] = by_check(await check(opened, "faults/contrast_gradient"))["contrast.text"]
    assert g.measured["route"] == "pixels" and g.measured["why_pixels"] == "image"
    assert g.measured["ratio"] < 1.5 and g.measured["share_below"] > 0.5 and g.expected["min_ratio"] == 3.0
    [gh] = by_check(await check(opened, "faults/contrast_ghost"))["contrast.text"]
    assert gh.measured["route"] == "pixels" and gh.measured["bg_at_worst"] == "#3b3b3b"
    [o] = by_check(await check(opened, "faults/contrast_opacity"))["contrast.text"]
    assert o.measured["route"] == "pixels" and o.measured["why_pixels"] == "opacity" and o.measured["ratio"] < 4.5


async def test_small_text_on_a_blurred_bar_is_judged_with_its_css_colour(opened):
    [f] = by_check(await check(opened, "faults/contrast_small_backdrop"))["contrast.text"]
    assert f.measured["route"] == "pixels" and f.measured["why_pixels"] == "backdrop"
    assert f.measured["fg_from"] == "css" and f.measured["fg_at_worst"] == "#a3a3a3"
    assert 2.3 < f.measured["ratio"] < 2.7 and f.seen_at[0].count == 2


# --- what the first run on a real app (agentos's web app) got wrong, kept clean -------------------


async def test_small_text_over_dots_gradients_and_blur_is_read_with_its_real_colour(opened):
    out = await check(opened, "clean/contrast_small_text", only=["contrast"])
    assert out.findings == [], [(f.where.name, f.measured) for f in out.findings]
    reason = {c.check: c.reason for c in out.coverage}["contrast.text"] or ""
    assert "separator glyphs" in reason


async def test_text_inside_aria_hidden_pictures_is_not_judged_by_any_text_check(opened):
    out = await check(opened, "clean/decorative_text")
    assert out.findings == []
    cov = {c.check: c.reason or "" for c in out.coverage}
    for c in ("contrast.text", "text.truncated"):
        assert "aria-hidden" in cov[c] and "decorative" in cov[c]


async def test_a_feed_that_fades_out_is_neither_cut_nor_covered(opened):
    for width in (360, 1280):
        out = await check(opened, "clean/fading_feed", width)
        assert out.findings == [], [(f.check, f.where.name, f.measured) for f in out.findings]
        reason = {c.check: c.reason for c in out.coverage}["contrast.text"] or ""
        assert "under a fade" in reason


async def test_a_2x_image_is_not_oversized_and_an_8x_one_is(opened):
    assert by_check(await check(opened, "clean/retina_image", only=["health.images"])) == {}
    [big] = [f for f in by_check(await check(opened, "faults/health_images"))["health.images"]
             if f.measured["problem"] == "oversized"]
    assert big.measured["factor"] >= 3 and "2 times" in big.expected["rule"]


async def test_target_size_folds_repeats_into_one_finding(opened):
    [f] = by_check(await check(opened, "faults/target"))["target.size"]
    assert f.seen_at[0].count == 2 and f.component.name == "Toolbar"
    assert f.measured["w"] == 16 and f.expected["min_px"] == 24


async def test_missing_names_cover_buttons_images_and_fields(opened):
    found = by_check(await check(opened, "faults/names"))["name.missing"]
    assert sorted(f.measured["role"] for f in found) == ["button", "img", "textbox"]
    [button] = [f for f in found if f.measured["role"] == "button"]
    assert button.component.name == "icon-button" and button.component.via == "attr"


async def test_the_44_px_level_is_information_and_only_on_request(opened):
    assert "target.size" not in by_check(await check(opened, "clean/inline_links"))
    out = await check(opened, "clean/inline_links", rules={"target.size": {"aaa": True}})
    infos = by_check(out)["target.size"]
    assert infos and {f.severity for f in infos} == {Severity.INFO}
    assert all(f.measured["level"] == "AAA" for f in infos)


async def test_health_reports_console_requests_fonts_and_images(opened):
    con = by_check(await check(opened, "faults/health_console"))["health.console"]
    assert sorted(f.measured["kind"] for f in con) == ["console.error", "uncaught"]
    req = by_check(await check(opened, "faults/health_requests"))["health.requests"]
    assert sorted(f.measured["url"].rsplit("/", 1)[-1] for f in req) == ["missing.css", "missing.json"]
    assert {f.measured["status"] for f in req} == {404}
    [font] = by_check(await check(opened, "faults/health_fonts"))["health.fonts"]
    assert font.measured["family"] == "Brand"
    imgs = by_check(await check(opened, "faults/health_images"))["health.images"]
    assert sorted(f.measured["problem"] for f in imgs) == ["no_dimensions", "oversized"]
    [lazy] = by_check(await check(opened, "faults/health_lazy"))["health.images"]
    assert lazy.measured["problem"] == "lazy_largest" and lazy.confidence == Confidence.MEASURED


@pytest.mark.parametrize("delay_s, confidence", [(0.6, Confidence.MEASURED), (2.5, Confidence.HEURISTIC)])
async def test_a_lazy_hero_from_a_slow_server_is_still_found(browser, delay_s, confidence):
    """A lazy image does not hold up the load event: read at once, the largest paint was the text
    under it (seen on a busy CI runner). The check waits for it, and past its wait says so."""
    import asyncio

    async def slow(route):
        if route.request.url.endswith("/img/hero.png"):
            await asyncio.sleep(delay_s)
        await _serve(route)

    ctx = await browser.new_context(viewport={"width": 360, "height": 800}, reduced_motion="reduce")
    try:
        page = await ctx.new_page()
        await page.route("**/*", slow)
        await page.goto(f"{HOST}/faults/health_lazy.html", wait_until="load")
        out = await measure(page, CheckContext(screen=Screen(width=360, height=800, theme=Theme.LIGHT)))
    finally:
        await ctx.close()
    [lazy] = by_check(out)["health.images"]
    assert lazy.measured["problem"] == "lazy_largest" and lazy.confidence == confidence
    assert set(by_check(out)) == {"health.images"}


async def test_layout_shift_and_long_tasks_are_measured_on_chromium(opened):
    [ls] = by_check(await check(opened, "faults/layout_shift"))["health.layout_shift"]
    assert ls.measured["cls"] > 0.1
    assert "cause" not in ls.measured and ls.confidence == Confidence.MEASURED   # no web font: the page's own
    [lt] = by_check(await check(opened, "faults/long_task"))["health.long_tasks"]
    assert lt.measured["blocking_ms"] > 300
    assert lt.measured["scripts"][0]["source"] == f"{HOST}/faults/long_task.html"  # the page's own work


async def test_other_engines_say_layout_shift_and_long_tasks_were_skipped(opened):
    out = await check(opened, "faults/layout_shift", engine=EngineName.FIREFOX)
    cov = {c.check: c for c in out.coverage}
    for c in ("health.layout_shift", "health.long_tasks"):
        assert cov[c].status == "skipped" and "firefox" in cov[c].reason
    assert "health.layout_shift" not in by_check(out)


# --- near misses stay clean ---------------------------------------------------------------------


@pytest.mark.parametrize("width", [360, 1280])
@pytest.mark.parametrize("name", CLEAN)
async def test_a_clean_page_gives_no_findings(opened, name, width):
    out = await check(opened, f"clean/{name}", width)
    assert out.findings == [], [(f.check, f.where.selector, f.measured) for f in out.findings]
    assert {c.status for c in out.coverage} <= {"ran", "skipped"}


async def test_the_modal_page_says_only_the_modal_was_checked(opened):
    out = await check(opened, "clean/modal")
    reason = {c.check: c.reason for c in out.coverage}["contrast.text"] or ""
    assert "modal is open" in reason


async def test_the_contrast_pixel_route_ran_on_the_clean_photo_page(opened):
    page, screen = await opened("clean/contrast_ok")
    shots = []
    orig = page.screenshot

    async def spy(**kw):
        shots.append(kw.get("style"))
        return await orig(**kw)

    page.screenshot = spy
    out = await measure(page, CheckContext(screen=screen, only=["contrast"]))
    assert out.findings == [] and len(shots) >= 4


# --- coverage, only, rules, the pixel route failing ---------------------------------------------


async def test_coverage_names_every_check_on_every_call(opened):
    out = await check(opened, "faults/contrast", only=["contrast", "layout.page_overflow"])
    cov = {c.check: c for c in out.coverage}
    assert set(cov) == set(IDS)
    assert cov["contrast.text"].status == "ran" and cov["layout.page_overflow"].status == "ran"
    assert cov["target.size"].status == "skipped" and "not asked for" in cov["target.size"].reason
    assert cov["theme.leak"].status == "skipped"


async def test_rules_change_the_thresholds(opened):
    out = await check(opened, "faults/contrast", rules={"contrast.text": {"min_ratio": 2.0}})
    assert "contrast.text" not in by_check(out)
    out = await check(opened, "faults/target", rules={"target.size.min_px": 16})
    assert "target.size" not in by_check(out)


async def test_when_the_pixel_route_cannot_run_coverage_says_so_and_nothing_is_guessed(opened):
    page, screen = await opened("faults/contrast_gradient")

    async def broken(**kw):
        raise RuntimeError("screenshots are off")

    page.screenshot = broken
    out = await measure(page, CheckContext(screen=screen, only=["contrast.text"]))
    assert out.findings == []
    [cov] = [c for c in out.coverage if c.check == "contrast.text"]
    assert cov.status == "ran" and "not judged" in cov.reason and "screenshots are off" in cov.reason


async def test_the_page_is_scrolled_back_after_hit_testing(opened):
    page, screen = await opened("clean/sticky_header")
    await page.evaluate("window.scrollTo(0, 300)")
    await measure(page, CheckContext(screen=screen))
    assert await page.evaluate("scrollY") == 300


# --- theme leak ---------------------------------------------------------------------------------


async def test_theme_leak_finds_a_hard_coded_copy_of_a_theme_colour(opened):
    dark, screen = await opened("faults/theme_leak", theme=Theme.DARK)
    light, _ = await opened("faults/theme_leak", theme=Theme.LIGHT)
    out = await measure(dark, CheckContext(screen=screen, only=["theme"]), other_theme=light)
    [f] = by_check(out)["theme.leak"]
    assert f.component.name == "Promo" and f.measured["property"] == "background-color"
    assert f.measured["copies"] == ["--surface"] and f.measured["value"] == "#f4f4f5"
    assert f.seen_at[0].screen == "default@360-dark" and "var(--surface)" in f.fix


async def test_theme_leak_is_clean_when_colours_follow_the_theme(opened):
    dark, screen = await opened("clean/theme_ok", theme=Theme.DARK)
    light, _ = await opened("clean/theme_ok", theme=Theme.LIGHT)
    out = await measure(dark, CheckContext(screen=screen), other_theme=light)
    assert out.findings == []
    assert {c.check: c.status for c in out.coverage}["theme.leak"] == "ran"


async def test_theme_leak_reads_colours_written_with_light_dark(opened):
    dark, screen = await opened("faults/theme_leak_light_dark", theme=Theme.DARK)
    light, _ = await opened("faults/theme_leak_light_dark", theme=Theme.LIGHT)
    out = await measure(dark, CheckContext(screen=screen, only=["theme"]), other_theme=light)
    [f] = by_check(out)["theme.leak"]
    assert f.component.name == "Promo" and f.measured["copies"] == ["--surface"]
    assert f.measured["var_in_dark"] == "#222222"
    dark, screen = await opened("clean/theme_ok_light_dark", theme=Theme.DARK)
    light, _ = await opened("clean/theme_ok_light_dark", theme=Theme.LIGHT)
    out = await measure(dark, CheckContext(screen=screen, only=["theme"]), other_theme=light)
    assert out.findings == [] and {c.check: c.status for c in out.coverage}["theme.leak"] == "ran"


async def test_theme_leak_without_the_other_theme_is_skipped_with_the_reason(opened):
    out = await check(opened, "clean/theme_ok", only=["theme.leak"])
    [cov] = [c for c in out.coverage if c.check == "theme.leak"]
    assert cov.status == "skipped" and "other_theme" in cov.reason


# --- merge and delta across real runs -----------------------------------------------------------


async def test_merge_keeps_one_finding_per_cause_across_screens_with_stable_ids(opened):
    a = await check(opened, "faults/target", 360)
    b = await check(opened, "faults/target", 768)
    again = await check(opened, "faults/target", 360)
    assert [f.id for f in a.findings] == [f.id for f in again.findings]
    run = merge({"default@360-light": a, "default@768-light": b})
    [f] = run.findings
    assert sorted(s.screen for s in f.seen_at) == ["default@360-light", "default@768-light"]
    assert run.id.startswith("run_") and len(run.coverage) == 2 * len(IDS)


async def test_delta_reads_a_fix(opened):
    page, screen = await opened("faults/contrast")
    ctx = CheckContext(screen=screen)
    before = merge([await measure(page, ctx)])
    await page.add_style_tag(content=".hint { color: #555 !important; }")
    after = merge([await measure(page, ctx)])
    d = delta(before, after)
    assert d.fixed == [before.findings[0].id] and d.new == [] and d.still == []


# --- what four designers found wrong on real pages (docs/design/*.md, the studio sections) ------


def reasons(out) -> dict[str, str]:
    return {c.check: c.reason or "" for c in out.coverage}


@pytest.mark.parametrize("width", [360, 768])
async def test_rows_scrolled_out_of_a_scroll_box_are_out_of_view_not_covered(opened, width):
    out = await check(opened, "clean/scroll_box", width, only=["layout"])
    assert out.findings == [], [(f.check, f.where.selector, f.measured) for f in out.findings]


async def test_text_covered_inside_a_scroll_box_is_still_covered(opened):
    [f] = by_check(await check(opened, "faults/occluded_scroll"))["layout.occluded"]
    assert f.where.selector == "#first" and "div" in f.measured["cover"]


async def test_stacked_before_and_after_layers_are_not_covered_and_coverage_says_so(opened):
    out = await check(opened, "clean/stacked_layers", 768, only=["layout"])
    assert out.findings == [], [(f.check, f.where.name, f.measured) for f in out.findings]
    assert "stacked on purpose" in reasons(out)["layout.occluded"]


async def test_text_in_drawn_pictures_inert_copies_and_miniatures_is_left_out_and_counted(opened):
    out = await check(opened, "clean/pictures", 768)
    assert out.findings == [], [(f.check, f.where.name, f.measured) for f in out.findings]
    why = reasons(out)
    for c in ("contrast.text", "text.truncated"):
        assert "inside pictures" in why[c] and "role=img" in why[c] and "under 4 px" in why[c]


async def test_the_label_of_a_disabled_control_is_not_judged_for_contrast(opened):
    out = await check(opened, "clean/disabled_labels", only=["contrast"])
    assert out.findings == []
    assert "disabled control" in reasons(out)["contrast.text"]


async def test_a_cut_text_whose_full_text_is_in_a_tooltip_or_title_is_not_reported(opened):
    out = await check(opened, "clean/truncated_tooltip", 768, only=["text"])
    assert out.findings == [], [(f.where.name, f.measured) for f in out.findings]


async def test_a_cut_with_a_title_asks_for_the_ellipsis_its_title_already_goes_with(opened):
    [f] = by_check(await check(opened, "faults/truncated_cut_title"))["text.truncated"]
    assert f.measured["kind"] == "cut" and f.measured["reachable"] and f.measured["full_text_in"] == "title"
    assert f.severity == Severity.MINOR and "text-overflow: ellipsis" in f.fix and "title" in f.fix


async def test_a_region_marked_data_studio_ignore_is_left_out_and_named_in_coverage(opened):
    out = await check(opened, "clean/ignored_region")
    assert out.findings == []
    why = reasons(out)
    for c in ("contrast.text", "text.truncated", "target.size", "name.missing"):
        assert "left out on purpose: 1 region" in why[c] and "the failing chip" in why[c], why[c]


async def test_an_ignored_region_that_covers_the_page_is_still_reported(opened):
    [f] = by_check(await check(opened, "faults/ignored_covers"))["layout.occluded"]
    assert f.where.role == "button" and "exhibit" not in (f.where.selector or "")


async def test_ignore_selectors_from_the_caller_leave_a_region_out_and_say_so(opened):
    out = await check(opened, "faults/ignore_selector", rules={"ignore": [".other-brand"]})
    assert out.findings == []
    assert "ignore: .other-brand" in reasons(out)["contrast.text"]
    bad = await check(opened, "faults/ignore_selector", rules={"ignore": ["[[nope"]})
    assert "contrast.text" in by_check(bad)
    assert "not valid CSS" in reasons(bad)["contrast.text"]


async def test_marks_and_drawn_pages_keep_their_colours_without_a_theme_leak(opened):
    dark, screen = await opened("clean/theme_marks", theme=Theme.DARK)
    light, _ = await opened("clean/theme_marks", theme=Theme.LIGHT)
    out = await measure(dark, CheckContext(screen=screen), other_theme=light)
    assert out.findings == [], [(f.check, f.where.selector, f.measured) for f in out.findings]
    why = reasons(out)["theme.leak"]
    assert "kept on purpose" in why and "regions left out" in why


async def test_without_component_markers_findings_group_by_section_or_part_and_say_how(opened):
    from agentos.studio import shaping

    out = await check(opened, "faults/grouped")
    comps = sorted((f.component.name, f.component.via) for f in out.findings)
    assert comps == [(".tabbar", "class"), ('section "Billing"', "none"), ('section "Team"', "none")]
    groups = shaping.grouped(out.findings, limit=None)
    assert all("grouped_by" in g for g in groups)
    assert {g["grouped_by"].split(" (")[0] for g in groups} == {
        "the nearest landmark or titled section", "the nearest class that names a part"}


async def test_ids_hold_when_rows_are_reordered_and_differ_between_hash_routes(opened):
    page, screen = await opened("faults/rows")
    ctx = CheckContext(screen=screen, only=["contrast"])
    [first] = (await measure(page, ctx)).findings
    await page.evaluate("render([3, 2, 1, 0])")
    [moved] = (await measure(page, ctx)).findings
    assert moved.id == first.id and moved.where.selector != first.where.selector
    await page.evaluate("location.hash = '#/jobs/2'")
    [routed] = (await measure(page, ctx)).findings
    assert routed.id != first.id


async def test_a_bar_drawn_at_a_rows_edge_does_not_send_its_text_to_the_pixel_route(opened):
    page, _ = await opened("clean/contrast_pseudo_bar")
    raw = await page.evaluate(script("page"), {"checks": ["contrast.text"]})
    res = raw["checks"]["contrast.text"]
    assert res["jobs"] == [] and len(res["items"]) == 2



FONT_FILES = [Path("/System/Library/Fonts/Supplemental/Arial.ttf"), Path("/Library/Fonts/Arial.ttf"),
              Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/TTF/DejaVuSans.ttf")]


async def test_a_shift_from_a_web_font_swapping_in_is_named_and_marked_as_varying(browser):
    import asyncio

    font = next((p for p in FONT_FILES if p.is_file()), None)
    if font is None:
        pytest.skip("no font file on this machine to serve as the web font")
    ctx = await browser.new_context(viewport={"width": 360, "height": 800})
    try:
        page = await ctx.new_page()

        async def serve(route):
            if route.request.url.endswith("/fonts/slow.ttf"):
                await asyncio.sleep(0.5)    # a cold cache: the font arrives after the first paint
                await route.fulfill(status=200, content_type="font/ttf", body=font.read_bytes())
                return
            await _serve(route)

        await page.route("**/*", serve)
        await page.goto(f"{HOST}/faults/font_swap.html", wait_until="load")
        await page.evaluate("document.fonts.ready.then(() => true)")
        await page.wait_for_timeout(300)
        out = await measure(page, CheckContext(screen=Screen(width=360), only=["health.layout_shift"]))
        perf = await page.evaluate(script("perf"), {"wait_ms": 60})
        assert "health.layout_shift" in by_check(out), perf
        [f] = by_check(out)["health.layout_shift"]
        assert f.measured["cause"] == "a web font swapping in" and f.confidence == Confidence.HEURISTIC
        assert "font-display" in f.fix and "another run" in f.why_heuristic
    finally:
        await ctx.close()


async def test_studios_own_full_page_screenshot_is_not_a_layout_shift_of_the_page(opened):
    from agentos.studio.checks.run import mark_capture

    page, screen = await opened("clean/viewport_shell", 375, height=800)
    await mark_capture(page)                      # what review does before its screenshot
    await page.screenshot(full_page=True)         # grows the viewport to the page's height
    await page.wait_for_timeout(100)
    out = await measure(page, CheckContext(screen=screen, only=["health"]))
    assert "health.layout_shift" not in by_check(out), [f.measured for f in out.findings]
