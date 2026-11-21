"""The provenance probe: does a value come from a token, or is it a hard-coded look-alike?

Computed styles lose var(), so a value alone cannot say. The probe sets each token's custom
property to a unique sentinel where the property is declared, reads the computed styles again,
and compares: a value that became a sentinel names its token; one that moved some other way
follows a token through calc() or color-mix(); one that did not move is written in by hand (or
inlined at build time, which the probe cannot tell apart, see `inconclusive`).

One category at a time, transitions off, always restored, with a time cap, and a check that the
page came back to its first state.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..contracts import Theme, TokenCategory, TokenSet
from . import page_js
from .units import parse_length, parse_number

C = TokenCategory
BATCH = 240
PROPS_BY_CAT: dict[str, list[str]] = {}
for _p, _c, _ in page_js.PROPS:
    PROPS_BY_CAT.setdefault(_c, []).append(_p)
INHERITED = {p for p, _, inh in page_js.PROPS if inh}
ALL_PROPS = [p for p, _, _ in page_js.PROPS]

# Sentinels. Colours put the token index in the green and blue channels behind a red of 7;
# lengths are whole px far above anything a page uses (borders snap to whole px); weights are
# small numbers no font uses; families are names no font has.
_SENT_RGB = re.compile(r"rgba?\(\s*7,\s*(11[3-6]),\s*(\d{1,3})(?:,\s*([\d.]+))?\s*\)")
_SENT_FAMILY = re.compile(r"openajar-tok-(\d+)")
_KS = sorted({k / 4 for k in range(1, 17)} | {float(k) for k in range(1, 101)} | {k + 0.5 for k in range(1, 50)})


def color_sentinel(i: int) -> str:
    return f"rgb(7, {113 + i // 256}, {i % 256})"


def length_sentinel(i: int) -> float:
    return 3001.0 + 7 * i


def number_sentinel(i: int) -> float:
    return 301.0 + i


def family_sentinel(i: int) -> str:
    return f'"openajar-tok-{i}"'


def weight_sentinel(i: int) -> int:
    n, k = 0, 0
    while True:
        k += 1
        if k % 50:
            if n == i:
                return k
            n += 1


@dataclass
class Prov:
    kind: str                      # token | derived | fixed
    token: str | None = None
    factor: float | None = None    # derived: k times the token


@dataclass
class ProbeResult:
    ran: dict[str, str] = field(default_factory=dict)          # category -> "probe" | why not
    prov: dict[tuple[int, str], Prov] = field(default_factory=dict)
    hidden: list[tuple[int, str, str]] = field(default_factory=list)   # (record, prop, base value)
    moved: dict[str, int] = field(default_factory=dict)
    base_rows: list[dict] | None = None
    notes: list[str] = field(default_factory=list)
    reloaded: bool = False
    defined: int = 0
    with_var: int = 0


def _decode(cat: str, prop: str, new: str, plan_vals: dict, row: dict, fs: float) -> Prov | None:
    """The token a moved value names, or None when it moved without naming one."""
    if cat == "color":
        m = _SENT_RGB.search(new)
        if m:
            idx = (int(m.group(1)) - 113) * 256 + int(m.group(2))
            return Prov("token", plan_vals.get(("c", idx)))
        return None
    if cat == "shadow":
        m = _SENT_RGB.search(new)
        return Prov("token", plan_vals.get(("c", (int(m.group(1)) - 113) * 256 + int(m.group(2))))) if m else None
    if cat == "font_family":
        m = _SENT_FAMILY.search(new)
        return Prov("token", plan_vals.get(("f", int(m.group(1))))) if m else None
    if cat == "font_weight":
        n = parse_number(new)
        return Prov("token", plan_vals[("w", int(n))]) if n is not None and ("w", int(n)) in plan_vals else None
    if cat == "line_height":
        n = parse_number(new)
        if n is None:
            px = parse_length(new)
            if px is not None and fs:
                for (kind, s), name in plan_vals.items():
                    if kind == "n" and abs(px / fs - s) < 1e-3:
                        return Prov("token", name)
        else:
            if ("n", n) in plan_vals:
                return Prov("token", plan_vals[("n", n)])
        # fall through to px sentinels
    px = parse_length(new)
    if px is None:
        return None
    lens = [(s, name) for (kind, s), name in plan_vals.items() if kind == "l"]
    for s, name in lens:
        if abs(px - s) < 0.02:
            return Prov("token", name)
    hits = []
    for s, name in lens:
        k = px / s
        for cand in _KS:
            if abs(abs(k) - cand) < 1e-4:
                hits.append((name, k))
    if len(hits) == 1:
        return Prov("derived", hits[0][0], round(hits[0][1], 4))
    return None


def _plan(cat: str, toks: list[tuple[str, str, str]]) -> tuple[dict[str, str], dict]:
    """toks: (css_var, token name, token value). Returns {var: sentinel} and the decode table."""
    plan, table = {}, {}
    for i, (var, name, value) in enumerate(toks):
        if cat in ("color",):
            plan[var] = color_sentinel(i)
            table[("c", i)] = name
        elif cat == "shadow":
            plan[var] = f"0px 0px 0px 1px {color_sentinel(i)}"
            table[("c", i)] = name
        elif cat == "font_family":
            plan[var] = family_sentinel(i)
            table[("f", i)] = name
        elif cat == "font_weight":
            w = weight_sentinel(i)
            plan[var] = str(w)
            table[("w", w)] = name
        elif cat == "line_height" and parse_number(value) is not None:
            n = number_sentinel(i)
            plan[var] = f"{n:g}"
            table[("n", n)] = name
        else:
            s = length_sentinel(i)
            plan[var] = f"{s:g}px"
            table[("l", s)] = name
    return plan, table


async def _eval(page, script: str, arg: Any, deadline: float):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError
    if arg is None:
        return await asyncio.wait_for(page.evaluate(script), left)
    return await asyncio.wait_for(page.evaluate(script, arg), left)


def _same_rows(a: list[dict], b: list[dict]) -> int:
    return sum(1 for x, y in zip(a, b, strict=False) if x != y) + abs(len(a) - len(b))


async def run_probe(page, records: list[dict], tokens: TokenSet, theme: Theme | None,
                    categories: list[str], *, timeout_s: float = 20.0) -> ProbeResult:
    """Probe each category in turn. `records` is the COLLECT result; the page must still hold
    its element list."""
    res = ProbeResult()
    deadline = time.monotonic() + timeout_s
    by_var = _vars_by_category(tokens, theme)
    all_vars = sorted({v for d in by_var.values() for v in d})
    res.with_var = len(all_vars)
    if not all_vars:
        for c in categories:
            res.ran[c] = "the tokens name no CSS custom properties"
        return res
    try:
        defs = await _eval(page, page_js.DEFS, all_vars, deadline)
        defined = set(defs["defined"])
        res.defined = len(defined)
        if defs["unreadable"]:
            res.notes.append(f"{len(defs['unreadable'])} stylesheet(s) could not be read (cross-origin); "
                             "tokens declared only there were set on the root element instead")
        first = await _eval(page, page_js.REREAD, ALL_PROPS, deadline)
    except TimeoutError:
        for c in categories:
            res.ran[c] = "the probe ran out of time before it started"
        return res
    res.base_rows = first
    root_px = first[0].get("font-size") if first else None
    counted = {(r["i"], v[0]) for r in records for v in r["v"]}
    try:
        for cat in categories:
            toks = [(var, nv[0], nv[1]) for var, nv in sorted(by_var.get(cat, {}).items()) if var in defined]
            if not toks:
                res.ran[cat] = ("none of its tokens is declared on this page as a custom property"
                                if by_var.get(cat) else "no token in this category names a custom property")
                continue
            news, plans = await _move_category(page, cat, toks, defs["decls"], first, root_px, deadline)
            res.moved[cat] = _judge_category(res, cat, records, first, news, plans, counted)
            res.ran[cat] = "probe"
    except TimeoutError:
        for cat in categories:
            res.ran.setdefault(cat, f"the probe stopped at its time cap ({timeout_s:g}s)")
        res.notes.append(f"the probe stopped at its time cap ({timeout_s:g}s)")
        with contextlib.suppress(Exception):   # the came-back check below reloads the page if needed
            await asyncio.wait_for(page.evaluate(page_js.RESTORE), 5)
    await _bring_back(page, res, first)
    return res


def _vars_by_category(tokens: TokenSet, theme: Theme | None) -> dict[str, dict[str, tuple[str, str]]]:
    """{category: {css var: (token name, value)}}, the first token naming a var winning."""
    by_var: dict[str, dict[str, tuple[str, str]]] = {}
    for t in tokens.tokens:
        if t.css_var and (theme is None or theme in t.themes):
            by_var.setdefault(t.category.value, {}).setdefault(t.css_var, (t.name, t.value))
    return by_var


async def _move_category(page, cat: str, toks: list[tuple[str, str, str]], decls: Any, first: list[dict],
                         root_px: Any, deadline: float) -> tuple[list[dict], dict]:
    """Set the category's tokens to sentinels, batch by batch, and read every element again.
    Returns what each element shows with the sentinels on, and the table that decodes them."""
    props = PROPS_BY_CAT[cat]
    news: list[dict] = []
    plans: dict = {}
    for start in range(0, len(toks), BATCH):
        plan, table = _plan(cat, toks[start:start + BATCH])
        plans.update(table)
        rows = await _read_with(page, cat, plan, decls, props, root_px, deadline)
        if not news:
            news = rows
            continue
        # a later batch only adds what an earlier one did not already move
        for k, row in enumerate(rows):
            for p in props:
                if row.get(p) != first[k].get(p):
                    news[k][p] = row[p]
    return news, plans


async def _read_with(page, cat: str, plan: dict, decls: Any, props: list[str], root_px: Any,
                     deadline: float) -> list[dict]:
    """Apply one batch of sentinels, read the rows, restore."""
    if cat != "font_size":
        await _eval(page, page_js.APPLY, [plan, decls, None], deadline)
        rows = await _eval(page, page_js.REREAD, props, deadline)
        await _eval(page, page_js.RESTORE, None, deadline)
        return rows
    # the root's own font size first, unpinned; then the rest with the root pinned so rem lengths
    # hold still
    await _eval(page, page_js.APPLY, [plan, decls, None], deadline)
    root_row = (await _eval(page, page_js.REREAD, props, deadline))[:1]
    await _eval(page, page_js.RESTORE, None, deadline)
    await _eval(page, page_js.APPLY, [plan, decls, root_px], deadline)
    rows = await _eval(page, page_js.REREAD, props, deadline)
    await _eval(page, page_js.RESTORE, None, deadline)
    if rows and root_row:
        rows[0] = root_row[0]
    return rows


def _judge_category(res: ProbeResult, cat: str, records: list[dict], first: list[dict], news: list[dict],
                    plans: dict, counted: set[tuple[int, str]]) -> int:
    """Each element's values in the category: fixed, or moved to the token it names (or derived
    from one). Records the provenance in `res`; returns how many moved."""
    props = PROPS_BY_CAT[cat]
    moved = 0
    for rec in records:
        i = rec["i"]
        if i >= len(news):
            continue
        pi = rec["pi"]
        fs = parse_length(first[i].get("font-size") or "16px") or 16.0
        for p in props:
            base, new = first[i].get(p), news[i].get(p)
            if (i, p) not in counted:
                # an inherited value equal to its parent's can still be written on this
                # element: the probe shows it when the parent moves and it does not
                if p not in INHERITED or pi < 0 or base is None:
                    continue
                pbase, pnew = first[pi].get(p), news[pi].get(p)
                if base != pbase or new == pnew:
                    continue
                res.hidden.append((i, p, base))
            prov = _provenance(cat, p, base, new, plans, news[i], fs, first[pi] if pi >= 0 else None,
                               news[pi] if pi >= 0 else None)
            res.prov[(i, p)] = prov
            if prov.kind != "fixed":
                moved += 1
    return moved


def _provenance(cat: str, p: str, base: Any, new: Any, plans: dict, row: dict, fs: float,
                parent_first: dict | None, parent_new: dict | None) -> Prov:
    if base == new:
        return Prov("fixed")
    prov = _decode(cat, p, new, plans, row, fs)
    if prov is None and cat == "font_size" and parent_first is not None and parent_new is not None:
        # an em or % size under a parent that moved: relative, not a token
        pb, pn = parse_length(parent_first.get(p) or ""), parse_length(parent_new.get(p) or "")
        b, n = parse_length(base or ""), parse_length(new or "")
        if pb and pn and b and n and pb != pn and abs(n / b - pn / pb) < 1e-3:
            return Prov("fixed")
    return prov or Prov("derived")


async def _bring_back(page, res: ProbeResult, first: list[dict]) -> None:
    """Did the page come back to its first state? If not, reload it, and say so."""
    try:
        await asyncio.wait_for(page.evaluate(page_js.RESTORE), 5)
        after = await asyncio.wait_for(page.evaluate(page_js.REREAD, ALL_PROPS), 10)
        diff = _same_rows(first, after)
    except Exception:  # noqa: BLE001 - an unreadable page counts as not back
        diff = -1
    if diff != 0:
        res.notes.append("the page did not come back to its first state after the probe"
                         + (f" ({diff} element(s) differ)" if diff > 0 else "") + ", so it was reloaded")
        res.reloaded = True
        try:
            await page.reload(wait_until="load")
        except Exception as e:  # noqa: BLE001 - said in the coverage
            res.notes.append(f"the reload failed: {e}")
