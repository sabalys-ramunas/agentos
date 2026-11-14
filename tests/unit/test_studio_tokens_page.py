"""Studio tokens against real pages in system Chrome: every value planted in the fixtures is
found and nothing else is, in light, dark and with values inlined at build time; the probe
leaves the page as it found it; tokens are read from a live page. Pages are served from
tests/fixtures/studio/tokens through page.route, no network. Skipped where Chrome cannot start."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import pytest_asyncio

from agentos.studio.contracts import CheckContext, ComponentRule, Screen, Theme, TokenSet
from agentos.studio.tokens import (
    adherence,
    analyse,
    check_and_adherence,
    findings_from,
    load_tokens,
    read_page_tokens,
    system_check,
    tokens_from_page,
)
from agentos.studio.tokens.color import delta_e, parse_color

pw = pytest.importorskip("playwright.async_api")

FIX = Path(__file__).parent.parent / "fixtures" / "studio" / "tokens"
HOST = "https://studio.fixture.example"
CDN = "https://cdn.other.example"
TRUTH = json.loads((FIX / "truth.json").read_text())
COMPONENTS = [ComponentRule(**c) for c in TRUTH["components"]]
PLANTED = {p["id"]: p for p in TRUTH["planted"]}
TYPES = {"html": "text/html", "css": "text/css", "js": "text/javascript"}

# Extra bodies for pages built from the fixtures in the route.
REACTS = """<script>
new MutationObserver(() => document.body.classList.add('touched'))
  .observe(document.documentElement, {attributes: true, attributeFilter: ['style']});
