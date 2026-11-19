"""Studio reads a size scale named by number (font.13, --font-13) as font sizes, and counts a
border width that equals a token as on the system even when a utility wrote it as a literal.

Both were misread on agentos's own web app: its font sizes are `font.11` to `font.40` and its
borders come from Tailwind's `border` (1px, no custom property behind it)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.studio.contracts import TokenCategory, TokenSet
from agentos.studio.tokens import from_css, from_dtcg, from_tailwind, match
from agentos.studio.tokens.check import Analysis, Value, adherence_from
from agentos.studio.tokens.probe import Prov

C = TokenCategory
REPO = Path(__file__).resolve().parents[2]
SIZES = [11, 12, 13, 14, 16, 20, 24, 28, 32, 40]


def _px(ts: TokenSet, cat: TokenCategory) -> list[float]:
    return sorted({float(t.value.removesuffix("px")) for t in ts.tokens if t.category == cat})


def _dim(n: float) -> dict:
    return {"$type": "dimension", "$value": {"value": n, "unit": "px"}}


def _dtcg() -> dict:
    """The shape web/tokens/tokens.json has: a size scale by number, type roles aliasing it,
    and border widths under their own group."""
    return {
        "font": {"13": _dim(13), "20": _dim(20), "32": _dim(32)},
        "line-height": {"20": _dim(20)},
        "font-family": {"sans": {"$type": "fontFamily", "$value": ["Geist", "sans-serif"]}},
        "type": {"body": {"$type": "typography", "$value": {
            "fontFamily": "{font-family.sans}", "fontSize": "{font.13}", "lineHeight": "{line-height.20}"}}},
        "border-width": {"hairline": _dim(1), "focus": _dim(2)},
    }


def test_a_dtcg_size_scale_named_by_number_is_font_sizes():
    ts = from_dtcg(_dtcg())
    assert _px(ts, C.FONT_SIZE) == [13, 20, 32]
    names = {t.name for t in ts.tokens if t.category == C.FONT_SIZE}
    assert {"font.13", "font.20", "font.32", "type.body.fontSize"} <= names
    assert next(t for t in ts.tokens if t.name == "font.20").css_var == "--font-20"
    assert _px(ts, C.BORDER_WIDTH) == [1, 2]
    assert _px(ts, C.LINE_HEIGHT) == [20]


def test_a_css_size_scale_named_by_number_is_font_sizes():
    css = ":root { --font-13: 13px; --font-20: 1.25rem; --font-13-line: 20px; --font-family-sans: Geist; }"
    ts = from_css(css)
    assert _px(ts, C.FONT_SIZE) == [13, 20]
    assert _px(ts, C.LINE_HEIGHT) == [20]
    assert [t.name for t in ts.tokens if t.category == C.FONT_FAMILY] == ["font.family.sans"]


def test_a_length_under_tailwinds_font_namespace_is_a_size_not_a_family():
    css = "@theme { --spacing: 4px; --font-sans: Geist, sans-serif; --text-base: 16px; --radius-md: 6px; }\n" \
          ":root { --font-13: 13px; }"
    ts = from_tailwind(css)
    fams = {t.name: t.value for t in ts.tokens if t.category == C.FONT_FAMILY}
    assert fams == {"font.sans": "Geist, sans-serif"}
    assert 13 in _px(ts, C.FONT_SIZE)


def test_agentos_own_tokens_file_gives_every_font_size_and_border_width():
    path = REPO / "web" / "tokens" / "tokens.json"
    if not path.exists():
        pytest.skip("web/tokens/tokens.json is not in this checkout")
    ts = from_dtcg(path)
    assert _px(ts, C.FONT_SIZE) == SIZES
    assert {f"font.{n}" for n in SIZES} <= {t.name for t in ts.tokens}
    assert _px(ts, C.BORDER_WIDTH) == [1, 1.5, 2]


def _value(ts: TokenSet, cat: str, v: str, prov: Prov) -> Value:
    val = Value(rec={"i": 0, "sel": "div", "tag": "div"}, props=["x"], category=cat, value=v, ua=False, fs=14.0)
    val.match = match(v, cat, ts)
    val.prov, val.method = prov, "probe"
    return val


def test_a_literal_border_width_equal_to_a_token_is_on_the_system():
    ts = from_dtcg(_dtcg())
    still = Prov(kind="fixed")
    assert _value(ts, "border_width", "1px", still).on_system          # Tailwind's border
    assert _value(ts, "border_width", "2px", still).on_system          # Tailwind's border-2
    assert not _value(ts, "border_width", "3px", still).on_system      # no token is 3px
    # a font size has a token to follow, so a still one is still a look-alike
    assert not _value(ts, "font_size", "13px", still).on_system
    assert _value(ts, "font_size", "20px", Prov(kind="token", token="font.20")).on_system


def test_adherence_counts_the_whole_scale_and_tailwind_borders():
    ts = from_dtcg(_dtcg())
    vals = [_value(ts, "font_size", f"{n}px", Prov(kind="token", token=f"font.{n}")) for n in (13, 20, 32)]
    vals += [_value(ts, "border_width", "1px", Prov(kind="fixed")),
             _value(ts, "border_width", "1px", Prov(kind="token", token="border-width.hairline"))]
    a = Analysis(records=[], values=vals, tokens=ts, theme=None, components=[])
    assert adherence_from(a).by_category == {"border_width": 1.0, "font_size": 1.0}
