"""The structural half of a diff: match two layout manifests element by element, say what
changed on each, and collapse the cascade so a cause is reported once.

Matching. Most elements keep their manifest key between captures. A key can change with the
element (a heading's text is part of its key) or move to another element (a positional key
like `div[2]` names a different card once the first is removed). So:

1. Equal keys are paired, except a positional key whose content (tag, role, name, own text,
   the names of its children) differs: that pair is undone and goes to step 2.
2. Unpaired elements are grouped by (paired parent, tag, role). In a group, an element pairs
   first with one of the same content; what is left pairs in document order when both sides
   have the same number left (one element edited in place). Otherwise it is added or removed.
   This repeats until nothing more pairs, so children follow their parents.

The cascade. An element with a change of its own (added, removed, text, styles) is a cause. A
box that only moved or only changed size is an effect when something explains it:

- a box that grew or shrank with a cause inside it (a section grows with its heading);
- a box stretched beside a sibling that grew the same way (cards in a flex row);
- a box that moved with its parent by the same amount follows its parent's cause;
- a box that moved otherwise is put on the nearest cause before it in document order
  (above it, or before it on the same row, or an ancestor), preferring one whose growth
  equals the move;
- a style change that only repeats an ancestor's change of an inherited property.

What nothing explains stays a cause of its own, worded as such and never given a reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from openajar.studio.contracts import Element, Rect

TOL = 0.5   # px: rects in the manifest are rounded to 0.1 px; below this nothing moved
INHERITED = frozenset({"color", "font-family", "font-size", "font-weight", "line-height", "letter-spacing"})

# A style the manifest leaves out has its initial value; it is spelled out so a change from or
# to it reads as one.
STYLE_DEFAULT = {
    "background-color": "transparent", "padding": "0px", "margin": "0px", "gap": "normal",
    "border-radius": "0px", "border-width": "0px", "box-shadow": "none", "letter-spacing": "normal",
}

_POSITIONAL = re.compile(r"(?:^|> |/)[a-z0-9-]+\[\d+\](?: #\d+)?$")
_DIGITS = re.compile(r"[0-9]+")


def _norm(s: str | None) -> str:
    return " ".join(_DIGITS.sub(" ", s or "").split()).lower()


@dataclass
class Item:
    """One element that differs, before its place in the cascade is decided."""

    key: str                            # the after key; the before key for a removed element
    b: Element | None                   # before
    a: Element | None                   # after
    pos: float                          # place in document order (after side)
    styles: dict[str, tuple[str, str]] = field(default_factory=dict)
    text: bool = False                  # own text or accessible name changed
    inside: int = 0                     # added or removed: elements inside it, not listed apart
    cause: Item | None = None           # the cause this is an effect of
    effects: list[Item] = field(default_factory=list)

    @property
    def added(self) -> bool:
        return self.b is None

    @property
    def removed(self) -> bool:
        return self.a is None

    @property
    def own(self) -> bool:
        """A change of its own, not of its box."""
        return self.added or self.removed or self.text or bool(self.styles)

    @property
    def dx(self) -> float:
        return 0.0 if self.a is None or self.b is None else self.a.rect.x - self.b.rect.x

    @property
    def dy(self) -> float:
        return 0.0 if self.a is None or self.b is None else self.a.rect.y - self.b.rect.y

    @property
    def dw(self) -> float:
        return 0.0 if self.a is None or self.b is None else self.a.rect.w - self.b.rect.w

    @property
    def dh(self) -> float:
        return 0.0 if self.a is None or self.b is None else self.a.rect.h - self.b.rect.h

    @property
    def moved(self) -> bool:
        return abs(self.dx) > TOL or abs(self.dy) > TOL

    @property
    def resized(self) -> bool:
        return abs(self.dw) > TOL or abs(self.dh) > TOL

    @property
    def kind(self) -> str:
        if self.added:
            return "added"
        if self.removed:
            return "removed"
        if self.text:
            return "text"
        if self.styles:
            return "restyled"
        if self.resized:
            return "resized"
        return "moved"

    @property
    def shift(self) -> tuple[float, float]:
        """How far this pushes what follows it: its size for an added or removed element, its
        growth otherwise."""
        if self.added:
            return self.a.rect.w, self.a.rect.h
        if self.removed:
            return -self.b.rect.w, -self.b.rect.h
        return self.dw, self.dh

    @property
    def rect(self) -> Rect:
        return (self.a or self.b).rect

    def root(self) -> Item:
        it = self
        while it.cause is not None:
            it = it.cause
        return it


@dataclass
class Structure:
    items: list[Item]                       # every difference, causes and effects
    pairs: dict[str, str]                   # before key -> after key
    before: dict[str, Element]
    after: dict[str, Element]


# ---- matching ----


def _kids(elements: list[Element]) -> dict[str | None, list[Element]]:
    out: dict[str | None, list[Element]] = {}
    for e in elements:
        out.setdefault(e.parent, []).append(e)
    return out


def _content(e: Element, kids: dict[str | None, list[Element]]) -> tuple:
    children = tuple((c.role or c.tag, _norm(c.name or c.text)) for c in kids.get(e.key, [])[:8])
    return e.tag, e.role, _norm(e.name), _norm(e.text), children


class _Contents:
    """Each element's content signature, worked out once: matching compares the same elements
    many times (every free element against every candidate, round after round)."""

    def __init__(self, kids: dict[str | None, list[Element]]):
        self.kids = kids
        self.seen: dict[str, tuple] = {}

    def __call__(self, e: Element) -> tuple:
        got = self.seen.get(e.key)
        if got is None:
            got = self.seen[e.key] = _content(e, self.kids)
        return got


def match(before: list[Element], after: list[Element]) -> dict[str, str]:
    """Before key -> after key for every element that is the same element on both sides."""
    akeys = {e.key: e for e in after}
    bcontent, acontent = _Contents(_kids(before)), _Contents(_kids(after))
    pairs: dict[str, str] = {}
    for e in before:
        other = akeys.get(e.key)
        if other is None:
            continue
        if _POSITIONAL.search(e.key) and bcontent(e) != acontent(other):
            continue
        pairs[e.key] = e.key
    # then, round by round, the free elements under parents already paired
    while _pair_round(pairs, before, after, bcontent, acontent):
        pass
    return pairs


def _pair_round(pairs: dict[str, str], before: list[Element], after: list[Element], bcontent: _Contents,
                acontent: _Contents) -> bool:
    """Pair free elements of the same tag and role under paired parents: by content first, then,
    when as many are left on both sides, in order. Returns whether any pair was added."""
    taken = set(pairs.values())
    free_a = [e for e in after if e.key not in taken]
    free_b = [e for e in before if e.key not in pairs]
    if not free_a or not free_b:
        return False
    groups_a: dict[tuple, list[Element]] = {}
    for e in free_a:
        groups_a.setdefault((e.parent, e.tag, e.role), []).append(e)
    groups_b: dict[tuple, list[Element]] = {}
    for e in free_b:
        if e.parent is not None and e.parent not in pairs:
            continue   # its parent is not placed yet (or is gone)
        groups_b.setdefault((pairs.get(e.parent) if e.parent else None, e.tag, e.role), []).append(e)
    grew = False
    for g, bs in groups_b.items():
        as_ = groups_a.get(g, [])
        used: set[str] = set()
        for b in bs:
            cb = bcontent(b)
            for a in as_:
                if a.key not in used and acontent(a) == cb:
                    pairs[b.key] = a.key
                    used.add(a.key)
                    grew = True
                    break
        rest_b = [b for b in bs if b.key not in pairs]
        rest_a = [a for a in as_ if a.key not in used]
        if rest_b and len(rest_b) == len(rest_a):
            for b, a in zip(rest_b, rest_a, strict=True):
                pairs[b.key] = a.key
            grew = True
    return grew


# ---- what differs ----


def _styles(b: Element, a: Element) -> dict[str, tuple[str, str]]:
    out = {}
    for prop in sorted(set(b.styles) | set(a.styles)):
        vb = b.styles.get(prop, STYLE_DEFAULT.get(prop, "none"))
        va = a.styles.get(prop, STYLE_DEFAULT.get(prop, "none"))
        if vb != va:
            out[prop] = (vb, va)
    return out


def _ancestors(key: str | None, by: dict[str, Element]) -> list[str]:
    out = []
    seen = set()
    while key is not None and key in by and key not in seen:
        seen.add(key)
        key = by[key].parent
        if key is not None:
            out.append(key)
    return out


def _inside(r: Rect, masks: list[Rect], slack: float = 2.0) -> bool:
    return any(r.x >= m.x - slack and r.y >= m.y - slack and r.x + r.w <= m.x + m.w + slack
               and r.y + r.h <= m.y + m.h + slack for m in masks)


def compare(before: list[Element], after: list[Element], *, ignore_keys: frozenset[str] = frozenset(),
            ignore_rects: list[Rect] | None = None) -> Structure:
    """Every element that differs, with its place in the cascade decided. Elements named in
    `ignore_keys`, or lying inside `ignore_rects` on every side they exist, are noise."""
    ignore_rects = ignore_rects or []
    pairs = match(before, after)
    bby = {e.key: e for e in before}
    aby = {e.key: e for e in after}
    apos = {e.key: i for i, e in enumerate(after)}
    back = {v: k for k, v in pairs.items()}

    noise = _Noise(ignore_keys, ignore_rects)
    gone = {e.key for e in before if e.key not in pairs}
    new = {e.key for e in after if e.key not in back}
    removed_pos = _removed_positions(before, pairs, apos, len(after))
    # removed and added: only the outermost element of a subtree that went or came
    items = [Item(key=e.key, b=e, a=None, pos=removed_pos[e.key])
             for e in _outermost(before, gone, bby) if not noise(e, None)]
    items += [Item(key=e.key, b=None, a=e, pos=apos[e.key]) for e in _outermost(after, new, aby) if not noise(None, e)]
    items += _changed(pairs, bby, aby, apos, noise)
    for it in items:
        if it.added:
            it.inside = sum(1 for k in new if it.key in _ancestors(k, aby))
        elif it.removed:
            it.inside = sum(1 for k in gone if it.key in _ancestors(k, bby))
    items.sort(key=lambda i: i.pos)
    _cascade(items, bby, aby)
    return Structure(items=items, pairs=pairs, before=bby, after=aby)


class _Noise:
    """An element named in `ignore_keys`, or inside `ignore_rects` on every side it exists."""

    def __init__(self, ignore_keys: frozenset[str], ignore_rects: list[Rect]):
        self.keys = ignore_keys
        self.rects = ignore_rects

    def __call__(self, b: Element | None, a: Element | None) -> bool:
        if (b is not None and b.key in self.keys) or (a is not None and a.key in self.keys):
            return True
        rects = [e.rect for e in (b, a) if e is not None]
        return bool(self.rects) and all(_inside(r, self.rects) for r in rects)


def _changed(pairs: dict[str, str], bby: dict[str, Element], aby: dict[str, Element], apos: dict[str, int],
             noise: _Noise) -> list[Item]:
    """The paired elements whose own style or text changed, or that moved or changed size."""
    out = []
    for bk, ak in pairs.items():
        b, a = bby[bk], aby[ak]
        if noise(b, a):
            continue
        it = Item(key=ak, b=b, a=a, pos=apos[ak], styles=_styles(b, a),
                  text=(b.text or "") != (a.text or "") or (b.name or "") != (a.name or ""))
        if it.own or it.moved or it.resized:
            out.append(it)
    return out


def _outermost(elements: list[Element], keys: set[str], by: dict[str, Element]) -> list[Element]:
    """The elements in `keys` with no ancestor in `keys`, in page order."""
    return [e for e in elements if e.key in keys and not any(k in keys for k in _ancestors(e.key, by))]


def _removed_positions(before: list[Element], pairs: dict[str, str], apos: dict[str, int],
                       end: int) -> dict[str, float]:
    """A removed element is placed just before the next element that is still there."""
    nxt = float(end)
    out: dict[str, float] = {}
    for e in reversed(before):
        if e.key in pairs:
            nxt = apos[pairs[e.key]]
        else:
            out[e.key] = nxt - 0.5
    return out


# ---- the cascade ----


def _tree_ancestors(it: Item, bby: dict[str, Element], aby: dict[str, Element]) -> set[str]:
    out: set[str] = set()
    if it.a is not None:
        out.update(_ancestors(it.a.key, aby))
    if it.b is not None:
        out.update(_ancestors(it.b.key, bby))
    return out


def _keys(it: Item) -> set[str]:
    return {e.key for e in (it.b, it.a) if e is not None}


def _same(x: float, y: float) -> bool:
    return abs(x - y) <= 1.0


def _cascade(items: list[Item], bby: dict[str, Element], aby: dict[str, Element]) -> None:
    """Decide each item's cause: own changes first, then boxes that changed size, then boxes that
    moved. Every effect points at the root cause, which lists its effects."""
    c = _Cascade(items, bby, aby)
    c.own_changes()
    c.resized_boxes()
    c.causes.sort(key=lambda x: x.pos)
    c.moved_boxes()
    for it in items:
        if it.cause is not None:
            root = it.root()
            it.cause = root
            root.effects.append(it)


class _Cascade:
    """The items of one compare and the causes found so far, in page order."""

    def __init__(self, items: list[Item], bby: dict[str, Element], aby: dict[str, Element]):
        self.items = items
        self.anc = {id(it): _tree_ancestors(it, bby, aby) for it in items}
        self.by_key: dict[str, Item] = {}
        for it in items:
            for k in _keys(it):
                self.by_key[k] = it
        self.causes: list[Item] = []

    def is_ancestor(self, x: Item, of: Item) -> bool:
        return bool(_keys(x) & self.anc[id(of)])

    def parent_item(self, it: Item) -> Item | None:
        p = it.a.parent if it.a is not None else (it.b.parent if it.b is not None else None)
        return self.by_key.get(p) if p else None

    def own_changes(self) -> None:
        """1. Own changes are causes, but a restyle that repeats an ancestor's inherited change is not."""
        for it in self.items:
            if not it.own:
                continue
            if it.styles and not it.text and not it.resized and set(it.styles) <= INHERITED:
                up = next((c for c in reversed(self.causes) if self.is_ancestor(c, it)
                           and all(c.styles.get(p) == v for p, v in it.styles.items())), None)
                if up is not None:
                    it.cause = up
                    continue
            self.causes.append(it)

    def resized_boxes(self) -> None:
        """2. A box that changed size: a cause inside it, else a sibling that grew the same way."""
        for it in reversed(self.items):
            if it.own or not it.resized:
                continue
            inner = [c for c in self.causes if self.is_ancestor(it, c)]
            if inner:
                it.cause = min(inner, key=lambda c: c.pos)
                continue
            parent = it.a.parent if it.a is not None else None
            sib = [c for c in self.causes if c.a is not None and c.a.parent == parent and c is not it
                   and ((abs(it.dh) > TOL and _same(c.shift[1], it.dh))
                        or (abs(it.dw) > TOL and _same(c.shift[0], it.dw)))]
            if sib:
                it.cause = min(sib, key=lambda c: abs(c.pos - it.pos))
                continue
            if not it.moved:
                self.causes.append(it)

    def moved_boxes(self) -> None:
        """3. A box that moved: with its parent, else the nearest cause before it."""
        for it in self.items:
            if it.own or it.cause is not None or not it.moved:
                continue
            p = self.parent_item(it)
            if p is not None and p.moved and _same(p.dx, it.dx) and _same(p.dy, it.dy):
                if p.cause is not None:
                    it.cause = p.root()
                    continue
                if not p.own and p in self.causes:
                    it.cause = p
                    continue
            cand = [x for c in self.causes if (x := self._candidate(c, it)) is not None]
            if cand:
                it.cause = min(cand, key=lambda t: (t[0], t[1]))[2]
            else:
                self.causes.append(it)
                self.causes.sort(key=lambda c: c.pos)

    def _candidate(self, c: Item, it: Item) -> tuple[bool, float, Item] | None:
        """Cause `c` as a candidate for moved box `it`: before it, not inside it, above it when it
        moved down (or level with it when it moved across). The sort key prefers one whose shift
        fits the move, then the nearest."""
        if c is it or c.pos >= it.pos or self.is_ancestor(it, c):
            return None
        if not self.is_ancestor(c, it):
            cr, er = (c.a or c.b).rect, (it.b or it.a).rect
            if c.added:
                er = it.a.rect
            vertical = abs(it.dy) > TOL
            if vertical and cr.y > er.y + TOL:
                return None
            if not vertical and (cr.y >= er.y + er.h or cr.y + cr.h <= er.y):
                return None
        sx, sy = c.shift
        fits = (abs(it.dy) > TOL and _same(sy, it.dy)) or (abs(it.dx) > TOL and _same(sx, it.dx))
        return (not fits, -c.pos, c)


def moved_effects(cause: Item) -> list[Item]:
    return [e for e in cause.effects if e.moved]


def outermost(effects: list[Item], aby: dict[str, Element]) -> list[Item]:
    """The moved effects worth listing: not those that only ride along inside a moved parent."""
    by_key = {e.a.key: e for e in effects if e.a is not None}
    out = []
    for e in effects:
        p = by_key.get(e.a.parent) if e.a is not None and e.a.parent else None
        if p is not None and e.moved and p.moved and _same(p.dx, e.dx) and _same(p.dy, e.dy) and not e.resized:
            continue
        out.append(e)
    return out
