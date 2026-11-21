"""What learning a brand reads from one rendered page.

- the authored values, through the tokens module's own reader (openajar.studio.tokens.page_js
  COLLECT): the same values system_check matches against tokens, read here to make tokens;
- which custom properties the page's CSS uses, and how often (rules and the elements they reach);
- component hints: `data-component` and `data-slot` values and repeated class names;
- a small signature of the page's colours, to tell whether a theme switch changed anything.

Only names and values leave the page: no text content, no attribute other than the two above.
"""

from __future__ import annotations

import contextlib
import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..tokens import page_js
from ..tokens.check import _family
from .cluster import Origin

INHERITED = {p for p, _c, inh in page_js.PROPS if inh}
# what is drafted; line heights, shadows and border widths are read but not drafted yet
DRAFTED = ("color", "space", "radius", "font_family", "font_size", "font_weight")

USAGE = r"""() => {
const refs = {}, els = {}, alias = {};
const RE = /var\(\s*(--[A-Za-z0-9_-]+)/g;
const roots = [document];
for (const el of document.querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
function decls(text, count) {
  for (const part of (text || '').split(';')) {
    const i = part.indexOf(':');
    if (i < 0 || part.indexOf('var(') < 0) continue;
    const prop = part.slice(0, i).trim();
    for (const m of part.slice(i + 1).matchAll(RE)) {
      const name = m[1];
      if (prop.startsWith('--')) { alias[name] = (alias[name] || 0) + 1; continue; }
      refs[name] = (refs[name] || 0) + 1;
      els[name] = (els[name] || 0) + count();
    }
  }
}
function visit(rules) {
  for (const r of rules) {
    if (r instanceof CSSStyleRule) {
      let n = null;
      decls(r.style.cssText, () => {
        if (n === null) { try { n = Math.min(document.querySelectorAll(r.selectorText).length, 100000); } catch (e) { n = 0; } }
        return n;
      });
      if (r.cssRules && r.cssRules.length) visit(r.cssRules);
    } else if (r.cssRules) visit(r.cssRules);
  }
}
for (const root of roots) {
  for (const sh of [...(root.styleSheets || []), ...(root.adoptedStyleSheets || [])]) {
    let rules; try { rules = sh.cssRules; } catch (e) { continue; }
    if (rules) visit(rules);
  }
}
for (const el of document.querySelectorAll('[style]')) decls(el.getAttribute('style'), () => 1);
return {refs, els, alias};
}"""

HINTS = r"""() => {
const comp = {}, slot = {}, cls = {};
const NAME = /^[A-Za-z][A-Za-z0-9_.:-]{0,59}$/;
for (const el of document.querySelectorAll('[data-component]')) {
  const v = (el.getAttribute('data-component') || '').trim(); if (NAME.test(v)) comp[v] = (comp[v] || 0) + 1;
}
for (const el of document.querySelectorAll('[data-slot]')) {
  const v = (el.getAttribute('data-slot') || '').trim(); if (NAME.test(v)) slot[v] = (slot[v] || 0) + 1;
}
const body = document.body || document.documentElement;
const svg = {};
for (const el of body.querySelectorAll('*')) {
  const inSvg = el.localName === 'svg' || !!el.closest('svg');
  for (const c of el.classList) {
    if (c.length > 80) continue;
    cls[c] = (cls[c] || 0) + 1;
    if (inSvg) svg[c] = (svg[c] || 0) + 1;
  }
}
const keep = Object.entries(cls).filter(([, n]) => n >= 2).sort((a, b) => b[1] - a[1]).slice(0, 400);
// A utility class: every rule that names it is that one class alone, with one or two declarations
// (Tailwind's @utility, a .tabular { font-variant-numeric: tabular-nums }). A component's class
// carries more, or is named in a selector with others (.card .title).
const want = new Set(keep.map(([c]) => c));
const solo = {}, mixed = new Set();
const visit = (rules) => {
  for (const r of rules) {
    if (r.selectorText) {
      for (const sel of r.selectorText.split(',')) {
        const names = [...sel.matchAll(/\.((?:\\.|[\w-])+)/g)].map((m) => m[1].replace(/\\/g, ''));
        const alone = /^\s*\.(?:\\.|[\w-])+(::?[\w-]+(\([^)]*\))?)*\s*$/.test(sel);
        for (const n of names) {
          if (!want.has(n)) continue;
          if (alone) solo[n] = Math.max(solo[n] || 0, r.style ? r.style.length : 0);
          else mixed.add(n);
        }
      }
    }
    if (r.cssRules) visit(r.cssRules);
  }
};
for (const sh of document.styleSheets) { try { visit(sh.cssRules); } catch (e) {} }
const util = {};
for (const [c, n] of Object.entries(solo)) if (n > 0 && n <= 2 && !mixed.has(c)) util[c] = 1;
const svgKeep = {};
for (const [c] of keep) if (svg[c]) svgKeep[c] = svg[c];
return {comp, slot, cls: Object.fromEntries(keep), svg: svgKeep, util};
}"""

