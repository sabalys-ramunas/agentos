"""The layout manifest: every visible element of a page with a stable key, its box and the style
properties a design system governs, read by one in-page script.

A key is the same on two loads of the same page, and never depends on the order things finished
loading or on generated ids:

- `testid=<value>` when the element has data-testid (or data-test-id);
- else `<anchor> > <role> "<name>"` for an element with a role and an accessible name (a
  landmark needs no name), where the anchor is the key of the nearest ancestor keyed this way;
- else `<anchor> > div[2]/p[1] ~"text hint"`: the DOM path from that anchor (or from body),
  each step the tag and its place among siblings of the same tag, with the first words of the
  element's own text, digits dropped so a clock or a counter does not move it.

A key that would repeat gets `#2`, `#3` in document order. The walk stops at a cap and says how
many elements it left out. The review browser is not a lane, so evaluating in the page's main
world is allowed here (docs/design/studio.md, decision 1).
"""

from __future__ import annotations

from typing import Any

from openajar.studio.contracts import ComponentRef, Element, Rect
from openajar.studio.store.models import Manifest

DEFAULT_CAP = 2000
TEXT_CAP = 80
NAME_CAP = 80

MANIFEST_JS = r"""
(opts) => {
  const CAP = opts.cap, TEXT_CAP = opts.text, NAME_CAP = opts.name;
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK', 'TITLE', 'BR', 'WBR', 'SOURCE', 'TRACK']);
  const LANDMARK = new Set(['banner', 'navigation', 'main', 'contentinfo', 'complementary', 'search', 'form',
                            'region', 'dialog', 'alertdialog', 'table', 'list', 'tablist', 'menu', 'menubar', 'toolbar']);
  const NAME_FROM_CONTENT = new Set(['button', 'link', 'heading', 'tab', 'menuitem', 'menuitemcheckbox',
                                     'menuitemradio', 'option', 'treeitem', 'switch', 'checkbox', 'radio', 'cell',
                                     'columnheader', 'rowheader', 'tooltip']);
  const NO_KEY_ROLES = new Set(['presentation', 'none', 'generic', 'listitem', 'row', 'cell', 'rowgroup', 'paragraph']);
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const cap = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
  const quote = (s) => s.replace(/\\/g, '\\\\').replace(/"/g, '\\"');

  const labelled = (el) => !!(norm(el.getAttribute('aria-label')) || el.getAttribute('aria-labelledby'));
  const roleOf = (el) => {
    const explicit = norm(el.getAttribute('role'));
    if (explicit) return explicit.split(' ')[0].toLowerCase();
    const t = el.tagName;
    switch (t) {
      case 'A': case 'AREA': return el.hasAttribute('href') ? 'link' : null;
      case 'BUTTON': case 'SUMMARY': return 'button';
      case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6': return 'heading';
      case 'NAV': return 'navigation';
      case 'MAIN': return 'main';
      case 'HEADER': return el.parentElement && el.parentElement.closest('article,aside,main,nav,section') ? null : 'banner';
      case 'FOOTER': return el.parentElement && el.parentElement.closest('article,aside,main,nav,section') ? null : 'contentinfo';
      case 'ASIDE': return 'complementary';
      case 'FORM': return labelled(el) ? 'form' : null;
      case 'SECTION': return labelled(el) ? 'region' : null;
      case 'SEARCH': return 'search';
      case 'DIALOG': return 'dialog';
      case 'ARTICLE': return 'article';
      case 'IMG': return el.getAttribute('alt') === '' ? 'presentation' : 'img';
      case 'SELECT': return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'OPTION': return 'option';
      case 'UL': case 'OL': case 'MENU': return 'list';
      case 'LI': return 'listitem';
      case 'TABLE': return 'table';
      case 'TR': return 'row';
      case 'TH': return 'columnheader';
      case 'TD': return 'cell';
      case 'P': return 'paragraph';
      case 'INPUT': {
        const type = (el.getAttribute('type') || 'text').toLowerCase();
        if (type === 'hidden') return null;
        if (type === 'checkbox' || type === 'radio') return type;
        if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
        if (type === 'range') return 'slider';
        if (type === 'search') return 'searchbox';
        if (type === 'number') return 'spinbutton';
        return 'textbox';
      }
      default: return null;
    }
  };
  const nameOf = (el, role) => {
    let n = norm(el.getAttribute('aria-label'));
    if (n) return n;
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      n = norm(by.split(/\s+/).map((id) => { const x = document.getElementById(id); return x ? x.textContent : ''; }).join(' '));
      if (n) return n;
    }
    const t = el.tagName;
    if (t === 'IMG' || (t === 'INPUT' && el.type === 'image')) { n = norm(el.getAttribute('alt')); if (n) return n; }
    if (t === 'INPUT' || t === 'SELECT' || t === 'TEXTAREA') {
      if (el.labels && el.labels.length) { n = norm(Array.from(el.labels).map((l) => l.textContent).join(' ')); if (n) return n; }
      if (['button', 'submit', 'reset'].includes(el.type)) { n = norm(el.value); if (n) return n; }
      n = norm(el.getAttribute('placeholder')); if (n) return n;
    }
    if (role && NAME_FROM_CONTENT.has(role)) { n = norm(el.textContent); if (n) return n; }
    return norm(el.getAttribute('title'));
  };
  const ownText = (el) => {
    let s = '';
    for (const c of el.childNodes) if (c.nodeType === 3) s += c.nodeValue;
    return norm(s);
  };
  const four = (a, b, c, d) => (a === b && b === c && c === d ? a : (a === c && b === d ? a + ' ' + b : [a, b, c, d].join(' ')));
  const round = (v) => Math.round(v * 10) / 10;

  const testidOf = (el) => norm(el.getAttribute('data-testid') || el.getAttribute('data-test-id'));
  const seen = new Map();
  const unique = (key) => {
    const n = (seen.get(key) || 0) + 1;
    seen.set(key, n);
    return n === 1 ? key : key + ' #' + n;
  };

  const sx = window.scrollX, sy = window.scrollY;
  const body = document.body;
  const out = [];
  let omitted = 0;
  if (!body) return { elements: out, total: 0, omitted: 0, width: 0, height: 0 };
  const total = body.getElementsByTagName('*').length;
  // stack entries: [element, parent key, anchor key, path from the anchor, faded (opacity 0 above)]
  const stack = [];
  const pushChildren = (el, parent, anchor, rel, faded) => {
    const counts = new Map();
    const kids = [];
    for (const c of el.children) {
      const tag = c.tagName.toLowerCase();
      const i = (counts.get(tag) || 0) + 1;
      counts.set(tag, i);
      kids.push([c, parent, anchor, (rel ? rel + '/' : '') + tag + '[' + i + ']', faded]);
    }
    for (let k = kids.length - 1; k >= 0; k--) stack.push(kids[k]);
  };
  pushChildren(body, null, '', '', false);
  while (stack.length) {
    const [el, parent, anchor, rel, fadedAbove] = stack.pop();
    if (SKIP.has(el.tagName)) continue;
    if (out.length >= CAP) {
      omitted += 1 + el.getElementsByTagName('*').length;
      continue;
    }
    const cs = getComputedStyle(el);
    if (cs.display === 'none') continue;
    const faded = fadedAbove || parseFloat(cs.opacity) === 0;
    const r = el.getBoundingClientRect();
    const visible = cs.display !== 'contents' && r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && !faded;
    const isSvg = el.namespaceURI === 'http://www.w3.org/2000/svg';
    if (!visible) {
      if (!isSvg) pushChildren(el, parent, anchor, rel, faded);
      continue;
    }
    const role = roleOf(el);
    const name = cap(nameOf(el, role), NAME_CAP);
    const text = cap(ownText(el), TEXT_CAP);
    const tid = testidOf(el);
    let key, anchored = false;
    if (tid) {
      key = 'testid=' + cap(tid, 80); anchored = true;
    } else if (role && !NO_KEY_ROLES.has(role) && (name || LANDMARK.has(role))) {
      key = (anchor ? anchor + ' > ' : '') + role + (name ? ' "' + quote(cap(name, 40)) + '"' : ''); anchored = true;
    } else {
      // a word with a digit in it is a count, a time or an id (a uuid, a hash): never part of the key
      const hint = norm(text.replace(/\S*[0-9]\S*/g, ' ')).slice(0, 24);
      key = (anchor ? anchor + ' > ' : '') + rel + (hint ? ' ~"' + quote(hint) + '"' : '');
    }
    key = unique(key);

    const styles = {};
    const put = (k, v, dflt) => { if (v && !dflt.includes(v)) styles[k] = v; };
    put('background-color', cs.backgroundColor, ['rgba(0, 0, 0, 0)', 'transparent']);
    put('padding', four(cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft), ['0px']);
    put('margin', four(cs.marginTop, cs.marginRight, cs.marginBottom, cs.marginLeft), ['0px']);
    put('gap', cs.rowGap === cs.columnGap ? cs.rowGap : cs.rowGap + ' ' + cs.columnGap, ['normal', '0px']);
    put('border-radius', four(cs.borderTopLeftRadius, cs.borderTopRightRadius, cs.borderBottomRightRadius,
                              cs.borderBottomLeftRadius), ['0px']);
    put('border-width', four(cs.borderTopWidth, cs.borderRightWidth, cs.borderBottomWidth, cs.borderLeftWidth), ['0px']);
    put('box-shadow', cs.boxShadow, ['none']);
    const control = ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(el.tagName);
    if (text || control) {
      styles['color'] = cs.color;
      styles['font-family'] = cs.fontFamily;
      styles['font-size'] = cs.fontSize;
      styles['font-weight'] = cs.fontWeight;
      styles['line-height'] = cs.lineHeight;
      put('letter-spacing', cs.letterSpacing, ['normal', '0px']);
    }
    const comp = norm(el.getAttribute('data-component'));
    out.push({
      key, tag: el.tagName.toLowerCase(), role: role || null, name: name || null, text: text || null,
      rect: { x: round(r.left + sx), y: round(r.top + sy), w: round(r.width), h: round(r.height) },
      styles, parent,
      component: comp ? { name: cap(comp, 120), source: norm(el.getAttribute('data-source')).slice(0, 300) || null, via: 'attr' } : null,
    });
    if (!isSvg) pushChildren(el, key, anchored ? key : anchor, anchored ? '' : rel, faded);
  }
  const de = document.documentElement;
  return { elements: out, total, omitted,
           width: Math.max(de.scrollWidth, body.scrollWidth), height: Math.max(de.scrollHeight, body.scrollHeight) };
}
"""


