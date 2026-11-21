"""Turning authored values into a design system, when a site declares none.

Every function here is pure: it takes the values read from a page (see read.py) and returns
clusters, a step or a scale. Everything it decides is a heuristic, and says so where it is used.

The unit of counting is an *origin*: one place in a page where a value is written (an element and
a property family, across widths). A value an element only inherits is not counted again, but an
origin's `reach` (the elements below it that inherit it) lets a base value (the body's text
colour, the body's font size) count as part of the system although it is written once.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise

from ..tokens.color import NEAR_DE, OKLab, delta_e, parse_color

# A cluster becomes a token when it is written in at least this many places, or when one of its
# origins is inherited by at least BASE_REACH elements (a base value written once).
MIN_USES = 2
BASE_REACH = 3
# Colours closer than this (delta E OK) are one colour: half a just noticeable difference (0.02),
# so a near-white surface (#f9fafb) and white stay two tokens while sloppy copies of one colour
# (#fafafa for #f9fafb) join.
JOIN_DE = NEAR_DE / 2
# A colour with at least this chroma (OKLab a, b) is taken as an accent, not a neutral.
ACCENT_CHROMA = 0.06
# Lengths closer than this are one value.
JOIN_PX = 0.5
# A spacing step fits when this share of spacing origins are multiples of it.
STEP_FIT = 0.85
STEP_CANDIDATES = (16.0, 12.0, 10.0, 8.0, 6.0, 5.0, 4.0, 3.0, 2.0)
FULL_RADIUS_PX = 999.0

ROLE_OF_PROP = {"color": "text", "background-color": "background"}
WEIGHT_NAMES = {100: "thin", 200: "extralight", 300: "light", 400: "regular", 500: "medium", 600: "semibold",
                700: "bold", 800: "extrabold", 900: "black"}
SIZE_UP = ["lg", "xl", "2xl", "3xl", "4xl", "5xl", "6xl", "7xl", "8xl", "9xl"]
SIZE_DOWN = ["sm", "xs", "2xs", "3xs", "4xs"]
GENERIC_FAMILIES = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui", "ui-serif",
                    "ui-sans-serif", "ui-monospace", "ui-rounded", "math", "emoji", "fangsong", "-apple-system",
                    "blinkmacsystemfont", "inherit", "initial"}


@dataclass
class Origin:
    """One place a value is written: (page, selector, property family), in one theme."""

    loc: tuple[int, str, str]
    prop: str
    category: str
    value: str
    tag: str
    reach: int = 0


def role_of(prop: str) -> str:
    if prop in ROLE_OF_PROP:
        return ROLE_OF_PROP[prop]
    if prop.startswith("border-") and prop.endswith("-color"):
        return "border"
    return "text"


def chroma(c: OKLab) -> float:
    return math.hypot(c.a, c.b)


def counts_as_system(uses: int, reach: int) -> bool:
    return uses >= MIN_USES or reach >= BASE_REACH


# ---- colours -------------------------------------------------------------------------------


@dataclass
class ColorCluster:
    center: OKLab
    uses: int = 0
    reach: int = 0
    roles: Counter = field(default_factory=Counter)
    members: Counter = field(default_factory=Counter)     # hex -> uses
    locs: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def token(self) -> bool:
        return counts_as_system(self.uses, self.reach)

    @property
    def role(self) -> str:
        if chroma(self.center) >= ACCENT_CHROMA:
            return "accent"
        return self.roles.most_common(1)[0][0] if self.roles else "text"


def cluster_colors(origins: list[Origin]) -> list[ColorCluster]:
    """Greedy, most used first: a colour joins the first cluster within JOIN_DE whose opacity
    agrees, else it starts one. The centre is the most used colour of the cluster."""
    by_value: dict[str, list[Origin]] = {}
    parsed: dict[str, OKLab] = {}
    for o in origins:
        c = parse_color(o.value)
        if c is None:
            continue
        key = c.hex()
        parsed.setdefault(key, c)
        by_value.setdefault(key, []).append(o)
    order = sorted(by_value, key=lambda k: (-len(by_value[k]), -sum(o.reach for o in by_value[k]), k))
    clusters: list[ColorCluster] = []
    for key in order:
        c = parsed[key]
        home = next((cl for cl in clusters if abs(cl.center.alpha - c.alpha) < 0.02 and delta_e(cl.center, c) < JOIN_DE),
                    None)
        if home is None:
            home = ColorCluster(center=c)
            clusters.append(home)
        for o in by_value[key]:
            home.uses += 1
            home.reach = max(home.reach, o.reach)
            home.roles[role_of(o.prop)] += 1
            home.members[key] += 1
            home.locs.append(o.loc)
    clusters.sort(key=lambda cl: (-cl.uses, -cl.reach, cl.center.hex()))
    return clusters


def name_colors(clusters: list[ColorCluster]) -> dict[int, str]:
    """color.<role>.<n>, n from 1 within the role by places written plus elements reached (the
    body's text colour, written once and inherited everywhere, is text.1). Index -> name."""
    seen: Counter = Counter()
    out = {}
    order = sorted(range(len(clusters)), key=lambda i: (-(clusters[i].uses + clusters[i].reach), i))
    for i in order:
        cl = clusters[i]
        if not cl.token:
            continue
        seen[cl.role] += 1
        out[i] = f"color.{cl.role}.{seen[cl.role]}"
    return out


# ---- lengths -------------------------------------------------------------------------------


@dataclass
class LengthCluster:
    px: float
    uses: int = 0
    reach: int = 0
    members: Counter = field(default_factory=Counter)

    @property
    def token(self) -> bool:
        return counts_as_system(self.uses, self.reach)


def cluster_lengths(values: list[tuple[float, int]]) -> list[LengthCluster]:
    """(px, reach) pairs into clusters JOIN_PX wide, most used first."""
    counts: Counter = Counter()
    reach: dict[float, int] = {}
    for px, r in values:
        v = round(px * 4) / 4
        counts[v] += 1
        reach[v] = max(reach.get(v, 0), r)
    clusters: list[LengthCluster] = []
    for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        home = next((cl for cl in clusters if abs(cl.px - v) <= JOIN_PX), None)
        if home is None:
            home = LengthCluster(px=v)
            clusters.append(home)
        home.uses += n
        home.reach = max(home.reach, reach[v])
        home.members[v] += n
    clusters.sort(key=lambda cl: cl.px)
    return clusters


def _on_step(px: float, step: float) -> bool:
    n = round(px / step)
    return n >= 1 and abs(px - n * step) <= JOIN_PX


def detect_step(values: list[float]) -> tuple[float | None, float]:
    """The largest step of which at least STEP_FIT of the values are multiples, and the share
    that fit. Needs three distinct values at least; a step of 1 px proves nothing and is never
    tried."""
    vals = [v for v in values if v > 0]
    if len({round(v * 4) for v in vals}) < 3:
        return None, 0.0
    best: tuple[float | None, float] = (None, 0.0)
    for step in STEP_CANDIDATES:
        fit = sum(1 for v in vals if _on_step(v, step)) / len(vals)
        if fit >= STEP_FIT:
            return step, fit
        if fit > best[1]:
            best = (None, fit)
    return best


def radius_names(pxs: list[float]) -> dict[float, str]:
    out = {}
    n = 0
    for px in sorted(pxs):
        if px >= FULL_RADIUS_PX:
            out[px] = "full"
        else:
            n += 1
            out[px] = str(n)
    return out


def type_scale(sizes: list[float], base: float | None) -> tuple[dict[float, str], float | None]:
    """Names for font sizes around the base (base, sm, xs, ... below; lg, xl, 2xl, ... above),
    and the ratio between neighbours above the base when it is steady (within 8 %)."""
    sizes = sorted(set(sizes))
    if not sizes:
        return {}, None
    if base is None or base not in sizes:
        base = min(sizes, key=lambda s: abs(s - 16))
    i = sizes.index(base)
    names = {base: "base"}
    for k, s in enumerate(reversed(sizes[:i])):
        names[s] = SIZE_DOWN[k] if k < len(SIZE_DOWN) else f"down{k + 1}"
    for k, s in enumerate(sizes[i + 1:]):
        names[s] = SIZE_UP[k] if k < len(SIZE_UP) else f"up{k + 1}"
    up = sizes[i:]
    ratio = None
    if len(up) >= 3:
        ratios = [b / a for a, b in pairwise(up)]
        mean = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
        if all(abs(r / mean - 1) <= 0.08 for r in ratios):
            ratio = round(mean, 3)
    return names, ratio


def weight_name(w: float) -> str:
    return WEIGHT_NAMES.get(int(w), str(int(w)))