</script><style>.touched .lead { color: #e11d48; }</style></head>"""
CROSS = f'<link rel="stylesheet" href="{CDN}/extra.css"></head>'


pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser():
    # one browser for the module: system Chrome takes seconds to close
    async with pw.async_playwright() as p:
        try:
            b = await p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:  # noqa: BLE001 - no Chrome here: the pure tests still run
            pytest.skip(f"no Chrome: {e}")
        yield b
        await b.close()


@pytest_asyncio.fixture(loop_scope="module")
async def chrome(browser):
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()

    async def serve(route):
        url = route.request.url
        if url.startswith(CDN):
            await route.fulfill(status=200, content_type="text/css",
                                body=":root { --color-extra: #123456; --space-extra: 40px; }")
            return
        if not url.startswith(HOST):
            await route.abort()
            return
        path, _, query = url[len(HOST):].lstrip("/").partition("?")
        file = FIX / path
        if not file.is_file():
            await route.fulfill(status=404, body="")
            return
        body = file.read_text()
        if path.endswith(".html"):
            if "theme=dark" in query:
                body = body.replace('data-theme="light"', 'data-theme="dark"')
            if "reacts" in query:
                body = body.replace("</head>", REACTS, 1)
            if "cross" in query:
                body = body.replace("</head>", CROSS, 1)
        await route.fulfill(status=200, content_type=TYPES[path.rsplit(".", 1)[1]], body=body)

    await page.route("**/*", serve)
    yield page
    await page.unroute_all(behavior="ignoreErrors")
    await context.close()


async def open_page(page, name: str, query: str = ""):
    await page.goto(f"{HOST}/{name}" + (f"?{query}" if query else ""))
    if "x-badge" in await page.content():
        await page.wait_for_function("() => !!document.querySelector('x-badge').shadowRoot")


@pytest.fixture(scope="module")
def tokens() -> TokenSet:
    return load_tokens(FIX / "resolver.json")


def ctx(theme: str = "light", **kw) -> CheckContext:
    return CheckContext(screen=Screen(width=1280, theme=Theme(theme)), **kw)


def plants_of(finding) -> set[str]:
    sels = finding.measured.get("selectors") or [finding.where.selector]
    return {pid for pid in PLANTED for s in sels if re.search(rf"#{pid}(?![\w-])", s)}


def found(out) -> tuple[dict[str, str], list]:
    """plant id -> check, and the findings that name no plant (false positives)."""
    hits, stray = {}, []
    for f in out.findings:
        ids = plants_of(f)
        if not ids:
            stray.append(f)
        for i in ids:
            hits[i] = f.check
    return hits, stray


# --- ground truth -------------------------------------------------------------------------

@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_every_planted_value_is_found_and_nothing_else(chrome, tokens, theme):
    await open_page(chrome, "planted.html", f"theme={theme}")
    out = await system_check(chrome, tokens, COMPONENTS, ctx(theme))
    hits, stray = found(out)
    assert not stray, [(f.check, f.measured) for f in stray]
    expected = {pid: p["check"] for pid, p in PLANTED.items()}
    assert hits == expected
    by_plant = {next(iter(plants_of(f))): f for f in out.findings if len(plants_of(f)) == 1}
    for pid, p in PLANTED.items():
        f = by_plant[pid]
        if "match" in p:
            assert f.measured["match"] == p["match"], pid
            assert f.measured["method"] == "probe", pid
        if "token" in p:
            assert f.expected["token"] == p["token"], pid
        if "component" in p:
            assert f.component is not None and f.component.name == p["component"], pid
        if "origin" in p:
            assert f.measured.get("origin") == p["origin"], pid
    assert all(c.status == "ran" for c in out.coverage)


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_the_clean_page_has_no_findings_and_full_adherence(chrome, tokens, theme):
    await open_page(chrome, "clean.html", f"theme={theme}")
    out, adh = await check_and_adherence(chrome, tokens, COMPONENTS, ctx(theme))
    assert out.findings == []
    assert {c.check for c in out.coverage if c.status == "ran"} == {
        "token.color", "token.space", "token.radius", "token.font", "token.shadow", "token.border",
        "token.hardcoded", "component.hand_rolled"}
    assert set(adh.by_category.values()) == {1.0}
    assert adh.interactive_coverage == 1.0 and adh.hand_rolled == 0
    assert 0 < adh.area_coverage < 1


async def test_values_inlined_at_build_time_fall_back_to_value_matching(chrome, tokens):
    await open_page(chrome, "inlined.html")
    out = await system_check(chrome, tokens, COMPONENTS, ctx())
    hits, stray = found(out)
    assert not stray
    expected = {pid: p["check"] for pid, p in PLANTED.items() if p.get("inlined") != "missed"}
    assert hits == expected        # the look-alikes cannot be seen here, and are not claimed
    assert {f.measured.get("method") for f in out.findings if f.check.startswith("token.")} == {"value"}
    cov = {c.check: c for c in out.coverage}
    assert cov["token.hardcoded"].status == "skipped"
    assert "build time" in cov["token.hardcoded"].reason
    assert "by value only" in cov["token.color"].reason


async def test_tokens_with_no_custom_properties_are_matched_by_value_and_say_so(chrome, tokens):
    await open_page(chrome, "planted.html")
    bare = TokenSet(tokens=[t.model_copy(update={"css_var": None}) for t in tokens.tokens])
    out = await system_check(chrome, bare, COMPONENTS, ctx())
    cov = {c.check: c for c in out.coverage}
    assert cov["token.hardcoded"].status == "skipped"
    assert "custom propert" in cov["token.hardcoded"].reason
    hits, _ = found(out)
    assert "p-look-color" not in hits and hits["p-off-color"] == "token.color"


# --- the shape of a finding ---------------------------------------------------------------

async def test_findings_are_grouped_stable_and_fixable(chrome, tokens):
    await open_page(chrome, "planted.html")
    first = await system_check(chrome, tokens, COMPONENTS, ctx())
    await chrome.reload()
    await chrome.wait_for_function("() => !!document.querySelector('x-badge').shadowRoot")
    second = await system_check(chrome, tokens, COMPONENTS, ctx())
    assert [f.id for f in first.findings] == [f.id for f in second.findings]
    assert len({f.id for f in first.findings}) == len(first.findings)
    look = next(f for f in first.findings if "p-look-color" in plants_of(f))
    assert look.check == "token.hardcoded" and look.confidence.value == "measured"
    assert look.expected["css_var"] == "--color-action-primary"
    assert "var(--color-action-primary)" in look.fix and look.measured["value"] == "#3b82f6"
    assert look.seen_at[0].screen == "default@1280-light" and look.seen_at[0].count == 1
    near = next(f for f in first.findings if "p-near-color" in plants_of(f))
    assert 0.005 < near.measured["delta_eok"] < 0.02
    hand = next(f for f in first.findings if f.check == "component.hand_rolled")
    assert hand.confidence.value == "heuristic" and hand.why_heuristic
    for f in first.findings:
        text = json.dumps(f.model_dump(mode="json"))
        assert "\u2014" not in text
        assert f.fix and len(f.fix) < 240


async def test_only_runs_the_checks_asked_for(chrome, tokens):
    await open_page(chrome, "planted.html")
    out = await system_check(chrome, tokens, COMPONENTS, ctx(only=["token.space"]))
    assert {f.check for f in out.findings} == {"token.space"}
    assert {c.check for c in out.coverage} == {"token.space"}


async def test_without_component_rules_hand_rolled_is_skipped_not_clean(chrome, tokens):
    await open_page(chrome, "planted.html")
    out = await system_check(chrome, tokens, [], ctx())
    cov = {c.check: c for c in out.coverage}
    assert cov["component.hand_rolled"].status == "skipped"
    assert not [f for f in out.findings if f.check == "component.hand_rolled"]
    adh = await adherence(chrome, tokens, [])
    assert adh.interactive_coverage is None and adh.area_coverage is None


async def test_adherence_on_the_planted_page(chrome, tokens):
    await open_page(chrome, "planted.html")
    adh = await adherence(chrome, tokens, COMPONENTS)
    assert adh.interactive_coverage == 0.6 and adh.hand_rolled == 2
    assert all(0 < v < 1 for v in adh.by_category.values())
    assert 0 < adh.area_coverage < 1


# --- the probe leaves the page as it found it ---------------------------------------------

SNAPSHOT = """() => {
  const out = [];
  const walk = (root) => { for (const el of root.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    out.push([el.localName, cs.color, cs.backgroundColor, cs.padding, cs.fontSize, cs.lineHeight, cs.boxShadow,
              cs.fontFamily, cs.fontWeight, cs.borderRadius, el.getAttribute('style')]);
    if (el.shadowRoot) walk(el.shadowRoot);
  } };
  walk(document);
  return {styles: out, n: document.querySelectorAll('style').length, key: '__agentosStudioTokens' in window,
          frames: document.querySelectorAll('iframe').length};
}"""


async def test_the_probe_restores_the_page(chrome, tokens):
    await open_page(chrome, "planted.html", "theme=dark")
    before = await chrome.evaluate(SNAPSHOT)
    a = await analyse(chrome, tokens, COMPONENTS, theme="dark")
    after = await chrome.evaluate(SNAPSHOT)
    assert before == after
    assert not any("reloaded" in n for n in a.notes)


async def test_a_page_that_reacts_to_the_probe_is_reloaded_and_says_so(chrome, tokens):
    await open_page(chrome, "clean.html", "reacts=1")
    out = await system_check(chrome, tokens, COMPONENTS, ctx())
    reasons = " ".join(c.reason or "" for c in out.coverage)
    assert "did not come back" in reasons and "reloaded" in reasons
    # and after the reload it is the page it was
    assert not await chrome.evaluate("() => document.body.classList.contains('touched')")


async def test_the_probe_stops_at_its_time_cap(chrome, tokens):
    await open_page(chrome, "planted.html")
    before = await chrome.evaluate(SNAPSHOT)
    out = await system_check(chrome, tokens, COMPONENTS, ctx(rules={"probe_timeout_s": 0.001}))
    reasons = " ".join(c.reason or "" for c in out.coverage)
    assert "time" in reasons
    assert await chrome.evaluate(SNAPSHOT) == before


# --- tokens from a live page --------------------------------------------------------------

async def test_tokens_read_from_the_page_match_the_token_file(chrome, tokens):
    await open_page(chrome, "planted.html")
    live = await tokens_from_page(chrome)
    for t in tokens.tokens:
        if t.category.value != "color":
            continue
        same = [x for x in live.tokens if x.css_var == t.css_var and set(t.themes) <= set(x.themes)]
        assert same, t.name
        assert delta_e(parse_color(same[0].value), parse_color(t.value)) < 0.002, t.name
    # and the page's own tokens drive a check as well as the file does
    out = await system_check(chrome, live, COMPONENTS, ctx())
    hits, _ = found(out)
    assert hits.get("p-look-color") == "token.hardcoded" and hits.get("p-off-color") == "token.color"


async def test_a_cross_origin_stylesheet_is_listed_and_can_be_supplied(chrome, tokens):
    await open_page(chrome, "clean.html", "cross=1")
    got = await read_page_tokens(chrome)
    assert got.unreadable == [f"{CDN}/extra.css"]
    assert not [t for t in got.tokens.tokens if t.css_var == "--color-extra"]
    got = await read_page_tokens(chrome, css_texts={f"{CDN}/extra.css": ":root { --color-extra: #123456; }"})
    assert got.unreadable == [] and [t for t in got.tokens.tokens if t.css_var == "--color-extra"]
    out = await system_check(chrome, tokens, COMPONENTS, ctx())
    assert out.findings == []
    assert any("cross-origin" in (c.reason or "") for c in out.coverage)


async def test_a_tailwind_page_traces_its_spacing_scale(chrome):
    await open_page(chrome, "tailwind.html")
    tw = await tokens_from_page(chrome)
    assert [s.step_px for s in tw.scales if s.category.value == "space"] == [4]
    a = await analyse(chrome, tw, [], theme="light")
    pads = {(v.rec["sel"], v.value): v for v in a.values if v.category == "space"}
    derived = [v for (sel, _), v in pads.items() if sel.startswith("html > body > div.p-3")]
    assert derived and derived[0].prov.kind == "derived" and derived[0].prov.token == "spacing"
    assert derived[0].prov.factor == 3
    out = findings_from(a, ctx())
    got = {(f.check, f.where.selector) for f in out.findings}
    assert got == {("token.hardcoded", "#p-look-scale"), ("token.space", "#p-near-scale"),
                   ("token.color", "#p-off-color")}
    look = next(f for f in out.findings if f.check == "token.hardcoded")
    assert look.expected["token"] == "space.scale.3"


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_a_tailwind_app_on_its_own_system_has_no_findings(browser, theme):
    """The near misses the first run on a real app found: half steps, a dot and a pill, sr-only's
    -1px margin, px line heights from a type scale, and colours written with light-dark()."""
    context = await browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=theme)
    page = await context.new_page()

    async def serve(route):
        name = route.request.url.split("/")[-1]
        if not route.request.url.startswith(HOST) or not (FIX / name).is_file():
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=(FIX / name).read_text())

    try:
        await page.route("**/*", serve)
        await page.goto(f"{HOST}/tailwind_near.html")
        ts = await tokens_from_page(page)
        bg = [t for t in ts.tokens if t.name == "background"]
        assert sorted(t.themes[0].value for t in bg) == ["dark", "light"], bg
        want = {"light": "#ffffff", "dark": "#0a0a0a"}[theme]
        assert parse_color(next(t for t in bg if t.themes == [Theme(theme)]).value).hex() == want
        assert {t.name for t in ts.tokens} >= {"type.14", "type.14.line", "foreground.subtle"}
        out = await system_check(page, ts, [], ctx(theme))
        assert out.findings == [], [(f.check, f.where.selector, f.measured.get("value")) for f in out.findings]
    finally:
        await context.close()


