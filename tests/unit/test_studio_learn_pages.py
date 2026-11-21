"""Learning a brand from real pages in system Chrome, through the review browser, with the pages
of tests/fixtures/studio/learn served through context.route (no network). Skipped where Chrome
cannot start.

Ground truth:
- declared.html declares its tokens as custom properties (one of them unused) and turns dark
  with a data-theme attribute: the tokens keep their own names, with use counts;
- hidden.html and hidden-pricing.html declare nothing but hold to a system (hidden.css says
  which): the palette within delta E 0.01, the 4 px spacing step and the type scale come back,
  and dark by media query;
- messy.html holds to nothing: low coverage, many one-offs, and nothing proposed.
And profile_learn_brand saves a draft the owner must approve, never an approved version.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from test_profiles import Ctx

from openajar.core.errors import EngineError, ServiceUnavailable
from openajar.core.models import Principal
from openajar.core.runtime import Runtime
from openajar.profiles.errors import ProfileInvalid
from openajar.profiles.models import ProfileState
from openajar.profiles.service import build
from openajar.studio.browser import EngineMissing, Limits, ReviewPool
from openajar.studio.contracts import EngineName
from openajar.studio.learn import learn_brand
from openajar.studio.learn.tool import profile_learn_brand
from openajar.studio.tokens.color import delta_e, parse_color

pw = pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.asyncio(loop_scope="module")

FIX = Path(__file__).parent.parent / "fixtures" / "studio" / "learn"
HOST = "https://brand.fixture.example"
TYPES = {"html": "text/html", "css": "text/css"}
FAST = Limits(network_quiet_s=0.05, network_cap_s=1.0, fonts_cap_s=2.0, load_cap_s=2.0, close_cap_s=0.3)

# what hidden.css holds to (its comment says the same)
HIDDEN_PALETTE = {"#1f2937": "text", "#6b7280": "text", "#f9fafb": "background", "#ffffff": "background",
                  "#e5e7eb": "border", "#2563eb": "accent"}
HIDDEN_DARK = {"#1f2937": "#f9fafb", "#6b7280": "#9ca3af", "#f9fafb": "#111827", "#e5e7eb": "#374151"}
COLOUR_TOLERANCE = 0.01        # delta E OK


async def _serve(route) -> None:
    url = route.request.url
    if not url.startswith(HOST):
        await route.abort()
        return
    path = url[len(HOST):].split("?")[0].lstrip("/")
    f = FIX / path
    if not path or not f.is_file():
        await route.fulfill(status=404, content_type="text/html", body="<h1>no page</h1>")
        return
    await route.fulfill(status=200, content_type=TYPES[path.rsplit(".", 1)[1]], body=f.read_text())


async def _routes(context) -> None:
    await context.route("**/*", _serve)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pool():
    pool = ReviewPool(chromium="chrome", on_context=_routes, limits=FAST)
    try:
        await pool._browser(EngineName.CHROMIUM)
    except (EngineMissing, EngineError) as exc:
        await pool.close()
        pytest.skip(f"no Chrome: {exc}")
    yield pool
    await pool.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def hidden(pool):
    return await learn_brand(pool, f"{HOST}/hidden.html", pages=["/hidden-pricing.html"])


def _palette(draft) -> dict[str, dict]:
    return {row["light"]: row for row in draft.palette}


def _near(hex_value: str, palette: dict[str, dict]) -> dict | None:
    want = parse_color(hex_value)
    best = min(palette, key=lambda h: delta_e(parse_color(h), want))
    return palette[best] if delta_e(parse_color(best), want) <= COLOUR_TOLERANCE else None


# ---- declared tokens ----------------------------------------------------------------------

async def test_declared_tokens_keep_their_names_and_say_how_often_they_are_used(pool):
    d = await learn_brand(pool, f"{HOST}/declared.html")
    for cat in ("color", "space", "radius", "font_size"):
        assert d.categories[cat].source == "declared" and d.categories[cat].system == "declared", cat
    assert d.categories["color"].coverage == 1.0
    names = {t.name: t for t in d.tokens.tokens}
    assert names["color.accent"].css_var == "--color-accent" and "space.4" in names and "radius.lg" in names
    pal = {r["name"]: r for r in d.palette}
    assert pal["color.text"]["light"] == "#18181b" and pal["color.text"]["dark"] == "#fafafa"
    assert pal["color.accent"]["how"] == "declared" and "role_guess" not in pal["color.accent"]
    tree = d.brand["tokens"]
    unused = tree["base"]["color"]["unused"]["$extensions"]["openajar"]
    accent = tree["base"]["color"]["accent"]["$extensions"]["openajar"]
    assert unused["css_rules"] == 0 and unused["heuristic"] is False
    assert accent["css_rules"] >= 1 and accent["css_elements"] >= 3
    assert d.categories["color"].note == "1 declared token(s) no rule uses"
    assert d.theme == {"how": "attr", "name": "data-theme", "light": "light", "dark": "dark"}
    comps = {c["name"]: c for c in d.components}
    assert comps["Card"]["attr"] == "data-component=Card" and comps["Button"]["attr"] == "data-slot=button"
    # the page's text is never kept: not the headings, not the card copy
    assert "First card" not in str(d.model_dump()) and "Three" not in str(d.model_dump())


# ---- a hidden system ----------------------------------------------------------------------

async def test_a_hidden_palette_is_recovered_with_its_roles_and_dark_values(hidden):
    assert hidden.categories["color"].source == "clustered" and hidden.categories["color"].system == "found"
    pal = _palette(hidden)
    assert len(pal) == len(HIDDEN_PALETTE)
    for hex_value, role in HIDDEN_PALETTE.items():
        row = _near(hex_value, pal)
        assert row is not None, f"{hex_value} was not recovered"
        assert row["role_guess"] == role and row["how"] == "heuristic", hex_value
    for light, dark in HIDDEN_DARK.items():
        got = _near(light, pal)["dark"]
        assert delta_e(parse_color(got), parse_color(dark)) <= COLOUR_TOLERANCE, (light, got)
    assert _near("#1f2937", pal)["name"] == "color.text.1"          # the body's colour comes first
    assert hidden.theme == {"how": "media"}


async def test_a_hidden_spacing_step_radii_and_type_scale_are_recovered(hidden):
    assert hidden.space_step_px == 4.0 and hidden.categories["space"].coverage == 1.0
    spaces = {t.name: t.value for t in hidden.tokens.tokens if t.category.value == "space"}
    assert spaces == {"space.1": "4px", "space.2": "8px", "space.3": "12px", "space.4": "16px", "space.6": "24px",
                      "space.8": "32px"}
    radii = {t.name: t.value for t in hidden.tokens.tokens if t.category.value == "radius"}
    assert radii == {"radius.1": "4px", "radius.2": "8px", "radius.full": "9999px"}
    assert hidden.type_scale == {"sm": 14.0, "base": 16.0, "lg": 20.0, "xl": 28.0, "2xl": 36.0}
    weights = {t.value for t in hidden.tokens.tokens if t.category.value == "font_weight"}
    assert weights == {"600", "800"}
    assert hidden.fonts == [{"family": "Inter", "role": "body"}]
    assert {c["name"] for c in hidden.components} >= {"Card", "Btn", "Tag", "NavLink"}
    # an icon library's classes (lucide) and a one-rule utility (.tabular) are not components
    assert not {c["name"] for c in hidden.components} & {"Lucide", "LucideCheck", "Tabular"}
    assert all(c["via"] == "repeated class (heuristic)" for c in hidden.components)
    assert hidden.uncertain == [] or all("dark value varies" in u for u in hidden.uncertain)
    assert len(hidden.screens) == 8 and hidden.pages == ["/hidden.html", "/hidden-pricing.html"]


# ---- a messy page -------------------------------------------------------------------------

async def test_a_messy_page_reports_low_coverage_and_does_not_pretend_to_a_system(pool):
    d = await learn_brand(pool, f"{HOST}/messy.html")
    assert not d.has_system and d.brand == {}
    for cat in ("color", "space", "radius", "font_size", "font_family"):
        c = d.categories[cat]
        assert c.system == "none" and (c.coverage or 0) < 0.5 and c.tokens == 0, cat
    assert d.categories["color"].one_offs >= 15 and d.categories["space"].one_offs >= 10
    assert d.space_step_px is None and d.theme is None
    assert sum("no consistent system" in u for u in d.uncertain) >= 5


async def test_a_page_that_does_not_load_fails_the_call(pool):
    from openajar.studio.browser import HttpErrorPage

    with pytest.raises(HttpErrorPage):
        await learn_brand(pool, f"{HOST}/nothing-here.html")
    d = await learn_brand(pool, f"{HOST}/declared.html", pages=["/gone.html"])
    assert d.failed == ["/gone.html at 360 px: studio_http_error", "/gone.html at 1280 px: studio_http_error"]


# ---- the tool -----------------------------------------------------------------------------

PRODUCT = {"profile": "shop", "workspace": "biz-a", "kind": "product", "display_name": "Acme Shop",
           "brand": {"widths": [360, 1280], "tokens": {"color": {"$type": "color", "old": {"$value": "#123456"}}}}}


def _runtime(tmp_path, pool, where: str = "node") -> Runtime:
    runtime = Runtime()
    build(runtime, home=tmp_path)
    runtime.register("studio", SimpleNamespace(browser=pool, where=where))
    return runtime


async def test_the_tool_proposes_a_draft_the_owner_must_approve(tmp_path, pool, hidden):
    runtime = _runtime(tmp_path, pool)
    svc = runtime.require("profiles")
    svc.save(PRODUCT, by={"kind": "human", "id": "owner"}, as_owner=True)
    agent = Ctx(runtime, Principal(kind="agent", id="agent-1"))
    out = await profile_learn_brand(agent, profile="biz-a/shop", url=f"{HOST}/hidden.html",
                                          pages=["/hidden-pricing.html"])
    assert out["proposal"]["state"] == "draft" and out["proposal"]["version"] == 2
    assert out["counts"]["color"] == 6 and out["space_step_px"] == 4.0 and "next" in out
    assert "openajar profile approve biz-a/shop --version 2" in out["owner_action"] and "panel" in out["next"]
    assert agent.seen[-1] == ("draft", None)
    assert svc.store.active("biz-a", "shop").version == 1           # nothing approved
    v2 = svc.store.version("biz-a", "shop", 2)
    assert v2.state is ProfileState.DRAFT
    assert v2.reason.startswith("Learned from brand.fixture.example: 2 page(s)")
    brand = v2.definition.brand
    assert set(brand.tokens) == {"base", "light", "dark", "$extensions"}    # the old tokens replaced
    assert "#123456" not in str(brand.tokens) and brand.widths == [360, 1280]   # the rest kept
    assert brand.theme.how == "media" and {c.name for c in brand.components} >= {"Card", "Btn"}
    assert brand.fonts[0].family == "Inter"
    # small result (AX rule 3): no token tree, no page text
    assert "tokens" not in out and len(str(out)) < 6000
    # an owner in person proposing is still a draft
    owner = Ctx(runtime, Principal(kind="human", id="owner"), in_person=True)
    again = await profile_learn_brand(owner, profile="biz-a/shop", url=f"{HOST}/declared.html")
    assert again["proposal"]["state"] == "draft"


async def test_the_tool_proposes_nothing_for_a_messy_site_and_refuses_other_kinds(tmp_path, pool):
    runtime = _runtime(tmp_path, pool)
    svc = runtime.require("profiles")
    svc.save(PRODUCT, by={"kind": "human", "id": "owner"}, as_owner=True)
    svc.save({"profile": "eye", "workspace": "biz-a", "kind": "reviewer", "display_name": "Eye"},
             by={"kind": "human", "id": "owner"}, as_owner=True)
    agent = Ctx(runtime, Principal(kind="agent", id="agent-1"))
    out = await profile_learn_brand(agent, profile="biz-a/shop", url=f"{HOST}/messy.html")
    assert out["proposal"] is None and "nothing was proposed" in out["next"]
    assert len(svc.store.versions("biz-a", "shop")) == 1
    with pytest.raises(ProfileInvalid, match="product or business"):
        await profile_learn_brand(agent, profile="biz-a/eye", url=f"{HOST}/declared.html")
    bare = Runtime()
    build(bare, home=tmp_path / "bare")
    bare.require("profiles").save(PRODUCT, by={"kind": "human", "id": "owner"}, as_owner=True)
    with pytest.raises(ServiceUnavailable, match="studio"):
        await profile_learn_brand(Ctx(bare, Principal(kind="agent", id="a")), profile="biz-a/shop",
                                        url=f"{HOST}/declared.html")


async def test_on_a_laptop_the_tool_says_the_command_that_approves_it_there(tmp_path, pool, hidden):
    runtime = _runtime(tmp_path, pool, where="local")
    runtime.require("profiles").save(PRODUCT, by={"kind": "human", "id": "owner"}, as_owner=True)
    agent = Ctx(runtime, Principal(kind="agent", id="agent-1"))
    out = await profile_learn_brand(agent, profile="biz-a/shop", url=f"{HOST}/hidden.html")
    assert "openajar studio profile approve biz-a/shop --version 2" in out["owner_action"]
    assert "openajar studio profile approve biz-a/shop --version 2" in out["next"] and "OPENAJAR_HOME" not in out["next"]
