"""Studio tokens without a browser: colour and length parsing, matching, loading DTCG, CSS and
Tailwind themes, and export. The page side is in test_studio_tokens_page.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openajar.core.errors import InvalidRequest
from openajar.studio.contracts import Scale, Theme, Token, TokenCategory, TokenSet
from openajar.studio.tokens import from_css, from_dtcg, from_tailwind, load_tokens, match, to_css, to_dtcg
from openajar.studio.tokens.color import EXACT_DE, NEAR_DE, delta_e, parse_color
from openajar.studio.tokens.match import scale_px
from openajar.studio.tokens.units import parse_length, parse_shadow

FIX = Path(__file__).parent.parent / "fixtures" / "studio" / "tokens"
C = TokenCategory


def by_name(ts: TokenSet, name: str, theme: Theme | None = None) -> Token:
    hits = [t for t in ts.tokens if t.name == name and (theme is None or theme in t.themes)]
    assert len(hits) == 1, (name, hits)
    return hits[0]


# --- colours and lengths ------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "#3b82f6", "#3B82F6", "#38f", "rgb(59, 130, 246)", "rgb(59 130 246)", "rgba(59,130,246,1)",
    "rgb(23.1% 51% 96.5%)", "color(srgb 0.23137 0.5098 0.96471)",
])
def test_one_colour_written_many_ways_is_the_same_colour(text):
    c = parse_color(text)
    ref = parse_color("#3b82f6") if text != "#38f" else parse_color("#3388ff")
    assert c is not None and delta_e(c, ref) < EXACT_DE


@pytest.mark.parametrize("text", [
    "#fff", "white", "color(display-p3 1 1 1)", "color(rec2020 1 1 1)", "color(a98-rgb 1 1 1)",
    "color(prophoto-rgb 1 1 1)", "lab(100 0 0)", "lch(100 0 0)", "oklch(1 0 0)", "hsl(0 0% 100%)",
    "hwb(0 100% 0%)", "color(xyz-d65 0.95046 1 1.08906)", "color(srgb-linear 1 1 1)",
])
def test_white_is_white_in_every_space(text):
    c = parse_color(text)
    assert c is not None and abs(c.L - 1) < 1e-3 and abs(c.a) < 1e-3 and abs(c.b) < 1e-3


def test_alpha_is_kept_apart_and_unreadable_colours_are_none():
    assert parse_color("rgba(0, 0, 0, 0.1)").alpha == pytest.approx(0.1)
    assert parse_color("#0000001a").alpha == pytest.approx(0x1A / 255)
    assert parse_color("oklch(0.5 0.1 200 / 50%)").alpha == pytest.approx(0.5)
    for bad in ("currentcolor", "var(--x)", "color-mix(in srgb, red, blue)", "not a colour", ""):
        assert parse_color(bad) is None


def test_lengths_become_px_and_calc_is_evaluated():
    assert parse_length("0.75rem") == 12
    assert parse_length("1.5em", em_px=20) == 30
    assert parse_length("12pt") == 16
    assert parse_length("calc(2 * 0.25rem)") == 8
    assert parse_length("calc(1rem + 4px)") == 20
    assert parse_length("1.5") is None and parse_length("auto") is None and parse_length("50%") is None


def test_shadows_parse_into_layers():
    layers = parse_shadow("rgba(0, 0, 0, 0.3) 0px 4px 8px 0px, inset 0 0 0 1px #fff")
    assert [(lay.x, lay.y, lay.blur, lay.spread, lay.inset) for lay in layers] == [(0, 4, 8, 0, False), (0, 0, 0, 1, True)]
    assert parse_shadow("none") == []


# --- matching -----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ts() -> TokenSet:
    return load_tokens(FIX / "resolver.json")


def test_colour_thresholds_are_the_researched_ones(ts):
    exact = match("#3b82f7", "color", ts, theme="light")      # 8-bit rounding: exact
    assert exact.kind == "exact" and exact.distance < EXACT_DE
    near = match("#3f86f8", "color", ts, theme="light")
    assert near.kind == "near" and EXACT_DE < near.distance < NEAR_DE and near.token == "color.action.primary"
    off = match("#e11d48", "color", ts, theme="light")
    assert off.kind == "off" and off.distance > NEAR_DE and off.token == "color.danger"


def test_shared_values_list_every_candidate_semantic_first_and_by_use(ts):
    m = match("#111827", "color", ts, theme="light", prop="color")
    assert m.kind == "exact" and m.token == "color.text.primary"
    assert set(m.candidates) == {"color.text.primary", "color.gray.900"}
    # in dark the same value is the page background: for a background that is the one meant
    bg = match("#111827", "color", ts, theme="dark", prop="background-color")
    assert bg.token == "color.bg.page" and "color.gray.900" in bg.candidates
    # a primitive with no semantic sibling stays itself
    assert match("#1d4ed8", "color", ts, theme="light").token == "color.blue.700"


def test_alpha_is_compared_apart(ts):
    assert match("rgba(0, 0, 0, 0.1)", "color", ts).token == "color.shadow"
    half = match("rgba(59, 130, 246, 0.5)", "color", ts, theme="light")
    assert half.kind == "off" and half.token == "color.action.primary"   # same hue, other opacity


def test_themes_keep_to_their_own_values(ts):
    assert by_name(ts, "color.text.primary", Theme.LIGHT).value != by_name(ts, "color.text.primary", Theme.DARK).value
    assert by_name(ts, "color.action.primary").themes == [Theme.LIGHT, Theme.DARK]
    # #1f2937 is a primitive in both themes but the surface only in dark
    assert match("#1f2937", "color", ts, theme="dark", prop="background-color").token == "color.bg.surface"
    assert match("#1f2937", "color", ts, theme="light", prop="background-color").token == "color.gray.800"


def test_lengths_exact_near_and_off(ts):
    assert match("12px", "space", ts).token == "space.3"           # 0.75rem in the file
    assert match("12.4px", "space", ts).kind == "exact"
    near = match("13px", "space", ts)
    assert (near.kind, near.token, near.distance) == ("near", "space.3", 1.0)
    assert match("20px", "space", ts).kind == "off"
    assert match("-16px", "space", ts).token == "space.4"          # negative margins use the scale too
    assert match("18px", "font_size", ts).kind == "off"            # 2px from 20 is not 'almost'
    assert match("15px", "font_size", ts).kind == "near"


def test_radius_percent_counts_as_full_only_from_half(ts):
    assert match("50%", "radius", ts).token == "radius.full"
    assert match("20%", "radius", ts).kind == "off"


def test_line_height_weight_family_and_shadow(ts):
    assert match("1.5", "line_height", ts, font_size_px=16).token == "font.lineHeight.normal"
    assert match("24px", "line_height", ts, font_size_px=16).token == "font.lineHeight.normal"
    assert match("25px", "line_height", ts, font_size_px=20).token == "font.lineHeight.tight"
    assert match("1.8", "line_height", ts, font_size_px=16).kind == "off"
    assert match("700", "font_weight", ts).token == "font.weight.bold"
    assert match("500", "font_weight", ts).kind == "off"
    assert match('Arial, Helvetica, sans-serif', "font_family", ts).token == "font.family.sans"
    assert match('"Courier New", monospace', "font_family", ts).token == "font.family.mono"
    assert match("Helvetica, sans-serif", "font_family", ts).kind == "near"
    assert match("Georgia, serif", "font_family", ts).kind == "off"
    assert match("rgba(0, 0, 0, 0.1) 0px 1px 2px 0px", "shadow", ts).token == "shadow.sm"
    assert match("rgba(0, 0, 0, 0.3) 0px 4px 8px 0px", "shadow", ts).kind == "off"


def test_a_scale_accepts_any_multiple_of_its_step_and_its_half_steps():
    ts = TokenSet(scales=[Scale(category=C.SPACE, step_px=4)])
    assert match("12px", "space", ts).token == "space.scale.3"
    near = match("13px", "space", ts)
    assert (near.kind, near.token) == ("near", "space.scale.3")
    # Tailwind's p-0.5, gap-1.5, 2.5 and 3.5: half steps are on the scale
    assert (match("2px", "space", ts).kind, match("2px", "space", ts).token) == ("exact", "space.scale.0.5")
    assert match("6px", "space", ts).token == "space.scale.1.5"
    assert match("10px", "space", ts).kind == "exact"
    assert match("3px", "space", ts).kind != "exact" and match("5px", "space", ts).kind != "exact"
    assert match("1px", "space", ts).token == "space.scale.px" and scale_px("space.scale.px", ts) == 1.0
    assert scale_px("space.scale.1.5", ts) == 6.0


def test_a_circle_and_a_pill_are_shapes_on_the_system():
    ts = TokenSet(tokens=[Token(name="radius.md", category=C.RADIUS, value="6px")])
    for v in ("50%", "9999px", "3.40282e+38px"):
        m = match(v, "radius", ts)
        assert m.kind == "exact" and m.token.startswith("radius.shape."), (v, m)
    assert match("30%", "radius", ts).kind == "off"
    assert match("7px", "radius", ts).kind == "near"


def test_thresholds_can_be_tightened_by_rules(ts):
    assert match("#3b82f7", "color", ts, theme="light", rules={"color_exact": 0.0001}).kind == "near"
    assert match("13px", "space", ts, rules={"px_near": 0.2, "px_near_ratio": 0.01}).kind == "off"


# --- loading ------------------------------------------------------------------------------

def test_dtcg_aliases_types_and_structured_values(ts):
    blue = by_name(ts, "color.blue.500")
    assert blue.value.startswith("oklab(") and not blue.semantic and blue.css_var == "--color-blue-500"
    assert by_name(ts, "color.action.primary").semantic
    assert by_name(ts, "space.3").value == "12px"
    assert by_name(ts, "font.weight.bold").value == "700"               # "bold" by name
    assert by_name(ts, "font.lineHeight.normal").category == C.LINE_HEIGHT
    assert by_name(ts, "font.lineHeight.normal").css_var == "--font-line-height-normal"
    assert by_name(ts, "border.width.thin").category == C.BORDER_WIDTH
    assert by_name(ts, "font.family.mono").value == '"Courier New", monospace'
    assert by_name(ts, "shadow.sm").value.startswith("0px 1px 2px 0px oklab(0 0 0 / 0.1")   # alias inside


def test_the_simple_themes_map_and_the_themes_argument_give_the_same_set(ts):
    names = lambda s: sorted((t.name, t.value, tuple(t.themes)) for t in s.tokens)
    assert names(load_tokens(FIX / "themes-map.json")) == names(ts)
    assert names(from_dtcg(FIX / "core.json", themes={"light": FIX / "light.json", "dark": FIX / "dark.json"})) == names(ts)


def test_an_alias_that_does_not_resolve_is_a_clear_error():
    with pytest.raises(InvalidRequest, match="does not resolve"):
        from_dtcg({"a": {"$type": "color", "$value": "{b}"}})
    with pytest.raises(InvalidRequest, match="refers back to itself"):
        from_dtcg({"a": {"$type": "color", "$value": "{b}"}, "b": {"$type": "color", "$value": "{a}"}})


def test_a_json_pointer_reference_is_an_alias():
    ts = from_dtcg({"base": {"$type": "color", "$value": "#ff0000"},
                    "brand": {"$type": "color", "$value": {"$ref": "#/base/$value"}}})
    assert by_name(ts, "brand").semantic and by_name(ts, "brand").value == by_name(ts, "base").value


def test_typography_and_border_composites_split_into_their_parts():
    ts = from_dtcg({"heading": {"$type": "typography", "$value": {
        "fontFamily": ["Inter"], "fontSize": {"value": 2, "unit": "rem"}, "fontWeight": 600, "lineHeight": 1.2}},
        "divider": {"$type": "border", "$value": {"color": "#e5e7eb", "width": {"value": 1, "unit": "px"}, "style": "solid"}}})
    assert by_name(ts, "heading.fontSize").value == "32px"
    assert by_name(ts, "heading.lineHeight").value == "1.2"
    assert by_name(ts, "divider.width").category == C.BORDER_WIDTH


def test_a_css_custom_properties_file_with_a_dark_theme():
    ts = from_css((FIX / "ds.css").read_text())
    assert by_name(ts, "color.text.primary", Theme.LIGHT).semantic
    assert delta_e(parse_color(by_name(ts, "color.text.primary", Theme.DARK).value), parse_color("#f9fafb")) < 1e-4
    assert by_name(ts, "space.3").value == "12px"
    assert by_name(ts, "font.line.height.normal").category == C.LINE_HEIGHT
    # the same values as the DTCG file, so the two describe one system
    dtcg = load_tokens(FIX / "resolver.json")
    for t in dtcg.tokens:
        if t.category == C.COLOR and t.themes == [Theme.LIGHT, Theme.DARK]:
            same = [x for x in ts.tokens if x.css_var == t.css_var]
            assert same and delta_e(parse_color(same[0].value), parse_color(t.value)) < EXACT_DE, t.name


def test_component_scoped_variables_are_not_tokens():
    ts = from_css(":root { --space-2: 8px } .card { --card-pad: 13px; } @media (prefers-color-scheme: dark) "
                  "{ :root { --color-bg: #000 } } :root { --color-bg: #fff }")
    assert {t.css_var for t in ts.tokens} == {"--space-2", "--color-bg"}
    assert by_name(ts, "color.bg", Theme.DARK).value == "oklab(0 0 0)"


TAILWIND = """
@import "tailwindcss";
@theme {
  --color-blue-500: oklch(62.3% 0.214 259.815);
  --color-white: #fff;
  --spacing: 0.25rem;
  --radius-md: 0.375rem;
  --text-sm: 0.875rem;
  --text-sm--line-height: calc(1.25 / 0.875);
  --font-sans: ui-sans-serif, system-ui, sans-serif;
  --font-weight-bold: 700;
  --leading-tight: 1.25;
  --shadow-sm: 0 1px 3px 0 rgb(0 0 0 / 0.1), 0 1px 2px -1px rgb(0 0 0 / 0.1);
  --breakpoint-md: 48rem;
  --ease-out: cubic-bezier(0, 0, 0.2, 1);
}
"""


def test_a_tailwind_v4_theme():
    ts = from_tailwind(TAILWIND)
    assert {s.category for s in ts.scales} == {C.SPACE, C.BORDER_WIDTH}
    assert next(s for s in ts.scales if s.category == C.SPACE).step_px == 4
    assert by_name(ts, "text.sm").category == C.FONT_SIZE and by_name(ts, "text.sm").value == "14px"
    assert by_name(ts, "text.sm.line.height").value == "1.4286"
    assert by_name(ts, "font.sans").category == C.FONT_FAMILY
    assert by_name(ts, "font.weight.bold").category == C.FONT_WEIGHT
    assert by_name(ts, "shadow.sm").category == C.SHADOW
    assert not [t for t in ts.tokens if "breakpoint" in t.name or "ease" in t.name]
    assert match("16px", "space", ts).token == "space.scale.4"
    assert match("3px", "border_width", ts).kind == "exact"
    assert load_tokens(TAILWIND).scales == ts.scales          # found without being told


def test_load_tokens_refuses_a_missing_file():
    with pytest.raises(InvalidRequest, match="no token file"):
        load_tokens("/nowhere/tokens.json")


# --- export -------------------------------------------------------------------------------

def test_dtcg_export_reads_back_to_the_same_set(ts):
    doc = to_dtcg(ts)
    assert set(doc) == {"base", "light", "dark"}
    assert doc["base"]["color"]["blue"]["500"]["$value"]["hex"] == "#3b82f6"
    back = from_dtcg(json.loads(json.dumps(doc)))
    key = lambda s: sorted((t.name, t.category, tuple(t.themes)) for t in s.tokens)
    assert key(back) == key(ts)
    for t in ts.tokens:
        b = next(x for x in back.tokens if x.name == t.name and x.themes == t.themes)
        if t.category == C.COLOR:
            assert delta_e(parse_color(b.value), parse_color(t.value)) < 1e-4
        else:
            assert b.value == t.value, t.name


def test_scales_survive_the_round_trip():
    ts = from_tailwind(TAILWIND)
    assert from_dtcg(to_dtcg(ts)).scales == ts.scales


def test_css_export_reads_back(ts):
    css = to_css(ts)
    assert ":root {" in css and '[data-theme="dark"] {' in css and "--color-blue-500: #3b82f6;" in css
    back = from_css(css)
    for t in ts.tokens:
        b = [x for x in back.tokens if x.css_var == t.css_var and set(t.themes) <= set(x.themes)]
        assert b, t.name
        if t.category == C.COLOR:
            assert delta_e(parse_color(b[0].value), parse_color(t.value)) < EXACT_DE