# --- the colour parser agrees with the browser --------------------------------------------

COLOURS = ["#3b82f6", "rebeccapurple", "rgb(10 20 30 / 0.5)", "hsl(200 50% 40%)", "hwb(120 10% 20%)",
           "lab(52 40 -30)", "lch(60 50 280)", "oklch(0.7 0.15 30)", "oklab(0.5 0.1 -0.1)",
           "color(display-p3 0.2 0.6 0.3)", "color(rec2020 0.5 0.4 0.3)", "color(a98-rgb 0.3 0.7 0.2)",
           "color(prophoto-rgb 0.4 0.3 0.8)", "color(xyz-d50 0.3 0.4 0.2)", "color(srgb-linear 0.2 0.3 0.4)"]


async def test_the_python_colour_parser_agrees_with_chrome(chrome):
    await chrome.set_content("<div id=x></div>")
    got = await chrome.evaluate("""(cs) => cs.map(c => {
      const el = document.getElementById('x');
      el.style.color = `color-mix(in oklab, ${c} 100%, ${c} 0%)`;
      return getComputedStyle(el).color; })""", COLOURS)
    for text, chrome_value in zip(COLOURS, got, strict=True):
        ours, theirs = parse_color(text), parse_color(chrome_value)
        assert theirs is not None, chrome_value
        assert delta_e(ours, theirs) < 0.001, (text, chrome_value, ours.css())
        assert abs(ours.alpha - theirs.alpha) < 0.01


async def test_what_four_designers_found_on_their_own_tokens_is_clean(browser):
    """--r-md is a radius because border-radius uses it; a ring and an inset edge are lines, not
    shadows; transparent text paints nothing; a drawn page (role=img) and a region marked
    data-studio="ignore" are another design system, left out and counted."""
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()

    async def serve(route):
        name = route.request.url.split("/")[-1]
        if not route.request.url.startswith(HOST) or not (FIX / name).is_file():
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=(FIX / name).read_text())

    try:
        await page.route("**/*", serve)
        await page.goto(f"{HOST}/designer_near.html")
        ts = await tokens_from_page(page)
        radius = {t.name: t.value for t in ts.tokens if t.category.value == "radius"}
        assert radius == {"r.md": "8px", "r.sm": "4px"}, radius
        a = await analyse(page, ts, [], theme="light")
        out = findings_from(a, ctx())
        assert out.findings == [], [(f.check, f.where.selector, f.measured.get("value")) for f in out.findings]
        assert any("2 regions left out" in n for n in a.notes), a.notes
    finally:
        await context.close()