SIGNATURE = r"""(vars) => {
const de = document.documentElement, b = document.body || de;
const cs = getComputedStyle(de), bs = getComputedStyle(b);
const v = {};
for (const n of vars) v[n] = cs.getPropertyValue(n).trim();
return {bg: [bs.backgroundColor, cs.backgroundColor], fg: bs.color, scheme: cs.colorScheme || '', vars: v};
}"""


@dataclass
class ScreenRead:
    page: int
    width: int
    theme: str
    origins: list[Origin] = field(default_factory=list)
    ua: int = 0                       # browser defaults seen (not part of any brand)
    truncated: bool = False


def _reach(records: list[dict]) -> dict[tuple[int, str], int]:
    """For each (record, inherited property) it writes: how many elements below inherit it."""
    children: dict[int, list[int]] = defaultdict(list)
    writes: dict[int, set[str]] = {}
    for r in records:
        if r.get("ps"):
            continue
        children[r["pi"]].append(r["i"])
        writes[r["i"]] = {p for p, *_ in r["v"] if p in INHERITED}
    out: dict[tuple[int, str], int] = {}
    for r in records:
        if r.get("ps"):
            continue
        for prop in writes.get(r["i"], ()):
            n = 0
            stack = list(children.get(r["i"], ()))
            while stack:
                c = stack.pop()
                if prop in writes.get(c, ()):
                    continue
                n += 1
                stack.extend(children.get(c, ()))
            out[(r["i"], prop)] = n
    return out


def origins_from(records: list[dict], page: int) -> tuple[list[Origin], int]:
    """The authored values of one screen, one per element, property family and value. Browser
    defaults are counted apart and left out."""
    reach = _reach(records)
    seen: set[tuple] = set()
    out: list[Origin] = []
    ua = 0
    for rec in records:
        for prop, cat, value, is_ua, _fs in rec["v"]:
            if cat not in DRAFTED:
                continue
            fam = _family(prop)
            key = (rec["i"], fam, value)
            if key in seen:
                continue
            seen.add(key)
            if is_ua:
                ua += 1
                continue
            out.append(Origin(loc=(page, rec["sel"], fam), prop=prop, category=cat, value=value, tag=rec["tag"],
                              reach=reach.get((rec["i"], prop), 0)))
    return out, ua


async def read_screen(page, page_index: int, width: int, theme: str, *, max_elements: int = 4000) -> ScreenRead:
    await page.evaluate(page_js.TRANSITIONS_OFF, True)
    try:
        data = await page.evaluate(page_js.COLLECT, {"max": max_elements, "components": []})
    finally:
        with contextlib.suppress(Exception):
            await page.evaluate(page_js.CLEANUP)
    origins, ua = origins_from(data["records"], page_index)
    return ScreenRead(page=page_index, width=width, theme=theme, origins=origins, ua=ua,
                      truncated=bool(data.get("truncated")))


async def read_usage(page) -> dict:
    return await page.evaluate(USAGE)


async def read_hints(page) -> dict:
    return await page.evaluate(HINTS)


async def read_signature(page, color_vars: list[str]) -> dict:
    return await page.evaluate(SIGNATURE, color_vars[:200])