def _element(raw: dict[str, Any]) -> Element:
    comp = raw.get("component")
    return Element(key=str(raw["key"]), tag=str(raw["tag"]), role=raw.get("role"), name=raw.get("name"),
                   text=raw.get("text"), rect=Rect(**raw["rect"]),
                   styles={str(k): str(v) for k, v in (raw.get("styles") or {}).items()},
                   component=ComponentRef(**comp) if comp else None, parent=raw.get("parent"))


async def read_manifest(page: Any, *, cap: int = DEFAULT_CAP) -> Manifest:
    """The whole manifest of a settled page, with the counts of what the cap left out."""
    cap = max(1, int(cap))
    raw = await page.evaluate(MANIFEST_JS, {"cap": cap, "text": TEXT_CAP, "name": NAME_CAP})
    return Manifest(elements=[_element(e) for e in raw.get("elements", [])], total=int(raw.get("total", 0)),
                    omitted=int(raw.get("omitted", 0)), cap=cap, page_width=float(raw.get("width", 0)),
                    page_height=float(raw.get("height", 0)))


async def build_manifest(page: Any, *, cap: int = DEFAULT_CAP) -> list[Element]:
    """The visible elements of a settled page, in document order (contract S2)."""
    return (await read_manifest(page, cap=cap)).elements
