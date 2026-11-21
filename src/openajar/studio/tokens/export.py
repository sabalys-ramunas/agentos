"""A TokenSet back out: as Design Tokens JSON (DTCG 2025.10) and as CSS custom properties.

Themed tokens are written as a simple themes map (`base`, `light`, `dark`), which `from_dtcg`
reads back; scales go in `$extensions.openajar.scales`.
"""

from __future__ import annotations

from typing import Any

from ..contracts import Theme, Token, TokenCategory, TokenSet
from .color import delta_e, parse_color
from .load import kebab
from .units import parse_length, parse_number, parse_shadow, parse_weight

C = TokenCategory
_TYPE = {
    C.COLOR: "color", C.SPACE: "dimension", C.RADIUS: "dimension", C.FONT_SIZE: "dimension",
    C.BORDER_WIDTH: "dimension", C.FONT_FAMILY: "fontFamily", C.FONT_WEIGHT: "fontWeight",
    C.LINE_HEIGHT: "number", C.SHADOW: "shadow",
}


def _dim(px: float) -> dict:
    return {"value": round(px, 4), "unit": "px"}


def _color_json(value: str) -> dict | str:
    c = parse_color(value)
    if c is None:
        return value
    out: dict[str, Any] = {"colorSpace": "oklab", "components": [round(c.L, 5), round(c.a, 5), round(c.b, 5)]}
    if c.alpha < 0.9995:
        out["alpha"] = round(c.alpha, 4)
    h = parse_color(c.hex())
    if h is not None and delta_e(c, h) < 0.002:
        out["hex"] = c.hex()[:7]
    return out


def _value_json(t: Token) -> tuple[str, Any]:
    typ = _TYPE[t.category]
    if t.category == C.COLOR:
        return typ, _color_json(t.value)
    if t.category in (C.SPACE, C.RADIUS, C.FONT_SIZE, C.BORDER_WIDTH):
        return typ, _dim(parse_length(t.value) or 0.0)
    if t.category == C.LINE_HEIGHT:
        n = parse_number(t.value)
        return (typ, n) if n is not None else ("dimension", _dim(parse_length(t.value) or 0.0))
    if t.category == C.FONT_WEIGHT:
        return typ, parse_weight(t.value)
    if t.category == C.FONT_FAMILY:
        return typ, [f.strip().strip('"') for f in t.value.split(",")]
    layers = parse_shadow(t.value) or []
    items = [{"color": _color_json(layer.color.css()) if layer.color else "#000000",
              "offsetX": _dim(layer.x), "offsetY": _dim(layer.y), "blur": _dim(layer.blur),
              "spread": _dim(layer.spread), **({"inset": True} if layer.inset else {})} for layer in layers]
    return typ, items[0] if len(items) == 1 else items


def _tree(tokens: list[Token]) -> dict:
    root: dict = {}
    for t in tokens:
        node = root
        parts = t.name.split(".")
        for p in parts[:-1]:
            nxt = node.setdefault(p, {})
            if "$value" in nxt:          # a token and a group share a name: keep the token
                break
            node = nxt
        else:
            typ, val = _value_json(t)
            entry: dict[str, Any] = {"$type": typ, "$value": val}
            default_var = "--" + "-".join(kebab(p) for p in parts)
            if t.css_var and t.css_var != default_var:
                entry["$extensions"] = {"openajar": {"cssVar": t.css_var}}
            node[parts[-1]] = entry
    return root


def to_dtcg(tokens: TokenSet) -> dict:
    """DTCG JSON. Without themed tokens, one token tree; with them, `{base, light, dark}`."""
    both = [t for t in tokens.tokens if set(t.themes) >= {Theme.LIGHT, Theme.DARK}]
    light = [t for t in tokens.tokens if t.themes == [Theme.LIGHT]]
    dark = [t for t in tokens.tokens if t.themes == [Theme.DARK]]
    if not light and not dark:
        out = _tree(both)
    else:
        out = {"base": _tree(both), "light": _tree(light), "dark": _tree(dark)}
    if tokens.scales:
        out.setdefault("$extensions", {})["openajar"] = {
            "scales": [s.model_dump(mode="json") for s in tokens.scales]}
    return out


def _css_value(t: Token) -> str:
    if t.category == C.COLOR:
        c = parse_color(t.value)
        if c is not None:
            h = parse_color(c.hex())
            if h is not None and delta_e(c, h) < 0.002 and abs(h.alpha - c.alpha) < 0.003:
                return c.hex()
        return t.value
    if t.category == C.SHADOW:
        layers = parse_shadow(t.value) or []
        out = []
        for layer in layers:
            col = ""
            if layer.color is not None:
                h = parse_color(layer.color.hex())
                col = " " + (layer.color.hex() if h and delta_e(layer.color, h) < 0.002 else layer.color.css())
            out.append(("inset " if layer.inset else "") + layer.css().removeprefix("inset ").rsplit(" oklab", 1)[0] + col)
        return ", ".join(out) or "none"
    return t.value


def to_css(tokens: TokenSet, *, dark_selector: str = '[data-theme="dark"]') -> str:
    """CSS custom properties: `:root` for tokens valid in light (or in every theme), the dark
    selector for the dark values that differ."""
    def var(t: Token) -> str:
        return t.css_var or "--" + "-".join(kebab(p) for p in t.name.split("."))

    root = [t for t in tokens.tokens if Theme.LIGHT in t.themes]
    dark = [t for t in tokens.tokens if t.themes == [Theme.DARK]]
    lines = [":root {"] + [f"  {var(t)}: {_css_value(t)};" for t in root] + ["}"]
    if dark:
        lines += ["", f"{dark_selector} {{"] + [f"  {var(t)}: {_css_value(t)};" for t in dark] + ["}"]
    return "\n".join(lines) + "\n"
