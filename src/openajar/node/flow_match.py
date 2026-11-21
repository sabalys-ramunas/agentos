"""How a locator's candidates are matched and ordered: accessible-name queries and their number
variants, attribute CSS, tree order, and the `own` filter that drops candidates nested inside
another element with the anchor's role.
"""

from __future__ import annotations

import re
from bisect import bisect_right

from openajar.node.snapshot import parse_line

# ---------------------------------------------------------------- small helpers


def _norm(value: str) -> str:
    return " ".join(value.split())


_REGEX_SPECIALS = set("\\^$.|?*+()[]{}/-")


def regex_escape(text: str) -> str:
    """Escape for a pattern that both Python (the fake, Locator validation) and the browser's
    JavaScript regex engine read the same way."""
    return "".join("\\" + ch if ch in _REGEX_SPECIALS else ch for ch in text)


def attr_css(name: str) -> str:
    """`[name]` for an attribute name, escaped for CSS (names may hold ':' and '.')."""
    return "[" + re.sub(r"([^A-Za-z0-9_-])", r"\\\1", name) + "]"


def tree_order(path: str) -> tuple[int, ...]:
    """Sort key for element paths: document order, a shadow root (-1) before light children."""
    return tuple(int(x) for x in path.split("/") if x)


def record_levels(paths: list[str], pick: str | None) -> list[int | None]:
    """ExtractStep.pick over element paths in tree order: per match, how many levels up its record
    is (0: the match itself, `card`), or None to drop it (`outermost`)."""
    if pick == "outermost":  # in tree order a match's descendants come right after it
        out: list[int | None] = []
        top: str | None = None
        for p in paths:
            if top is not None and inside(top, p):
                out.append(None)
            else:
                top = p
                out.append(0)
        return out
    if pick != "card":
        return [0] * len(paths)
    parts = [p.split("/") for p in paths]

    def shared(a: list[str], b: list[str]) -> int:
        n = 0
        while n < min(len(a), len(b)) and a[n] == b[n]:
            n += 1
        return n

    out = []
    for i, mine in enumerate(parts):
        near = [shared(mine, parts[j]) for j in (i - 1, i + 1) if 0 <= j < len(parts)]
        keep = (max(near) if near else max(len(mine) - 1, 0)) + 1  # the first level no other match shares
        if "-1" in mine[keep:]:  # never up through a shadow root
            keep = len(mine) - mine[::-1].index("-1") + 1  # the first element under that root
        out.append(max(len(mine) - keep, 0))
    return out


def inside(outer: str, inner: str) -> bool:
    """True if the element at path `inner` is a strict descendant of the one at `outer`."""
    return inner.startswith(outer + "/")


def own_filter(paths: list[str], anchor: str, nested: list[str]) -> list[bool]:
    """For each candidate path: kept unless it sits inside a nested element (strictly inside
    the anchor) that has the anchor's role. Mirrors fakes._nested_same_role."""
    inner = sorted((n for n in nested if inside(anchor, n)), key=tree_order)
    walls = [w for w, up in zip(inner, record_levels(inner, "outermost"), strict=True) if up == 0]
    starts = [tree_order(w) for w in walls]

    def kept(path: str) -> bool:  # subtrees of outermost walls do not overlap: only the last one before it can hold it
        at = bisect_right(starts, tree_order(path)) - 1
        return at < 0 or not inside(walls[at], path)

    return [kept(p) for p in paths]


_NUMBER_RE = re.compile(r"\d[\d.,]*[KkMm]?")
_NUMBER_PATTERN = r"\d[\d.,]*[KkMm]?"


def name_variants(name: str | None) -> list[tuple[str | None, str]]:
    """(name, match) choices for a recorded locator, the most durable first. Counts in a name
    ("Like 12") change, so a pattern that accepts any number comes first; very long names
    (a link around a whole post) match on their start."""
    if name is None:
        return [(None, "exact")]
    name = _norm(name)
    if not name:
        return [(None, "exact")]
    out: list[tuple[str | None, str]] = []
    if _NUMBER_RE.search(name):
        pieces = _NUMBER_RE.split(name)
        out.append(("^" + _NUMBER_PATTERN.join(regex_escape(p) for p in pieces) + "$", "regex"))
    if len(name) > 80:
        head = name[:60].rsplit(" ", 1)[0] or name[:60]
        out.append((head, "contains"))
    out.append((name, "exact"))
    return out


def ref_role_and_name(snapshot: str, ref: str) -> tuple[str | None, str | None]:
    """Role and name of the line that carries `[ref=<ref>]` in a snapshot (raw or outline)."""
    tag = f"[ref={ref}]"
    for raw in snapshot.splitlines():
        if tag in raw:
            line = parse_line(raw)
            return line.role, line.name
    return None, None


def _browser_error(e: BaseException) -> bool:
    return type(e).__module__.startswith("playwright") or isinstance(e, TimeoutError)


def _css_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
