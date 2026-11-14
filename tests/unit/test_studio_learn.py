"""Learning a brand, the parts that need no browser: colour clustering and roles, the spacing step,
radii and the type scale, component hints from class names, theme detection's read back, which
pages may be read, the replace patch a proposal is built from, and a draft that holds no system
proposing nothing."""

from __future__ import annotations

import pytest

from agentos.core.errors import InvalidRequest
from agentos.profiles.service import merge
from agentos.studio.learn import cluster as cl
from agentos.studio.learn.draft import Reads, build_draft
from agentos.studio.learn.learn import MAX_PAGES, signature_changed, site_pages
from agentos.studio.learn.read import ScreenRead, class_roots, origins_from
from agentos.studio.learn.tool import brand_change, replace_patch
from agentos.studio.tokens.color import delta_e, parse_color


def o(value, prop="color", cat="color", loc=None, reach=0, tag="div"):
    return cl.Origin(loc=loc or (0, f"#{value}{prop}", prop), prop=prop, category=cat, value=value, tag=tag,
                     reach=reach)


# ---- colours -------------------------------------------------------------------------------

def test_near_copies_of_a_colour_join_and_close_neighbours_stay_apart():
    origins = [o("#f9fafb", "background-color", loc=(0, "a", "bg")), o("#fafafa", "background-color", loc=(0, "b", "bg")),
               o("#ffffff", "background-color", loc=(0, "c", "bg")), o("#ffffff", "background-color", loc=(0, "d", "bg")),
               o("#ffffff", "background-color", loc=(0, "e", "bg"))]
    clusters = cl.cluster_colors(origins)
    assert [c.center.hex() for c in clusters] == ["#ffffff", "#f9fafb"]
    assert clusters[1].uses == 2 and set(clusters[1].members) == {"#f9fafb", "#fafafa"}


def test_opacity_keeps_colours_apart():
    clusters = cl.cluster_colors([o("rgba(0, 0, 0, 0.5)"), o("rgb(0, 0, 0)")])
    assert len(clusters) == 2


def test_roles_are_guessed_from_use_and_chroma_makes_an_accent():
    origins = [o("#1f2937", "color", loc=(0, "body", "color"), reach=40),
               o("#6b7280", "color", loc=(0, "p", "color")), o("#6b7280", "color", loc=(0, "small", "color")),
               o("#e5e7eb", "border-top-color", loc=(0, "card", "border-color")),
               o("#e5e7eb", "border-top-color", loc=(0, "hr", "border-color")),
               o("#2563eb", "background-color", loc=(0, "btn", "bg")), o("#2563eb", "color", loc=(0, "a", "color")),
               o("#ff00aa", "color", loc=(0, "odd", "color"))]
    clusters = cl.cluster_colors(origins)
    names = {clusters[i].center.hex(): n for i, n in cl.name_colors(clusters).items()}
    # the body's colour is written once but inherited everywhere: it is the first text colour
    assert names == {"#1f2937": "color.text.1", "#6b7280": "color.text.2", "#e5e7eb": "color.border.1",
                     "#2563eb": "color.accent.1"}


# ---- lengths and type ----------------------------------------------------------------------

@pytest.mark.parametrize("values, step", [
    ([4, 8, 12, 16, 24, 32, 8, 16], 4.0),
    ([8, 16, 24, 32, 48, 8, 16], 8.0),
    ([6, 12, 18, 24, 6], 6.0),
    ([7, 13, 22, 9, 31, 5, 17, 21], None),
    ([16, 16, 16], None),                      # one value proves no step
])
def test_the_spacing_step_is_the_largest_that_fits(values, step):
    assert cl.detect_step([float(v) for v in values])[0] == step


def test_a_step_tolerates_rounding_but_not_one_offs():
    step, fit = cl.detect_step([4.0, 8.25, 12.0, 15.75, 24.0, 32.0, 8.0])
    assert step == 4.0 and fit == 1.0
    step, fit = cl.detect_step([4.0, 8.0, 12.0, 7.0, 13.0, 16.0])
    assert step is None and fit < cl.STEP_FIT


def test_radii_are_named_by_size_with_full_apart():
    assert cl.radius_names([8.0, 4.0, 9999.0, 16.0]) == {4.0: "1", 8.0: "2", 16.0: "3", 9999.0: "full"}


