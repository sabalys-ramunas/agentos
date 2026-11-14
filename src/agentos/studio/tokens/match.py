"""`match(value, category, tokens)`: is a value on the design system, and which token is it?

Colours compare in OKLab with delta E OK (exact below 0.005, near below 0.02, alpha apart).
Lengths compare in px (exact within 0.5px, near within 1px or 10% of the token). Scales
(Tailwind's spacing) accept any multiple of the step. When several tokens share the value,
all are listed and the semantic one, then the one named for the property's use, comes first.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from ..contracts import Match, Scale, Theme, Token, TokenCategory, TokenSet
from .color import EXACT_DE, NEAR_DE, OKLab, delta_e, parse_color
from .units import EXACT_PX, ROOT_PX, font_stack, parse_length, parse_number, parse_shadow, parse_weight

C = TokenCategory
LENGTHS = (C.SPACE, C.RADIUS, C.FONT_SIZE, C.BORDER_WIDTH)

# The property a colour is used for, and the words a token named for that use carries.
_CONTEXT_WORDS = {
    "text": ("text", "fg", "foreground", "content", "ink", "link", "heading"),
    "background": ("bg", "background", "surface", "fill", "canvas", "layer", "container"),
    "border": ("border", "stroke", "outline", "divider", "line", "separator"),
}


def context_of(prop: str | None) -> str | None:
    if not prop:
        return None
    if prop == "color":
        return "text"
    if prop.startswith("background"):
        return "background"
    if prop.startswith(("border", "outline")):
        return "border"
    return None


@lru_cache(maxsize=4096)
def _color(value: str) -> OKLab | None:
    return parse_color(value)


def _pref_key(tok: Token, ctx: str | None):
    parts = set(re.split(r"[.\-_/]", tok.name.lower()))
    ctx_hit = bool(ctx) and any(w in parts for w in _CONTEXT_WORDS.get(ctx, ()))
    return (not ctx_hit, not tok.semantic, tok.name.count("."), len(tok.name), tok.name)


def _themed(tokens: TokenSet, category: TokenCategory, theme: Theme | None) -> list[Token]:
    return [t for t in tokens.tokens if t.category == category and (theme is None or theme in t.themes)]


def _rules(rules: dict[str, Any] | None) -> dict[str, float]:
    r = {"color_exact": EXACT_DE, "color_near": NEAR_DE, "alpha_exact": 0.01, "alpha_near": 0.05,
         "px_exact": EXACT_PX, "px_near": 1.0, "px_near_ratio": 0.10, "weight_near": 50.0}
    for k, v in (rules or {}).items():
        if k in r and isinstance(v, (int, float)):
            r[k] = float(v)
    return r


def _result(scored: list[tuple[float, Token]], exact_cut: float, near_cut, ctx: str | None,
            same_value, extra_exact: list[str] | None = None) -> Match:
    """`scored` is (distance, token). `near_cut(distance, token)` says whether it is near."""
    if not scored and not extra_exact:
        return Match(kind="off")
    if extra_exact and (not scored or min(d for d, _ in scored) > exact_cut):
        return Match(kind="exact", token=extra_exact[0], candidates=extra_exact, distance=0.0)
    best_d = min(d for d, _ in scored)
    if best_d <= exact_cut:
        cands = sorted((t for d, t in scored if d <= exact_cut), key=lambda t: _pref_key(t, ctx))
        names = [t.name for t in cands] + [n for n in (extra_exact or []) if n not in {t.name for t in cands}]
        return Match(kind="exact", token=names[0], candidates=names, distance=round(best_d, 4))
    nearest = [t for d, t in scored if d - best_d <= 1e-9]
    nearest.sort(key=lambda t: _pref_key(t, ctx))
    top = nearest[0]
    sharing = sorted((t for d, t in scored if same_value(t, top)), key=lambda t: _pref_key(t, ctx))
    names = [t.name for t in sharing] or [top.name]
    kind = "near" if near_cut(best_d, top) else "off"
    return Match(kind=kind, token=names[0], candidates=names, distance=round(best_d, 4))


def match(value: Any, category: TokenCategory | str, tokens: TokenSet, *, theme: Theme | str | None = None,
          prop: str | None = None, font_size_px: float | None = None, root_px: float = ROOT_PX,
          rules: dict[str, Any] | None = None) -> Match:
    """Map one value to the token set. `value` is CSS text as a computed style or a token file
    writes it. `theme` keeps to tokens valid in that theme. `prop` (the CSS property) decides
    which tokens are preferred when several share the value. `font_size_px` is needed for line
    heights and em lengths."""
    category = TokenCategory(category)
    theme = Theme(theme) if theme else None
    r = _rules(rules)
    toks = _themed(tokens, category, theme)
    ctx = context_of(prop)

    if category == C.COLOR:
        c = parse_color(str(value))
        if c is None:
            return Match(kind="off")
        # alpha is handled apart: the nearest token is the nearest in colour (opacity only
        # breaks ties); a token is exact or near only when the opacity agrees too
        scored = []
        for t in toks:
            tc = _color(t.value)
            if tc is not None:
                scored.append((delta_e(c, tc), abs(c.alpha - tc.alpha), t))
        if not scored:
            return Match(kind="off")
        exact = [(d, t) for d, da, t in scored if d <= r["color_exact"] and da <= r["alpha_exact"]]
        if exact:
            cands = sorted((t for _, t in exact), key=lambda t: _pref_key(t, ctx))
            return Match(kind="exact", token=cands[0].name, candidates=[t.name for t in cands],
                         distance=round(min(d for d, _ in exact), 4))
        best = min(d + da for d, da, _ in scored)
        nearest = sorted((x for x in scored if x[0] + x[1] - best <= 1e-9), key=lambda x: _pref_key(x[2], ctx))
        d, da, top = nearest[0]
        tc = _color(top.value)
        sharing = sorted((t for _, _, t in scored if (o := _color(t.value)) and delta_e(o, tc) <= r["color_exact"]
                          and abs(o.alpha - tc.alpha) <= r["alpha_exact"]), key=lambda t: _pref_key(t, ctx))
        kind = "near" if d <= r["color_near"] and da <= r["alpha_near"] else "off"
        return Match(kind=kind, token=sharing[0].name if sharing else top.name,
                     candidates=[t.name for t in sharing] or [top.name], distance=round(d, 4))

    if category in LENGTHS or category == C.LINE_HEIGHT:
        text = str(value).strip()
        if category == C.RADIUS:
            shape = radius_shape(text, root_px=root_px)
            if shape is not None:
                # a circle (50%) or a pill (9999px, Tailwind's rounded-full) is a shape, not a size
                pills = [t for t in toks if (parse_length(t.value) or 0) >= 999]
                if pills:
                    return Match(kind="exact", token=pills[0].name, candidates=[t.name for t in pills], distance=0.0)
                return Match(kind="exact", token=SHAPES[shape], candidates=[SHAPES[shape]], distance=0.0)
            if text.endswith("%"):
                return Match(kind="off")
        fs = font_size_px if font_size_px is not None else root_px
        if category == C.LINE_HEIGHT:
            num = parse_number(text)
            px = num * fs if num is not None else parse_length(text, root_px=root_px, em_px=fs)
        else:
            px = parse_length(text, root_px=root_px, em_px=fs)
        if px is None:
            return Match(kind="off")
        apx = abs(px)

        def token_px(t: Token) -> float | None:
            if category == C.LINE_HEIGHT:
                n = parse_number(t.value)
                if n is not None:
                    return n * fs
            return parse_length(t.value, root_px=root_px)

        scored = [(abs(apx - tp), t) for t in toks if (tp := token_px(t)) is not None]
        extra: list[str] = []
        for s in tokens.scales:
            if s.category != category or s.step_px <= 0:
                continue
            # half steps are on the scale too (Tailwind's p-0.5, gap-1.5, 2.5 and 3.5), and so is a
            # 1px hairline on a spacing scale (Tailwind's p-px)
            n = round(apx / s.step_px * 2) / 2
            if n >= 0.5 and n * s.step_px <= s.max_px and abs(n * s.step_px - apx) <= r["px_exact"]:
                extra.append(scale_name(s, n))
            elif s.category == C.SPACE and s.step_px > 1 and abs(apx - 1) <= 0.01:
                extra.append(f"{s.category.value}.scale.px")

        def near(d: float, t: Token) -> bool:
            tp = token_px(t) or 0.0
            return d <= r["px_near"] or d < r["px_near_ratio"] * tp

        def same(a: Token, b: Token):
            pa, pb = token_px(a), token_px(b)
            return pa is not None and pb is not None and abs(pa - pb) <= r["px_exact"]

        m = _result(scored, r["px_exact"], near, None, same, extra)
        if m.kind != "exact" and tokens.scales:
            for s in tokens.scales:
                if s.category == category and s.step_px > 0:
                    n = max(1, round(apx / s.step_px))
                    d = abs(n * s.step_px - apx)
                    if m.distance is None or d < m.distance:
                        kind = "near" if d <= r["px_near"] or d < r["px_near_ratio"] * n * s.step_px else "off"
                        m = Match(kind=kind, token=scale_name(s, n), candidates=[scale_name(s, n)], distance=round(d, 4))
        return m

    if category == C.FONT_WEIGHT:
        w = parse_weight(value)
        if w is None:
            return Match(kind="off")
        scored = [(abs(w - tw), t) for t in toks if (tw := parse_weight(t.value)) is not None]
        return _result(scored, 0.5, lambda d, t: d <= r["weight_near"], None,
                       lambda a, b: parse_weight(a.value) == parse_weight(b.value))

    if category == C.FONT_FAMILY:
        stack = font_stack(str(value))
        if not stack:
            return Match(kind="off")
        scored = []
        for t in toks:
            ts = font_stack(t.value)
            if not ts:
                continue
            if ts[0] == stack[0]:
                d = 0.0
            elif stack[0] in ts or ts[0] in stack:
                d = 1.0
            else:
                d = 2.0
            scored.append((d, t))
        return _result(scored, 0.0, lambda d, t: d <= 1.0, None,
                       lambda a, b: font_stack(a.value)[:1] == font_stack(b.value)[:1])

    if category == C.SHADOW:
        layers = parse_shadow(str(value), root_px=root_px)
        if not layers:
            return Match(kind="off")
        scored = []
        for t in toks:
            tl = parse_shadow(t.value, root_px=root_px)
            if not tl or len(tl) != len(layers):
                continue
            d = 0.0
            for a, b in zip(layers, tl, strict=True):
                if a.inset != b.inset:
                    d = 999.0
                    break
                d = max(d, abs(a.x - b.x), abs(a.y - b.y), abs(a.blur - b.blur), abs(a.spread - b.spread))
                if a.color and b.color:
                    de = delta_e(a.color, b.color) + abs(a.color.alpha - b.color.alpha)
                    if de > r["color_near"]:
                        d = max(d, 2.0 + de)
                    elif de > r["color_exact"]:
                        d = max(d, r["px_exact"] + 0.01)
            scored.append((d, t))
        return _result(scored, r["px_exact"], lambda d, t: d <= r["px_near"], None, lambda a, b: a.value == b.value)

    return Match(kind="off")


def scale_name(s: Scale, n: float) -> str:
    return f"{s.category.value}.scale.{n:g}"


def scale_px(name: str, tokens: TokenSet) -> float | None:
    m = re.fullmatch(r"([a-z_]+)\.scale\.(\d+(?:\.5)?|px)", name or "")
    if not m:
        return None
    for s in tokens.scales:
        if s.category.value == m.group(1):
            return 1.0 if m.group(2) == "px" else s.step_px * float(m.group(2))
    return None


# Radius values that draw a shape rather than a size, and the name a match gives them.
SHAPES = {"circle": "radius.shape.circle", "pill": "radius.shape.pill"}
PILL_PX = 999.0


def radius_shape(text: str, *, root_px: float = ROOT_PX) -> str | None:
    """"circle" for 50% or more, "pill" for 999px or more (9999px, calc(infinity * 1px) as Chrome
    computes it), else None."""
    text = str(text).strip()
    if text.endswith("%"):
        pct = parse_number(text[:-1])
        return "circle" if pct is not None and pct >= 50 else None
    big = re.fullmatch(r"(\d+(?:\.\d+)?)e\+?(\d+)px", text, re.IGNORECASE)   # calc(infinity * 1px)
    px = float(big.group(1)) * 10 ** int(big.group(2)) if big else parse_length(text, root_px=root_px)
    return "pill" if px is not None and px >= PILL_PX else None
