"""Plain sentences from measured data, one per cause. No model writes them: every number is read
from the two manifests, every token name comes from `openajar.studio.tokens.match`. Page text is
quoted, capped and in single quotes (AX rule 12)."""

from __future__ import annotations

import re

from openajar.studio.contracts import Element, Rect, Theme, TokenCategory, TokenSet
from openajar.studio.diff.structure import TOL, Item, moved_effects
from openajar.studio.tokens import match

QUOTE_CAP = 40

ROLE_WORDS = {
    "img": "Image", "heading": "Heading", "button": "Button", "link": "Link", "textbox": "Text field",
    "searchbox": "Search field", "combobox": "Select", "listbox": "List box", "checkbox": "Checkbox",
    "radio": "Radio button", "slider": "Slider", "spinbutton": "Number field", "banner": "Header",
    "navigation": "Navigation", "main": "Main", "contentinfo": "Footer", "complementary": "Aside",
    "region": "Section", "form": "Form", "search": "Search", "dialog": "Dialog", "alertdialog": "Dialog",
    "article": "Article", "list": "List", "listitem": "List item", "table": "Table", "row": "Row",
    "cell": "Cell", "columnheader": "Column header", "paragraph": "Paragraph", "tab": "Tab",
    "tablist": "Tab list", "menu": "Menu", "menuitem": "Menu item", "option": "Option", "switch": "Switch",
    "status": "Status", "alert": "Alert", "toolbar": "Toolbar",
}
TAG_WORDS = {
    "div": "Box", "span": "Text", "p": "Paragraph", "section": "Section", "header": "Header",
    "footer": "Footer", "img": "Image", "svg": "Icon", "canvas": "Canvas", "video": "Video", "li": "List item",
    "strong": "Bold text", "em": "Italic text", "code": "Code", "label": "Label", "figure": "Figure",
    "iframe": "Frame",
}
PROP_CATEGORY = {
    "background-color": TokenCategory.COLOR, "color": TokenCategory.COLOR,
    "padding": TokenCategory.SPACE, "margin": TokenCategory.SPACE, "gap": TokenCategory.SPACE,
    "border-radius": TokenCategory.RADIUS, "border-width": TokenCategory.BORDER_WIDTH,
    "font-size": TokenCategory.FONT_SIZE, "font-family": TokenCategory.FONT_FAMILY,
    "font-weight": TokenCategory.FONT_WEIGHT, "line-height": TokenCategory.LINE_HEIGHT,
    "box-shadow": TokenCategory.SHADOW,
}
_RGB = re.compile(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)")


def quote(s: str | None, cap: int = QUOTE_CAP) -> str:
    s = " ".join((s or "").split()).replace("'", "’")
    return "'" + (s if len(s) <= cap else s[:cap - 1] + "…") + "'"


def px(v: float) -> str:
    r = round(v, 1)
    return f"{int(r)}" if r == int(r) else f"{r}"


def label(e: Element) -> str:
    """'Button 'Save' (Button/primary)', 'Heading 'Welcome'', 'Box at 16,300'."""
    word = ROLE_WORDS.get(e.role or "") or TAG_WORDS.get(e.tag) or (e.role or e.tag).capitalize()
    text = e.name or e.text
    out = f"{word} {quote(text)}" if text else word
    if e.component is not None:
        out += f" ({e.component.name[:60]})"
    elif not text and e.key.startswith("testid="):
        out += f" ({e.key.split(' ')[0][:60]})"
    elif not text:
        out += f" at {px(e.rect.x)},{px(e.rect.y)}"
    return out


def box(r: Rect) -> str:
    return f"{px(r.w)}x{px(r.h)} at {px(r.x)},{px(r.y)}"


def hex_color(v: str) -> str:
    """rgb(37, 99, 235) as #2563eb, with alpha when it is not 1. Anything else as it is."""
    m = _RGB.fullmatch(v.strip())
    if not m:
        return v
    r, g, b = (max(0, min(255, round(float(x)))) for x in m.groups()[:3])
    out = f"#{r:02x}{g:02x}{b:02x}"
    a = m.group(4)
    if a is not None:
        av = float(a[:-1]) / 100 if a.endswith("%") else float(a)
        if av < 1:
            out += f"{max(0, min(255, round(av * 255))):02x}"
    return out


def show(prop: str, v: str) -> str:
    if prop in ("color", "background-color"):
        return hex_color(v)
    if prop == "box-shadow":
        return _RGB.sub(lambda m: hex_color(m.group(0)), v)
    if prop == "font-family":
        return v.split(",")[0].strip().strip("'\"")
    return v


def token_names(prop: str, value: str, tokens: TokenSet, *, theme: Theme, font_size_px: float | None) -> str | None:
    """The token a value is exactly on (a list for shorthands like padding), or None."""
    cat = PROP_CATEGORY.get(prop)
    if cat is None:
        return None
    parts = value.split() if cat in (TokenCategory.SPACE, TokenCategory.RADIUS, TokenCategory.BORDER_WIDTH) else [value]
    names = []
    for part in parts:
        m = match(part, cat, tokens, theme=theme, prop=prop, font_size_px=font_size_px)
        if m.kind != "exact" or not m.token:
            return None
        names.append(m.token)
    return " ".join(names)


