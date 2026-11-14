"""`diff_captures`: what changed between two captures of the same target, cause first.

Per screen: match the two layout manifests and collapse the cascade (structure.py), compare the
screenshots (pixels.py), give every changed pixel to the element change that covers it, and
keep what no element change covers as an unexplained `Region` with a before and after crop.
Words come from the data (words.py). A screen missing on one side, failed on one side or
rendered at another size is `skipped` with the reason (AX rule 6).

Noise: `noise_mask(store, a, b)` takes two captures of the same unchanged state and records,
per screen, the elements and areas that differ anyway (a clock, a random id). Passing that mask
to `diff_captures` leaves them out.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from agentos.core.errors import AgentosError, NotFound
from agentos.studio.contracts import Capture, Change, DiffResult, Rect, Region, ScreenDiff, Shot, TokenSet
from agentos.studio.diff import pixels, structure
from agentos.studio.diff.structure import Item, Structure
from agentos.studio.diff.words import cause_line, font_px, label, px, style_pieces, token_names
from agentos.studio.store import Manifest, StudioStore

log = logging.getLogger("agentos.studio")

EXPLAIN_PAD = 4.0        # CSS px around an element change that its pixels may spill into (shadow, AA)
MAX_REGIONS = 20
MAX_GROUP_NAMES = 3


class ScreenMask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[str] = Field(default_factory=list)      # manifest keys that change on their own
    rects: list[Rect] = Field(default_factory=list)    # CSS px areas whose pixels change on their own


class NoiseMask(BaseModel):
    """What differs between two captures of the same unchanged state, per screen key."""

    model_config = ConfigDict(extra="forbid")

    captures: list[str] = Field(default_factory=list)
    screens: dict[str, ScreenMask] = Field(default_factory=dict)


@dataclass
class _Screen:
    diff: ScreenDiff
    structure: Structure | None = None
    changed_rects: list[Rect] = field(default_factory=list)    # every changed pixel area, explained or not


def _rect_union(a: Rect, b: Rect) -> Rect:
    x, y = min(a.x, b.x), min(a.y, b.y)
    return Rect(x=x, y=y, w=max(a.x + a.w, b.x + b.w) - x, h=max(a.y + a.h, b.y + b.h) - y)


def _explains(it: Item) -> list[Rect]:
    """The areas whose pixels an element change accounts for. A box that only changed size
    accounts for the strips it gained or lost and its edges, not its whole inside (where an
    image could have changed on its own)."""
    if it.a is None:
        return [it.b.rect]
    if it.b is None:
        return [it.a.rect]
    if it.own or it.moved:
        return [it.b.rect, it.a.rect]
    out = []
    for r, o in ((it.a.rect, it.b.rect), (it.b.rect, it.a.rect)):
        if r.h > o.h + structure.TOL:
            out.append(Rect(x=r.x, y=o.y + o.h, w=r.w, h=r.y + r.h - (o.y + o.h)))
        if r.w > o.w + structure.TOL:
            out.append(Rect(x=o.x + o.w, y=r.y, w=r.x + r.w - (o.x + o.w), h=r.h))
    for r in (it.a.rect, it.b.rect):   # the edges: a border or a shadow redrawn
        out += [Rect(x=r.x, y=r.y, w=r.w, h=1), Rect(x=r.x, y=r.y + r.h - 1, w=r.w, h=1),
                Rect(x=r.x, y=r.y, w=1, h=r.h), Rect(x=r.x + r.w - 1, y=r.y, w=1, h=r.h)]
    return out


def _covered(grid: pixels.Grid, rects: list[Rect]) -> bytearray:
    flags = bytearray(grid.cols * grid.rows)
    for r in rects:
        c0, c1, r0, r1 = grid.cells_of(r, EXPLAIN_PAD)
        if c1 <= c0:
            continue
        ones = b"\x01" * (c1 - c0)
        for row in range(r0, r1):
            flags[row * grid.cols + c0: row * grid.cols + c1] = ones
    return flags


def _smallest_cover(rect: Rect, elements: list) -> object | None:
    slack = pixels.CELL
    best = None
    for e in elements:
        r = e.rect
        covers = (r.x - slack <= rect.x and r.y - slack <= rect.y and r.x + r.w + slack >= rect.x + rect.w
                  and r.y + r.h + slack >= rect.y + rect.h)
        if covers and (best is None or r.w * r.h < best.rect.w * best.rect.h):
            best = e
    return best


def _tokens_for(it: Item, tokens: TokenSet | None, theme) -> dict[str, list[str | None]]:
    if tokens is None or not it.styles:
        return {}
    out: dict[str, list[str | None]] = {}
    for prop, (vb, va) in it.styles.items():
        tb = token_names(prop, vb, tokens, theme=theme, font_size_px=font_px(it.b))
        ta = token_names(prop, va, tokens, theme=theme, font_size_px=font_px(it.a))
        if tb or ta:
            out[prop] = [tb, ta]
    return out


def _change(it: Item, toks: dict[str, list[str | None]]) -> Change:
    before: dict = {}
    after: dict = {}
    if it.added:
        after["rect"] = it.a.rect.model_dump()
    elif it.removed:
        before["rect"] = it.b.rect.model_dump()
    else:
        if it.b.key != it.a.key:
            before["key"] = it.b.key
        if it.moved or it.resized:
            before["rect"], after["rect"] = it.b.rect.model_dump(), it.a.rect.model_dump()
        if (it.b.text or "") != (it.a.text or ""):
            before["text"], after["text"] = it.b.text, it.a.text
        if (it.b.name or "") != (it.a.name or ""):
            before["name"], after["name"] = it.b.name, it.a.name
        if it.styles:
            before["styles"] = {p: v[0] for p, v in it.styles.items()}
            after["styles"] = {p: v[1] for p, v in it.styles.items()}
    el = it.a or it.b
    return Change(kind=it.kind, key=it.key, component=el.component, before=before, after=after, tokens=toks,
                  caused_by=it.cause.key if it.cause is not None else None,
                  moved_with=len(structure.moved_effects(it)) if it.cause is None else 0)


def _lines(roots: list[Item], toks: dict[int, dict], with_tokens: bool) -> list[str]:
    """One sentence per cause. Restyles that are the same change on several elements, with
    nothing moved, are one cause and one sentence."""
    out: list[str] = []
    groups: dict[tuple, list[Item]] = {}
    order: list[tuple] = []
    for it in roots:
        plain = (it.styles and not it.text and not it.resized and not it.added and not it.removed
                 and not structure.moved_effects(it))
        key = ("style", tuple(sorted(it.styles.items()))) if plain else ("one", id(it))
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(it)
    for key in order:
        group = groups[key]
        first = group[0]
        if len(group) == 1:
            out.append(cause_line(first, toks.get(id(first), {}), with_tokens))
            continue
        names = ", ".join(label(it.b) for it in group[:MAX_GROUP_NAMES])
        more = f" and {len(group) - MAX_GROUP_NAMES} more" if len(group) > MAX_GROUP_NAMES else ""
        what = "; ".join(style_pieces(first, toks.get(id(first), {}), with_tokens))
        out.append(f"{len(group)} elements changed {what}: {names}{more}.")
    return out


def _skip(key: str, why: str) -> _Screen:
    return _Screen(ScreenDiff(screen=key, status="skipped", reason=why))


def _shots(cap: Capture) -> dict[str, Shot]:
    return {s.screen.key: s for s in cap.shots}


def _screen(store: StudioStore, before: Capture, after: Capture, key: str, *, tokens: TokenSet | None,
            mask: ScreenMask | None, crops: bool) -> _Screen:
    sb, sa = _shots(before).get(key), _shots(after).get(key)
    why = _not_comparable(key, sb, sa)
    if why is not None:
        return _skip(key, why)
    try:
        mb, ma = store.manifest(before.id, key), store.manifest(after.id, key)
        png_b, png_a = store.blob(sb.image), store.blob(sa.image)
    except NotFound as e:
        return _skip(key, str(e))
    wb, wa = pixels.image_size(png_b), pixels.image_size(png_a)
    if wb[0] != wa[0]:
        return _skip(key, f"the screenshots differ in width ({wb[0]} px before, {wa[0]} px after) at the "
                          "same screen width: capture both with the same engine and scale")
    scale = sa.screen.scale
    mask = mask or ScreenMask()
    st = structure.compare(mb.elements, ma.elements, ignore_keys=frozenset(mask.keys), ignore_rects=mask.rects)
    grid = pixels.compare(png_b, png_a, scale=scale, masks=mask.rects)
    flags = _covered(grid, [r for it in st.items for r in _explains(it)])
    changed = {i for i, n in enumerate(grid.counts) if n}
    found = pixels.regions(grid, {i for i in changed if not flags[i]})
    unexplained, region_lines = _unexplained(store, after.id, key, found, (png_b, png_a), scale, grid.total,
                                             ma.elements, crops=crops)

    roots = [it for it in st.items if it.cause is None]
    theme = sa.screen.theme
    toks = {id(it): _tokens_for(it, tokens, theme) for it in st.items}
    changes = [_change(it, toks[id(it)]) for it in roots]
    for r in roots:
        changes += [_change(e, toks[id(e)]) for e in structure.outermost(r.effects, st.after)]
    lines = _lines(roots, toks, tokens is not None) + region_lines
    for side, m in (("before", mb), ("after", ma)):
        lines += _manifest_notes(side, m)
    status = "changed" if changes or unexplained else "same"
    masked_share = round(grid.changed / grid.total, 6) if grid.total else 0.0
    diff = ScreenDiff(screen=key, status=status, changes=changes, unexplained=unexplained,
                      pixel_share=masked_share, lines=lines if status == "changed" else [])
    all_rects = [r for r, _ in pixels.regions(grid, changed)]
    return _Screen(diff, st, all_rects)


def _not_comparable(key: str, sb: Shot | None, sa: Shot | None) -> str | None:
    """Why the two shots of a screen cannot be compared, or None when they can."""
    if sb is None or sa is None:
        side = "before" if sb is None else "after"
        return f"the {side} capture has no screen {key}: capture both with the same screens"
    for side, shot in (("before", sb), ("after", sa)):
        if not shot.ok or not shot.image or not shot.manifest:
            return f"the {side} screen failed to capture: {shot.error or 'no image was kept'}"
    if (sb.screen.height, sb.screen.scale) != (sa.screen.height, sa.screen.scale):
        return (f"the screens differ in size: before {sb.screen.width}x{sb.screen.height} at "
                f"{sb.screen.scale:g}x, after {sa.screen.width}x{sa.screen.height} at {sa.screen.scale:g}x")
    return None


def _unexplained(store: StudioStore, after_id: str, key: str, found: list, pngs: tuple[bytes, bytes], scale: float,
                 total: int, elements: list, *, crops: bool) -> tuple[list[Region], list[str]]:
    """The changed areas no element explains, each with a before and after crop when asked for,
    and the lines that say where they are."""
    png_b, png_a = pngs
    unexplained: list[Region] = []
    region_lines: list[str] = []
    for rect, n in found[:MAX_REGIONS]:
        evidence = None
        if crops:
            try:
                evidence = store.put_blob(pixels.side_by_side(png_b, png_a, rect, scale=scale), "image/png")
                store.attach(after_id, evidence)
            except AgentosError as e:
                log.info("studio diff: no crop for %s on %s: %s", rect, key, e)
        unexplained.append(Region(rect=rect, share=round(n / total, 6), evidence=evidence))
        el = _smallest_cover(rect, elements)
        where = f"inside {label(el)}" if el is not None else "outside every element"
        tail = f" (before and after side by side: {evidence})" if evidence else ""
        region_lines.append(f"Pixels changed in a {px(rect.w)}x{px(rect.h)} px area at "
                            f"{px(rect.x)},{px(rect.y)} {where}, and nothing in the "
                            f"layout, styles or text explains it{tail}.")
    if len(found) > MAX_REGIONS:
        region_lines.append(f"{len(found) - MAX_REGIONS} smaller changed areas are not listed.")
    return unexplained, region_lines


def _manifest_notes(side: str, m: Manifest) -> list[str]:
    out = []
    if m.omitted:
        out.append(f"Note: the {side} manifest stops at {m.cap} elements ({m.omitted} left out), so a change "
                   "further down is seen only in pixels.")
    if m.image_height is not None:
        out.append(f"Note: the {side} screenshot is cut at {px(m.image_height)} px of "
                   f"{px(m.page_height)}, so pixels below that were not compared.")
    return out


def _diff_id(before: str, after: str, tokens: TokenSet | None, mask: NoiseMask | None) -> str:
    h = hashlib.sha256()
    h.update(f"{before}\n{after}\n".encode())
    h.update((tokens.model_dump_json() if tokens else "").encode())
    h.update((mask.model_dump_json() if mask else "").encode())
    return "diff_" + h.hexdigest()[:12]


def _keys(before: Capture, after: Capture, only: list[str] | None) -> list[str]:
    keys = list(dict.fromkeys([s.screen.key for s in before.shots] + [s.screen.key for s in after.shots]))
    if only is None:
        return keys
    missing = [k for k in only if k not in keys]
    if missing:
        raise NotFound(f"neither capture has screen {missing[0][:60]!r}; they have: {', '.join(keys)}")
    return [k for k in keys if k in only]


def _run(store: StudioStore, before_id: str, after_id: str, *, tokens: TokenSet | None, mask: NoiseMask | None,
         screens: list[str] | None, crops: bool) -> tuple[DiffResult, dict[str, _Screen]]:
    before, after = store.get(before_id), store.get(after_id)
    done: dict[str, _Screen] = {}
    for key in _keys(before, after, screens):
        sm = mask.screens.get(key) if mask is not None else None
        done[key] = _screen(store, before, after, key, tokens=tokens, mask=sm, crops=crops)
    result = DiffResult(id=_diff_id(before_id, after_id, tokens, mask), before=before_id, after=after_id,
                        screens=[s.diff for s in done.values()])
    return result, done


def diff_captures(store: StudioStore, before_id: str, after_id: str, *, tokens: TokenSet | None = None,
                  mask: NoiseMask | None = None, screens: list[str] | None = None) -> DiffResult:
    """What changed from `before_id` to `after_id`, per screen, causes first. With `tokens`, a
    style change names the tokens before and after. With `mask` (from `noise_mask`), what
    changes on its own is left out. `screens` keeps to those screen keys. Synchronous and
    CPU bound: call it through asyncio.to_thread from the event loop."""
    return _run(store, before_id, after_id, tokens=tokens, mask=mask, screens=screens, crops=True)[0]


def noise_mask(store: StudioStore, capture_a: str, capture_b: str) -> NoiseMask:
    """What differs between two captures of the same unchanged state: per screen, the keys of the
    elements that changed and the areas whose pixels did. Screens skipped between the two get
    no mask."""
    _, done = _run(store, capture_a, capture_b, tokens=None, mask=None, screens=None, crops=False)
    out = NoiseMask(captures=[capture_a, capture_b])
    for key, s in done.items():
        if s.structure is None:
            continue
        keys: set[str] = set()
        rects: list[Rect] = list(s.changed_rects)
        for it in s.structure.items:
            for e in (it.b, it.a):
                if e is not None:
                    keys.add(e.key)
                    rects.append(e.rect)
        pad = 2.0
        rects = [Rect(x=r.x - pad, y=r.y - pad, w=r.w + 2 * pad, h=r.h + 2 * pad) for r in rects]
        out.screens[key] = ScreenMask(keys=sorted(keys), rects=_merge(rects))
    return out


def _merge(rects: list[Rect]) -> list[Rect]:
    """Overlapping rects as one, so a mask stays short."""
    out = list(rects)
    merged = True
    while merged:
        merged = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                r, o = out[i], out[j]
                if r.x <= o.x + o.w and o.x <= r.x + r.w and r.y <= o.y + o.h and o.y <= r.y + r.h:
                    out[i] = _rect_union(r, o)
                    del out[j]
                    merged = True
                    break
            if merged:
                break
    return sorted(out, key=lambda r: (r.y, r.x))