def test_the_type_scale_is_named_around_its_base_and_a_steady_ratio_is_found():
    names, ratio = cl.type_scale([12.0, 14.0, 16.0, 20.0, 25.0, 31.25], 16.0)
    assert names == {16.0: "base", 14.0: "sm", 12.0: "xs", 20.0: "lg", 25.0: "xl", 31.25: "2xl"}
    assert ratio == 1.25
    _, ratio = cl.type_scale([16.0, 17.0, 30.0, 31.0], 16.0)
    assert ratio is None


def test_weights_get_their_usual_names():
    assert [cl.weight_name(w) for w in (400.0, 600.0, 700.0, 450.0)] == ["regular", "semibold", "bold", "450"]


# ---- component hints -----------------------------------------------------------------------

def test_repeated_component_classes_are_hints_and_utilities_are_not():
    classes = {"card": 6, "card__title": 6, "card--wide": 2, "btn": 4, "p-4": 30, "text-sm": 20, "flex": 40,
               "hover:bg-blue-500": 9, "active": 5, "Button_root__a1b2c": 3, "ab": 9, "once": 1, "nav-link": 3,
               "md:p-6": 4}
    roots = {r["name"]: r for r in class_roots(classes)}
    assert set(roots) == {"Card", "Btn", "Button", "NavLink"}
    assert roots["Card"]["class_pattern"] == "^card($|__|--)" and roots["Card"]["count"] == 6
    assert roots["Button"]["class_pattern"] == "^Button_"


def test_icon_libraries_and_one_rule_utilities_are_not_component_hints():
    classes = {"card": 6, "card__title": 6, "lucide": 40, "lucide-chevron-right": 12, "fa": 9, "fa-user": 9,
               "glyph": 8, "tabular": 30, "badge": 5}
    roots = {r["name"] for r in class_roots(classes, svg={"glyph": 8}, utility={"tabular": 1})}
    assert roots == {"Card", "Badge"}


def test_origins_count_each_place_once_and_leave_browser_defaults_out():
    records = [
        {"i": 0, "pi": -1, "ps": None, "sel": "body", "tag": "body",
         "v": [["color", "color", "rgb(31, 41, 55)", False, 16], ["margin-top", "space", "8px", True, 16]]},
        {"i": 1, "pi": 0, "ps": None, "sel": "body > p:nth-of-type(1)", "tag": "p",
         "v": [["padding-top", "space", "8px", False, 16], ["padding-left", "space", "8px", False, 16]]},
        {"i": 2, "pi": 0, "ps": None, "sel": "body > p:nth-of-type(2)", "tag": "p", "v": []},
        {"i": 3, "pi": 0, "ps": None, "sel": "body > span", "tag": "span",
         "v": [["color", "color", "rgb(255, 0, 0)", False, 16]]},
    ]
    origins, ua = origins_from(records, 0)
    assert ua == 1
    assert [(x.loc[1], x.value, x.reach) for x in origins] == [
        ("body", "rgb(31, 41, 55)", 2), ("body > p:nth-of-type(1)", "8px", 0), ("body > span", "rgb(255, 0, 0)", 0)]


# ---- theme read back -----------------------------------------------------------------------

def test_a_theme_switch_counts_only_when_the_page_changes():
    light = {"bg": ["rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"], "fg": "rgb(17, 17, 17)", "vars": {"--bg": "#fff"}}
    assert not signature_changed(light, dict(light))
    assert signature_changed(light, {**light, "bg": ["rgb(10, 10, 10)", "rgb(255, 255, 255)"]})
    assert signature_changed(light, {**light, "fg": "rgb(240, 240, 240)"})
    assert signature_changed(light, {**light, "vars": {"--bg": "#000"}})


# ---- pages ---------------------------------------------------------------------------------

def test_extra_pages_are_paths_or_urls_on_the_same_site():
    assert site_pages("http://localhost:3000/", ["/pricing", "http://localhost:3000/a", "/pricing"]) == [
        "http://localhost:3000/", "http://localhost:3000/pricing", "http://localhost:3000/a"]
    with pytest.raises(InvalidRequest, match="same site"):
        site_pages("http://localhost:3000/", ["https://other.example/"])
    with pytest.raises(InvalidRequest, match=f"at most {MAX_PAGES}"):
        site_pages("http://localhost:3000/", [f"/p{i}" for i in range(MAX_PAGES)])
    with pytest.raises(InvalidRequest, match="http or https"):
        site_pages("file:///etc/passwd", None)


# ---- the proposal --------------------------------------------------------------------------