def font_px(e: Element | None) -> float | None:
    if e is None:
        return None
    v = e.styles.get("font-size", "")
    try:
        return float(v[:-2]) if v.endswith("px") else None
    except ValueError:
        return None


def _vpad(e: Element) -> float:
    parts = e.styles.get("padding", "0px").split()
    try:
        vals = [float(p[:-2]) for p in parts if p.endswith("px")]
    except ValueError:
        return 0.0
    if len(vals) != len(parts) or not vals:
        return 0.0
    top = vals[0]
    bottom = vals[2] if len(vals) >= 3 else vals[0]
    return top + bottom


def lines_of(e: Element) -> int | None:
    lh = e.styles.get("line-height", "")
    if not (e.text and lh.endswith("px")):
        return None
    try:
        h = float(lh[:-2])
    except ValueError:
        return None
    return max(1, round((e.rect.h - _vpad(e)) / h)) if h > 0 else None


def size_phrase(it: Item) -> str:
    """' and wrapped from 1 to 2 lines (+40 px)', ', 16 px taller', or ''."""
    if it.b is None or it.a is None or not it.resized:
        return ""
    lb, la = lines_of(it.b), lines_of(it.a)
    if lb is not None and la is not None and lb != la and abs(it.dh) > TOL:
        dh = f"{'+' if it.dh > 0 else '-'}{px(abs(it.dh))} px"
        if la > lb:
            return f" and wrapped from {lb} to {la} lines ({dh})"
        return f" and now takes {la} line{'s' if la != 1 else ''} instead of {lb} ({dh})"
    parts = []
    if abs(it.dw) > TOL:
        parts.append(f"{px(abs(it.dw))} px {'wider' if it.dw > 0 else 'narrower'}")
    if abs(it.dh) > TOL:
        parts.append(f"{px(abs(it.dh))} px {'taller' if it.dh > 0 else 'shorter'}")
    return ", " + " and ".join(parts)


def _axis(values: list[float], plus: str, minus: str) -> str | None:
    moving = [v for v in values if abs(v) > TOL]
    if not moving:
        return None
    pos = [v for v in moving if v > 0]
    neg = [-v for v in moving if v < 0]
    out = []
    for word, vs in ((plus, pos), (minus, neg)):
        if not vs:
            continue
        lo, hi = min(vs), max(vs)
        out.append(f"{word} by {px(lo)} px" if hi - lo <= 1 else f"{word} by {px(lo)} to {px(hi)} px")
    return " and ".join(out)


def direction(effects: list[Item]) -> str:
    parts = [p for p in (_axis([e.dy for e in effects], "down", "up"),
                         _axis([e.dx for e in effects], "right", "left")) if p]
    return " and ".join(parts)


def push_phrase(it: Item) -> str:
    moved = moved_effects(it)
    if not moved:
        return ""
    n = len(moved)
    return f", which moved {n} element{'s' if n != 1 else ''} {direction(moved)}"


def style_pieces(it: Item, tokens_by_prop: dict[str, list[str | None]], with_tokens: bool) -> list[str]:
    out = []
    for prop, (vb, va) in it.styles.items():
        sb, sa = show(prop, vb), show(prop, va)
        tb, ta = (tokens_by_prop.get(prop) or [None, None])
        if with_tokens and (tb or ta):
            piece = f"{prop} {tb or sb} -> {ta or sa} ({sb} -> {sa}"
            if not tb or not ta:
                piece += f", the {'old' if not tb else 'new'} value is on no token"
            out.append(piece + ")")
        elif with_tokens and prop in PROP_CATEGORY:
            out.append(f"{prop} {sb} -> {sa} (neither is on a token)")
        else:
            out.append(f"{prop} {sb} -> {sa}")
    return out


def cause_line(it: Item, tokens_by_prop: dict[str, list[str | None]], with_tokens: bool) -> str:
    if it.added:
        inside = f" with {it.inside} element{'s' if it.inside != 1 else ''} inside" if it.inside else ""
        return f"Added {label(it.a)} ({box(it.a.rect)}){inside}{push_phrase(it)}."
    if it.removed:
        inside = f" with {it.inside} element{'s' if it.inside != 1 else ''} inside" if it.inside else ""
        return f"Removed {label(it.b)} (was {box(it.b.rect)}){inside}{push_phrase(it)}."
    what = []
    if it.text:
        new = it.a.text if (it.a.text or "") != (it.b.text or "") else it.a.name
        what.append(f"text changed to {quote(new)}" if new else "text removed")
    what += style_pieces(it, tokens_by_prop, with_tokens)
    if what:
        return f"{label(it.b)} {'; '.join(what)}{size_phrase(it)}{push_phrase(it)}."
    if it.resized:
        return (f"{label(it.b)} changed size from {px(it.b.rect.w)}x{px(it.b.rect.h)} to "
                f"{px(it.a.rect.w)}x{px(it.a.rect.h)}, with no change in its text or styles"
                f"{push_phrase(it)}.")
    return (f"{label(it.b)} moved {direction([it])}, and nothing measured on the page explains it"
            f"{push_phrase(it)}.")