# ---- component hints -----------------------------------------------------------------------

_UTILITY = re.compile(
    r"^-?(p|px|py|pt|pb|pl|pr|ps|pe|m|mx|my|mt|mb|ml|mr|ms|me|gap|gap-x|gap-y|space-x|space-y|w|h|min-w|min-h|max-w|"
    r"max-h|size|text|bg|border|rounded|flex|grid|col|cols|row|rows|items|justify|content|self|place|font|leading|"
    r"tracking|shadow|opacity|z|top|left|right|bottom|inset|overflow|order|basis|grow|shrink|ring|outline|fill|"
    r"stroke|decoration|underline|transition|duration|ease|delay|animate|cursor|select|pointer-events|sr|not-sr|"
    r"aspect|object|translate|rotate|scale|skew|origin|from|via|to|divide|truncate|whitespace|break|list|align|"
    r"float|clear|isolate|mix-blend|bg-blend|blur|backdrop|line-clamp|columns|container|sticky|fixed|absolute|"
    r"relative|static|block|inline|inline-block|inline-flex|hidden|visible|invisible|table|contents|flow-root|"
    r"is|has|js|u|l|o|t)(-|$)")
_PLAIN_WORDS = {"active", "disabled", "selected", "open", "show", "hide", "clearfix", "wrapper", "inner", "outer",
                "row", "col", "container", "dark", "light", "fade", "in", "out", "first", "last", "odd", "even"}
_MODULE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)_[A-Za-z0-9]+__[A-Za-z0-9_-]{4,}$")     # CSS modules: Card_root__a1b2c


def _pascal(s: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[-_]+", s) if p)


# Icon libraries put their own classes on every icon (lucide lucide-chevron-right, fa fa-user).
_ICON_LIBRARY = re.compile(
    r"^(lucide|fa|fas|far|fab|fal|fad|fa-solid|fa-regular|fa-brands|bi|mdi|ri|ph|ion|la|las|lab|octicon|feather|"
    r"heroicon|heroicons|tabler-icon|iconify|glyphicon|material-icons|material-symbols|icon|icons|svg-inline--fa)"
    r"(-|$)", re.IGNORECASE)


def class_roots(classes: dict[str, int], *, min_count: int = 3, limit: int = 12, svg: dict[str, int] | None = None,
                utility: dict[str, int] | set[str] | None = None) -> list[dict]:
    """Repeated class names that look like a component's own (card, card__title, card--wide; or a
    CSS module's Card_root__a1b2c), never utility classes (p-4, text-sm, flex, or a one-rule class
    the page's CSS defines, `utility`), and never an icon library's classes (lucide, fa-user, or a
    class found mostly on <svg>, `svg`). Heuristic."""
    svg = svg or {}
    utility = set(utility or ())
    roots: dict[str, dict] = {}
    for c, n in classes.items():
        if any(ch in c for ch in ":[]/.%#!@") or c in _PLAIN_WORDS or _UTILITY.match(c):
            continue
        if _ICON_LIBRARY.match(c) or svg.get(c, 0) * 2 >= n or c in utility:
            continue
        m = _MODULE.match(c)
        if m:
            root, how = m.group(1), "module"
        else:
            root = re.split(r"__|--", c, maxsplit=1)[0]
            how = "bem" if root != c else "plain"
        if len(root) < 3 or not re.match(r"^[A-Za-z][A-Za-z0-9-]*$", root) or _UTILITY.match(root.lower()):
            continue
        r = roots.setdefault(root, {"root": root, "count": 0, "parts": 0, "how": how})
        r["count"] = max(r["count"], n)
        if c != root:
            r["parts"] += 1
    out = []
    for r in roots.values():
        if r["count"] < min_count:
            continue
        if r["how"] == "module":
            pattern = f"^{re.escape(r['root'])}_"
        else:
            pattern = f"^{re.escape(r['root'])}($|__|--)"
        out.append({"name": _pascal(r["root"]), "class_pattern": pattern, "count": r["count"], "via": "class"})
    out.sort(key=lambda r: (-r["count"], r["name"]))
    return out[:limit]