def test_the_replace_patch_leaves_exactly_the_learned_tokens():
    old = {"tokens": {"color": {"$type": "color", "a": {"$value": {"colorSpace": "srgb", "components": [1, 0, 0],
                                                                  "hex": "#ff0000"}, "$description": "old"},
                                "gone": {"$value": "#000000"}}, "motion": {"x": 1}},
           "theme": {"how": "attr", "name": "data-theme", "dark": "dark"}, "widths": [360]}
    learned = {"tokens": {"color": {"$type": "color", "a": {"$value": {"colorSpace": "oklab", "components": [0, 0, 0]}}}},
               "theme": {"how": "class", "dark": "dark"}}
    merged = merge(old, brand_change(old, learned))
    assert merged["tokens"] == learned["tokens"] and merged["theme"] == learned["theme"]
    assert merged["widths"] == [360]                    # what was not learned stays
    assert replace_patch(None, {"a": 1}) == {"a": 1}


def _screen(origins, theme="light", page=0, width=1280):
    return ScreenRead(page=page, width=width, theme=theme, origins=origins)


def test_a_draft_with_a_system_is_valid_dtcg_and_labels_its_guesses():
    origins = [o("#1f2937", loc=(0, "body", "color"), reach=20), o("#6b7280", loc=(0, "p1", "color")),
               o("#6b7280", loc=(0, "p2", "color")),
               *[o(f"{px}px", "padding-top", "space", loc=(0, f"s{i}", "padding")) for i, px in
                 enumerate([4, 8, 12, 16, 24, 8, 16])]]
    dark = [o("#f9fafb", loc=(0, "body", "color"), reach=20), o("#9ca3af", loc=(0, "p1", "color")),
            o("#9ca3af", loc=(0, "p2", "color"))]
    draft = build_draft(Reads(url="http://localhost:3000/", pages=["/"], light=[_screen(origins)],
                              dark=[_screen(dark, "dark")], screens=["default@1280-light", "default@1280-dark"]))
    from agentos.profiles.sections import check_tokens  # imported here: sections loads through models

    assert draft.categories["color"].system == "found" and draft.space_step_px == 4.0
    check_tokens(draft.brand["tokens"])                 # what profile_propose will validate
    tree = draft.brand["tokens"]
    t1 = tree["light"]["color"]["text"]["1"]
    assert t1["$extensions"]["agentos"]["heuristic"] is True and t1["$description"].startswith("heuristic")
    dark1 = parse_color(tree["dark"]["color"]["text"]["1"]["$value"]["hex"])
    assert delta_e(dark1, parse_color("#f9fafb")) < 0.002
    assert tree["base"]["space"]["3"]["$value"] == {"value": 12, "unit": "px"}
    assert "light and dark" in draft.reason() and len(draft.reason()) <= 500
    small = draft.summary()
    assert small["palette"][0]["name"] == "color.text.1" and small["palette"][0]["dark"] == "#f9fafb"
    assert "heuristic" in small


def test_values_that_fit_nothing_make_no_system_and_no_brand():
    origins = [o(c, loc=(0, f"e{i}", "color")) for i, c in enumerate(["#123456", "#abcdef", "#ff8800", "#00aa11"])]
    origins += [o(f"{px}px", "padding-top", "space", loc=(0, f"s{i}", "padding")) for i, px in enumerate([7, 13, 22, 31])]
    draft = build_draft(Reads(url="http://x.dev/", pages=["/"], light=[_screen(origins)], screens=["default@1280-light"]))
    assert not draft.has_system and draft.brand == {}
    assert draft.categories["color"].one_offs == 4 and draft.categories["color"].coverage == 0.0
    assert any("no consistent system" in u for u in draft.uncertain)
    assert any("no dark theme found" in u for u in draft.uncertain)


def test_the_tool_is_in_the_profiles_toolset_and_says_no_em_dash():
    from agentos.mcp.toolsets import TOOLSETS
    from agentos.studio import tools

    specs = {s.name: s for s in tools.tool_specs(None)}
    assert specs["profile_learn_brand"].tags == ("profiles",)
    assert specs["profile_learn_brand"].hidden_without == "profiles"
    assert specs["profile_learn_brand"].effect.value == "draft"
    assert "profile_learn_brand" in next(t for t in TOOLSETS if t.name == "profiles").tools
    assert "—" not in (specs["profile_learn_brand"].handler.__doc__ or "")
