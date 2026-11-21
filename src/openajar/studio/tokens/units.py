"""Lengths, numbers, font stacks and shadows, read from CSS text into plain values."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .color import OKLab, parse_color

ROOT_PX = 16.0
EXACT_PX = 0.5

_ABS = {"px": 1.0, "pt": 96 / 72, "pc": 16.0, "in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4, "q": 96 / 101.6}
_LEN = re.compile(r"^\s*(-?(?:\d+\.?\d*|\.\d+)(?:e-?\d+)?)\s*([a-z%]*)\s*$", re.IGNORECASE)


def parse_length(text, *, root_px: float = ROOT_PX, em_px: float | None = None) -> float | None:
    """A length in px. rem uses the root font size, em the element's (the root's when unknown).
    Unitless 0 is 0. A bare number is None: it is not a length. calc() with plain arithmetic
    is evaluated."""
    if isinstance(text, (int, float)):
        return float(text) if text == 0 else None
    if not isinstance(text, str):
        return None
    s = text.strip().lower()
    if s.startswith(("calc(", "(")):
        return _calc_length(s, root_px, em_px)
    m = _LEN.match(s)
    if not m:
        return None
    v, unit = float(m.group(1)), m.group(2)
    if unit == "":
        return 0.0 if v == 0 else None
    if unit in _ABS:
        return v * _ABS[unit]
    if unit == "rem":
        return v * root_px
    if unit == "em":
        return v * (em_px if em_px is not None else root_px)
    return None


def parse_number(text) -> float | None:
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    if not isinstance(text, str):
        return None
    s = text.strip().lower()
    if s.startswith("calc("):
        r = _calc(s, lambda tok: None)
        return r[0] if r and r[1] == "num" else None
    try:
        return float(s)
    except ValueError:
        return None


# --- a small calc() for what token files write: + - * / and parentheses ---------------------

_TOK = re.compile(r"\s*(?:(-?(?:\d+\.?\d*|\.\d+)(?:e-?\d+)?)([a-z%]*)|(calc\()|([-+*/()]))", re.IGNORECASE)


def _calc(text: str, unit_px):
    """Evaluate to (value, "num" | "px"), or None when it holds anything else."""
    toks = []
    pos = 0
    while pos < len(text):
        m = _TOK.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            return None
        pos = m.end()
        if m.group(1) is not None:
            unit = m.group(2).lower()
            if unit == "":
                toks.append(("val", float(m.group(1)), "num"))
            else:
                px = unit_px(m.group(1) + unit)
                if px is None:
                    return None
                toks.append(("val", px, "px"))
        elif m.group(3):
            toks.append(("op", "("))
        else:
            toks.append(("op", m.group(4)))
    i = 0

    def expr():
        nonlocal i
        left = term()
        while left is not None and i < len(toks) and toks[i] in (("op", "+"), ("op", "-")):
            op = toks[i][1]
            i += 1
            right = term()
            if right is None or right[1] != left[1]:
                return None
            left = (left[0] + right[0] if op == "+" else left[0] - right[0], left[1])
        return left

    def term():
        nonlocal i
        left = factor()
        while left is not None and i < len(toks) and toks[i] in (("op", "*"), ("op", "/")):
            op = toks[i][1]
            i += 1
            right = factor()
            if right is None:
                return None
            if op == "*":
                if left[1] == "px" and right[1] == "px":
                    return None
                left = (left[0] * right[0], "px" if "px" in (left[1], right[1]) else "num")
            else:
                if right[1] != "num" or right[0] == 0:
                    return None
                left = (left[0] / right[0], left[1])
        return left

    def factor():
        nonlocal i
        if i >= len(toks):
            return None
        t = toks[i]
        if t == ("op", "("):
            i += 1
            v = expr()
            if i < len(toks) and toks[i] == ("op", ")"):
                i += 1
                return v
            return None
        if t == ("op", "-"):
            i += 1
            v = factor()
            return None if v is None else (-v[0], v[1])
        if t[0] == "val":
            i += 1
            return (t[1], t[2])
        return None

    v = expr()
    if v is None or i != len(toks):
        return None
    return v


def _calc_length(text: str, root_px: float, em_px: float | None) -> float | None:
    r = _calc(text, lambda tok: parse_length(tok, root_px=root_px, em_px=em_px))
    if r is None:
        return None
    if r[1] == "num":
        return 0.0 if r[0] == 0 else None
    return r[0]


def fmt_px(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return ("0" if s in ("-0", "") else s) + "px"


def fmt_num(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# --- font stacks --------------------------------------------------------------------------

def font_stack(text: str) -> list[str]:
    """The families in a font-family value, lowercased and unquoted."""
    out = []
    for part in re.findall(r'"[^"]*"|\'[^\']*\'|[^,]+', text or ""):
        p = part.strip().strip("'\"").strip().lower()
        if p:
            out.append(re.sub(r"\s+", " ", p))
    return out


WEIGHT_NAMES = {
    "thin": 100, "hairline": 100, "extra-light": 200, "extralight": 200, "ultra-light": 200,
    "light": 300, "normal": 400, "regular": 400, "book": 400, "medium": 500, "semi-bold": 600,
    "semibold": 600, "demi-bold": 600, "bold": 700, "extra-bold": 800, "extrabold": 800,
    "ultra-bold": 800, "black": 900, "heavy": 900, "extra-black": 950, "ultra-black": 950,
}


def parse_weight(v) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in WEIGHT_NAMES:
            return float(WEIGHT_NAMES[s])
        return parse_number(s)
    return None


# --- shadows ------------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowLayer:
    x: float
    y: float
    blur: float
    spread: float
    color: OKLab | None
    inset: bool = False

    def css(self) -> str:
        parts = ["inset"] if self.inset else []
        parts += [fmt_px(self.x), fmt_px(self.y), fmt_px(self.blur), fmt_px(self.spread)]
        if self.color is not None:
            parts.append(self.color.css())
        return " ".join(parts)


def _split_top(text: str, sep: str = ",") -> list[str]:
    out, depth, cur = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [p.strip() for p in out if p.strip()]


def parse_shadow(text: str, *, root_px: float = ROOT_PX) -> list[ShadowLayer] | None:
    """A box-shadow value as layers, or None when a part cannot be read. `none` is []."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if s.lower() == "none" or not s:
        return []
    layers = []
    for layer in _split_top(s):
        inset = False
        color = None
        lengths: list[float] = []
        for tok in re.findall(r"[a-z-]+\([^()]*(?:\([^()]*\)[^()]*)*\)|\S+", layer, re.IGNORECASE):
            if tok.lower() == "inset":
                inset = True
                continue
            px = parse_length(tok, root_px=root_px)
            if px is not None:
                lengths.append(px)
                continue
            c = parse_color(tok)
            if c is None:
                return None
            color = c
        if len(lengths) < 2 or len(lengths) > 4:
            return None
        lengths += [0.0] * (4 - len(lengths))
        layers.append(ShadowLayer(lengths[0], lengths[1], lengths[2], lengths[3], color, inset))
    return layers


def shadow_css(layers: list[ShadowLayer]) -> str:
    return ", ".join(layer.css() for layer in layers) if layers else "none"
