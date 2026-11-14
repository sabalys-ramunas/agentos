"""The scripts studio runs in a page to read authored values, token declarations and to
perturb custom properties for the provenance probe. They run in the page's main world (a
review browser, not a lane: docs/design/studio.md, decision 1). State lives on one window
property and is removed by CLEANUP."""

# The properties checked, their category and whether they inherit.
PROPS = [
    ("color", "color", True),
    ("font-family", "font_family", True),
    ("font-size", "font_size", True),
    ("font-weight", "font_weight", True),
    ("line-height", "line_height", True),
    ("background-color", "color", False),
    ("border-top-color", "color", False), ("border-right-color", "color", False),
    ("border-bottom-color", "color", False), ("border-left-color", "color", False),
    ("border-top-width", "border_width", False), ("border-right-width", "border_width", False),
    ("border-bottom-width", "border_width", False), ("border-left-width", "border_width", False),
    ("padding-top", "space", False), ("padding-right", "space", False),
    ("padding-bottom", "space", False), ("padding-left", "space", False),
    ("margin-top", "space", False), ("margin-right", "space", False),
    ("margin-bottom", "space", False), ("margin-left", "space", False),
    ("row-gap", "space", False), ("column-gap", "space", False),
    ("border-top-left-radius", "radius", False), ("border-top-right-radius", "radius", False),
    ("border-bottom-right-radius", "radius", False), ("border-bottom-left-radius", "radius", False),
    ("box-shadow", "shadow", False),
]

_COMMON = r"""
const ST_KEY = '__agentosStudioTokens';
const PROPS = %PROPS%;
const SIDES = ['top', 'right', 'bottom', 'left'];
const CLEAR = /^(transparent|rgba\(\s*[\d.]+,\s*[\d.]+,\s*[\d.]+,\s*0\)|.*\/\s*0\))$/;
function rawValue(cs, typed, prop) {
  if (prop === 'line-height' && typed) {
    try { const v = typed.get('line-height'); if (v) return String(v); } catch (e) {}
  }
  if (prop.startsWith('border-') && prop.endsWith('-radius')) {
    return (cs.getPropertyValue(prop) || '').split(' ')[0];
  }
  return cs.getPropertyValue(prop);
}
function marginAuto(typed, prop) {
  if (!typed) return null;
  try { const v = typed.get(prop); return v && String(v) === 'auto'; } catch (e) { return null; }
}
"""

_COMMON = _COMMON.replace("%PROPS%", repr([list(p) for p in PROPS]).replace("True", "true").replace("False", "false"))

COLLECT = "async (opts) => {" + _COMMON + r"""
const MAX = opts.max || 4000;
// Left out: pictures of another page (role=img, inert) and regions marked as another product
// (data-studio="ignore", or the caller's `ignore` selectors). Their values are not this design system.
const OUT = ['[role=img]', '[inert]', '[data-studio="ignore"]'];
for (const q of opts.ignore || []) { try { document.querySelector(q); OUT.push(q); } catch (e) {} }
const OUT_SEL = OUT.join(',');
let leftOut = 0;
const SKIP = new Set(['SCRIPT','STYLE','LINK','META','HEAD','TITLE','NOSCRIPT','TEMPLATE','BR','WBR',
  'SOURCE','TRACK','PARAM','BASE','AREA','MAP','DATALIST','OPTION','OPTGROUP']);
const INTERACTIVE = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],' +
  '[role=checkbox],[role=radio],[role=switch],[role=tab],[role=menuitem],[role=menuitemcheckbox],[role=option],' +
  '[role=combobox],[role=textbox],[role=slider],[role=spinbutton],[role=searchbox],[contenteditable=""],' +
  '[contenteditable=true],[tabindex]:not([tabindex="-1"])';
const old = window[ST_KEY];
if (old && old.frame) old.frame.remove();
const st = window[ST_KEY] = {els: [], roots: [document], frame: null, uaCache: new Map()};
const rules = (opts.components || []).map(r => {
  let attrName = null, attrVal = null, re = null;
  if (r.attr) {
    const i = r.attr.indexOf('=');
    if (i >= 0) { attrName = r.attr.slice(0, i).trim(); attrVal = r.attr.slice(i + 1).trim().replace(/^["']|["']$/g, ''); }
    else attrName = r.attr.trim();
  }
  if (r.class_pattern) { try { re = new RegExp(r.class_pattern); } catch (e) { re = null; } }
  return {name: r.name, attrName, attrVal, re};
});
function compOf(el) {
  for (const r of rules) {
    if (r.attrName && el.hasAttribute(r.attrName)) {
      const v = el.getAttribute(r.attrName);
      if (r.attrVal === null || v === r.attrVal) return {name: r.name === '*' ? v : r.name, via: 'attr'};
    }
    if (r.re && el.classList) for (const c of el.classList) if (r.re.test(c)) return {name: r.name, via: 'class'};
  }
  return null;
}
function cssId(s) { return /^[A-Za-z_][\w-]*$/.test(s); }
function selOf(el) {
  const parts = [];
  let e = el;
  while (e && e.nodeType === 1) {
    if (e.id && cssId(e.id)) { parts.unshift('#' + e.id); break; }
    let p = e.localName;
    const cls = [...e.classList].filter(c => cssId(c) && !/\d{4,}/.test(c)).slice(0, 2);
    if (cls.length) p += '.' + cls.join('.');
    const par = e.parentElement;
    if (par && !cls.length) {
      const sib = [...par.children].filter(s => s.localName === e.localName);
      if (sib.length > 1) p += ':nth-of-type(' + (sib.indexOf(e) + 1) + ')';
    }
    parts.unshift(p);
    if (!par) {
      const root = e.getRootNode();
      if (root && root.host) return selOf(root.host) + ' >> ' + parts.join(' > ');
      break;
    }
    if (parts.length >= 5) break;
    e = par;
  }
  return parts.join(' > ');
}
function uaFrame() {
  if (st.frame) return st.frame.contentDocument;
  const f = document.createElement('iframe');
  f.setAttribute('aria-hidden', 'true');
  f.style.cssText = 'position:absolute!important;left:-99999px!important;top:0!important;width:10px!important;height:10px!important;border:0!important;visibility:hidden!important';
  document.documentElement.appendChild(f);
  st.frame = f;
  // the browser's defaults in the page's colour scheme: in dark, the root's CanvasText is white
  try { f.contentDocument.documentElement.style.colorScheme = getComputedStyle(document.documentElement).colorScheme; } catch (e) {}
  return f.contentDocument;
}
// What the browser alone gives this tag: its computed style in a blank document, at the same
// font size (margins such as an h2's are in em).
function uaStyle(el, fontSize) {
  const tag = el.localName;
  const type = el.getAttribute('type') || '';
  const href = el.hasAttribute('href') ? '1' : '';
  const key = tag + '|' + type + '|' + href + '|' + fontSize;
  if (st.uaCache.has(key)) return st.uaCache.get(key);
  const doc = uaFrame();
  let out = null;
  try {
    const plain = doc.createElement(tag);
    if (type) plain.setAttribute('type', type);
    if (href) plain.setAttribute('href', '#');
    const sized = plain.cloneNode();
    sized.style.fontSize = fontSize;
    doc.body.appendChild(plain); doc.body.appendChild(sized);
    const a = doc.defaultView.getComputedStyle(plain), b = doc.defaultView.getComputedStyle(sized);
    const body = doc.defaultView.getComputedStyle(doc.body);
    out = {};
    for (const [p, , inh] of PROPS) {
      // an inherited property is a browser default only where the browser's own sheet sets it
      // for this tag (a link's colour, a heading's size), not where it merely inherits
      if (inh) out[p] = (p !== 'line-height' && a.getPropertyValue(p) !== body.getPropertyValue(p)) ? a.getPropertyValue(p) : undefined;
      else out[p] = rawValue(b, null, p);
    }
    plain.remove(); sized.remove();
  } catch (e) { out = null; }
  st.uaCache.set(key, out);
  return out;
}
let rootUA = null;
function rootStyle() {
  if (rootUA) return rootUA;
  const doc = uaFrame();
  const cs = doc.defaultView.getComputedStyle(doc.documentElement);
  rootUA = {};
  for (const [p] of PROPS) rootUA[p] = rawValue(cs, null, p);
  return rootUA;
}
const typedOK = typeof Element.prototype.computedStyleMap === 'function';
function values(el, pseudo, cs, pcs, isRoot) {
  const typed = (!pseudo && typedOK) ? el.computedStyleMap() : null;
  const fs = parseFloat(cs.fontSize) || 16;
  const out = [];
  let ua = null;
  const uaOf = () => { if (ua === null) ua = pseudo ? {} : (uaStyle(el, cs.fontSize) || {}); return ua; };
  for (const [prop, cat, inh] of PROPS) {
    let v = rawValue(cs, typed, prop);
    if (v === '' || v == null) continue;
    // a colour that paints nothing (transparent text behind a busy spinner, a clear border) is no choice
    if (cat === 'color' && CLEAR.test(v)) continue;
    let counted = false;
    if (inh) {
      if (pcs) {
        const pv = rawValue(pcs, null, prop);
        if (prop === 'line-height') {
          if (typed && parentTypedLH !== null) counted = v !== parentTypedLH;
          else {
            // without typed values: the same px, or the same ratio to the font size, is inherited
            const pfs = parseFloat(pcs.fontSize) || 16;
            const px = parseFloat(cs.lineHeight), ppx = parseFloat(pcs.lineHeight);
            counted = !(cs.lineHeight === pcs.lineHeight ||
                        (!isNaN(px) && !isNaN(ppx) && Math.abs(px / fs - ppx / pfs) < 1e-3));
            if (!typed && !isNaN(px)) v = String(Math.round(px * 1000) / 1000) + 'px';
          }
        } else counted = v !== pv;
      } else {
        counted = v !== rootStyle()[prop];
      }
    } else if (cat === 'color') {
      if (prop === 'background-color') counted = !CLEAR.test(v);
      else {
        // a transparent border keeps the box's size without a colour: not a colour choice
        const side = prop.split('-')[1];
        const style = cs.getPropertyValue('border-' + side + '-style');
        counted = style !== 'none' && style !== 'hidden' && parseFloat(cs.getPropertyValue('border-' + side + '-width')) > 0
                  && v !== cs.color && !CLEAR.test(v);
      }
    } else if (cat === 'border_width') {
      const side = prop.split('-')[1];
      const style = cs.getPropertyValue('border-' + side + '-style');
      counted = style !== 'none' && style !== 'hidden' && parseFloat(v) > 0;
    } else if (prop.startsWith('margin-')) {
      const auto = marginAuto(typed, prop);
      counted = parseFloat(v) !== 0 && auto !== true;
      if (auto === null && !pseudo) {
        // no typed values here: equal left and right margins are taken as centring
        if ((prop === 'margin-left' || prop === 'margin-right') && cs.marginLeft === cs.marginRight) counted = false;
      }
    } else if (cat === 'space') {
      counted = v !== 'normal' && parseFloat(v) !== 0;
    } else if (cat === 'radius') {
      counted = parseFloat(v) !== 0;
    } else if (cat === 'shadow') {
      counted = v !== 'none';
    }
    if (!counted) continue;
    const u = uaOf();
    const isUA = !!(u && u[prop] !== undefined && u[prop] === v && (inh ? !isRoot : true));
    out.push([prop, cat, v, isUA, fs]);
  }
  return out;
}
let parentTypedLH = null;
function typedLH(el) {
  if (!typedOK || !el || el.nodeType !== 1) return null;
  try { const v = el.computedStyleMap().get('line-height'); return v ? String(v) : null; } catch (e) { return null; }
}
const records = [];
let truncated = false, total = 0;
const W = Math.max(document.documentElement.scrollWidth, innerWidth);
const H = Math.max(document.documentElement.scrollHeight, innerHeight);
const cell = Math.max(4, Math.ceil(Math.sqrt(W * H / 2e6)));
const gw = Math.ceil(W / cell), gh = Math.ceil(H / cell);
const content = new Uint8Array(gw * gh), ds = new Uint8Array(gw * gh);
function paint(grid, r) {
  const x0 = Math.max(0, Math.floor((r.left + scrollX) / cell)), x1 = Math.min(gw, Math.ceil((r.right + scrollX) / cell));
  const y0 = Math.max(0, Math.floor((r.top + scrollY) / cell)), y1 = Math.min(gh, Math.ceil((r.bottom + scrollY) / cell));
  for (let y = y0; y < y1; y++) grid.fill(1, y * gw + x0, y * gw + x1);
}
function paints(el, cs) {
  if (/^(img|svg|video|canvas|input|select|textarea|button|iframe|picture|object|embed|progress|meter)$/.test(el.localName)) return true;
  if (!/^(transparent|rgba\(0, 0, 0, 0\))$/.test(cs.backgroundColor) || cs.backgroundImage !== 'none') return true;
  if (parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth) + parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth) > 0) return true;
  for (const n of el.childNodes) if (n.nodeType === 3 && n.textContent.trim()) return true;
  return false;
}
let inSvg = 0;
// Screen-reader-only text (sr-only: a 1px box that clips, with margin -1px) has no design: its
// values are the hiding technique, not the system, so none are read inside it.
function visuallyHidden(el, cs, rects) {
  if (!rects.length) return false;
  const r = el.getBoundingClientRect();
  const clips = cs.overflow !== 'visible' || (cs.clip && cs.clip !== 'auto') || (cs.clipPath && cs.clipPath !== 'none');
  return ((r.width <= 2 || r.height <= 2) && clips) || /inset\(\s*50%/.test(cs.clipPath || '');
}
function walk(el, parentIdx, comp, interAncestor, hiddenAbove) {
  if (SKIP.has(el.tagName)) return;
  if (el !== document.documentElement && el.localName !== 'svg' && el.matches(OUT_SEL)) { leftOut++; return; }
  total++;
  if (records.length >= MAX) { truncated = true; return; }
  const cs = getComputedStyle(el);
  if (cs.display === 'none') return;
  const isRoot = el === document.documentElement;
  const rects = el.getClientRects();
  const boxed = isRoot || rects.length > 0;
  const hidden = !!hiddenAbove || (!isRoot && visuallyHidden(el, cs, rects));
  const own = compOf(el);
  const myComp = own ? {name: own.name, via: own.via, source: el.getAttribute('data-source')} : comp;
  const fp = el.assignedSlot || el.parentElement || (el.getRootNode().host || null);
  const pcs = isRoot ? null : (fp ? getComputedStyle(fp) : null);
  let idx = parentIdx;
  if (boxed && cs.visibility !== 'hidden') {
    parentTypedLH = typedLH(fp);
    const r = el.getBoundingClientRect();
    const inter = !interAncestor && el.matches(INTERACTIVE) && !el.disabled;
    idx = records.length;
    st.els.push({el, ps: null, pi: parentIdx});
    records.push({i: idx, pi: parentIdx, ps: null, sel: selOf(el), tag: el.localName, role: el.getAttribute('role'),
      rect: [r.x + scrollX, r.y + scrollY, r.width, r.height], comp: myComp, own: !!own,
      inter, inComp: !!myComp, v: hidden || (inSvg > 0 && el.localName !== 'svg') ? [] : values(el, null, cs, pcs, isRoot)});
    if (!isRoot && el !== document.body && paints(el, cs)) for (const rr of rects) paint(content, rr);
    if (own) for (const rr of rects) paint(ds, rr);
    for (const pseudo of ['::before', '::after']) {
      const ps = getComputedStyle(el, pseudo);
      if (!ps || ps.content === 'none' || ps.content === 'normal' || ps.display === 'none') continue;
      const pi = records.length;
      st.els.push({el, ps: pseudo, pi: idx});
      records.push({i: pi, pi: idx, ps: pseudo, sel: selOf(el) + pseudo, tag: el.localName, role: null,
        rect: [r.x + scrollX, r.y + scrollY, r.width, r.height], comp: myComp, own: false, inter: false,
        inComp: !!myComp, v: hidden ? [] : values(el, pseudo, ps, cs, false)});
    }
  }
  const nextInter = interAncestor || (boxed && el.matches(INTERACTIVE));
  if (el.localName === 'svg') inSvg++;
  if (el.shadowRoot) {
    st.roots.push(el.shadowRoot);
    for (const c of el.shadowRoot.children) walk(c, idx, myComp, nextInter, hidden);
  }
  for (const c of el.children) walk(c, idx, myComp, nextInter, hidden);
  if (el.localName === 'svg') inSvg--;
}
walk(document.documentElement, -1, null, false, false);
let c1 = 0, both = 0;
for (let k = 0; k < content.length; k++) if (content[k]) { c1++; if (ds[k]) both++; }
const dark = matchMedia('(prefers-color-scheme: dark)').matches;
const de = document.documentElement;
const themeHint = (de.dataset.theme || de.dataset.mode || (de.classList.contains('dark') ? 'dark' :
  de.classList.contains('light') ? 'light' : (dark ? 'dark' : 'light'))).toLowerCase();
return {records, truncated, total, typed: typedOK, area: c1 ? both / c1 : null, leftOut,
        areaCells: c1, themeHint, rootFontSize: parseFloat(getComputedStyle(de).fontSize) || 16};
}"""


# Read again the values of the given properties for every stored record (no origination
# filter: the probe compares them with the first read).
REREAD = "(props) => {" + _COMMON + r"""
const st = window[ST_KEY];
const want = new Set(props);
const typedOK = typeof Element.prototype.computedStyleMap === 'function';
const out = [];
for (const {el, ps} of st.els) {
  const cs = getComputedStyle(el, ps);
  const typed = (!ps && typedOK) ? el.computedStyleMap() : null;
  const row = {};
  for (const [prop] of PROPS) if (want.has(prop)) row[prop] = rawValue(cs, typed, prop);
  if (want.has('line-height')) row['line-height-px'] = cs.lineHeight;
  if (want.has('font-size')) row['font-size'] = cs.fontSize;
  out.push(row);
}
return out;
}"""


# Where each custom property is declared: per root (0 = the document, then open shadow roots
# in walk order), the selector and the chain of @media conditions around it.
DEFS = "(vars) => {" + _COMMON + r"""
const st = window[ST_KEY];
const want = new Set(vars);
const decls = {};
const unreadable = [];
function visit(rules, rootIdx, media) {
  for (const r of rules) {
    if (r instanceof CSSStyleRule) {
      for (let k = 0; k < r.style.length; k++) {
        const name = r.style[k];
        if (!want.has(name)) continue;
        (decls[name] = decls[name] || []).push({root: rootIdx, sel: r.selectorText, media: media.slice(),
          important: r.style.getPropertyPriority(name) === 'important'});
      }
      if (r.cssRules && r.cssRules.length) visit(r.cssRules, rootIdx, media);
    } else if (typeof CSSMediaRule !== 'undefined' && r instanceof CSSMediaRule) {
      visit(r.cssRules, rootIdx, [...media, r.conditionText || r.media.mediaText]);
    } else if (r.cssRules) {
      visit(r.cssRules, rootIdx, media);
    }
  }
}
st.roots.forEach((root, idx) => {
  const sheets = [...(root.styleSheets || []), ...(root.adoptedStyleSheets || [])];
  for (const sh of sheets) {
    let rules = null;
    try { rules = sh.cssRules; } catch (e) { unreadable.push(sh.href || '(inline)'); continue; }
    if (rules) visit(rules, idx, []);
  }
});
const rootCS = getComputedStyle(document.documentElement);
const defined = vars.filter(v => decls[v] || rootCS.getPropertyValue(v) !== '');
return {decls, defined, unreadable, roots: st.roots.length};
}"""


# Set custom properties to sentinels at their definition scopes. `plan` is
# {var: sentinel}; `defs` the DEFS result; `pinRootFont` pins the root font size so rem
# lengths hold still while font size tokens move.
APPLY = r"""([plan, decls, pinRootFont]) => {
const st = window.__agentosStudioTokens;
st.applied = st.applied || {styles: [], inline: [], hadStyle: document.documentElement.hasAttribute('style')};
const perRoot = new Map();
for (const [name, value] of Object.entries(plan)) {
  for (const d of (decls[name] || [])) {
    let rule = `${d.sel} { ${name}: ${value} !important; }`;
    for (const m of [...d.media].reverse()) rule = `@media ${m} { ${rule} }`;
    if (!perRoot.has(d.root)) perRoot.set(d.root, []);
    perRoot.get(d.root).push(rule);
  }
}
for (const [idx, rules] of perRoot) {
  const root = st.roots[idx];
  const s = document.createElement('style');
  s.setAttribute('data-agentos-probe', '');
  s.textContent = rules.join('\n');
  (root === document ? (document.head || document.documentElement) : root).appendChild(s);
  st.applied.styles.push(s);
}
const de = document.documentElement;
for (const [name, value] of Object.entries(plan)) {
  st.applied.inline.push([name, de.style.getPropertyValue(name), de.style.getPropertyPriority(name)]);
  de.style.setProperty(name, value, 'important');
}
if (pinRootFont) {
  st.applied.inline.push(['font-size', de.style.getPropertyValue('font-size'), de.style.getPropertyPriority('font-size')]);
  de.style.setProperty('font-size', pinRootFont, 'important');
}
return true;
}"""

RESTORE = r"""() => {
const st = window.__agentosStudioTokens;
if (!st || !st.applied) return true;
for (const s of st.applied.styles) s.remove();
const de = document.documentElement;
for (const [name, value, prio] of st.applied.inline.reverse()) {
  if (value) de.style.setProperty(name, value, prio); else de.style.removeProperty(name);
}
// Blink writes inline style changes back to the attribute lazily: read it first, or a later
// read brings back an empty style="" after the attribute was removed
de.getAttribute('style');
if (!st.applied.hadStyle && de.style.length === 0) de.removeAttribute('style');
st.applied = null;
return true;
}"""

TRANSITIONS_OFF = r"""(on) => {
const id = 'agentos-probe-still';
let s = document.getElementById(id);
if (on && !s) {
  s = document.createElement('style');
  s.id = id;
  s.textContent = '*, *::before, *::after { transition: none !important; }';
  (document.head || document.documentElement).appendChild(s);
} else if (!on && s) s.remove();
return true;
}"""

CLEANUP = r"""() => {
const st = window.__agentosStudioTokens;
if (st && st.frame) st.frame.remove();
for (const s of document.querySelectorAll('style[data-agentos-probe]')) s.remove();
const still = document.getElementById('agentos-probe-still');
if (still) still.remove();
delete window.__agentosStudioTokens;
return true;
}"""


# Custom property declarations for loading a token set from the page.
PAGE_DECLS = r"""() => {
const out = [];
const unreadable = [];
// What each custom property is used for, read from the declarations that use it: a variable used
// only in border-radius is a radius token whatever its name (--r-md as well as --radius-md).
const uses = {};
const USE = [['radius', /(?:^|;)\s*border(?:-[a-z]+)*-radius\s*:\s*([^;]+)/g]];
function visit(rules, chain) {
  for (const r of rules) {
    if (r instanceof CSSStyleRule) {
      const text = r.style.cssText || '';
      for (const [what, re] of USE) {
        for (const m of text.matchAll(re)) for (const v of m[1].matchAll(/var\(\s*(--[\w-]+)/g)) {
          uses[v[1]] = uses[v[1]] || {};
          uses[v[1]][what] = true;
        }
      }
      const d = {};
      for (let k = 0; k < r.style.length; k++) {
        const n = r.style[k];
        if (n.startsWith('--')) d[n] = r.style.getPropertyValue(n).trim();
      }
      if (Object.keys(d).length) out.push([[...chain, r.selectorText], d]);
    } else if (typeof CSSMediaRule !== 'undefined' && r instanceof CSSMediaRule) {
      visit(r.cssRules, [...chain, '@media ' + (r.conditionText || r.media.mediaText)]);
    } else if (r.cssRules) {
      visit(r.cssRules, chain);
    }
  }
}
const roots = [document];
const walker = (el) => { if (el.shadowRoot) { roots.push(el.shadowRoot); for (const c of el.shadowRoot.querySelectorAll('*')) walker(c); } };
for (const el of document.querySelectorAll('*')) walker(el);
for (const root of roots) {
  for (const sh of [...(root.styleSheets || []), ...(root.adoptedStyleSheets || [])]) {
    let rules;
    try { rules = sh.cssRules; } catch (e) { unreadable.push(sh.href || '(inline)'); continue; }
    visit(rules, root === document ? [] : [':host']);
  }
}
const inline = {};
const de = document.documentElement;
for (let k = 0; k < de.style.length; k++) { const n = de.style[k]; if (n.startsWith('--')) inline[n] = de.style.getPropertyValue(n).trim(); }
if (Object.keys(inline).length) out.push([[':root'], inline]);
return {blocks: out, unreadable, uses};
}"""

# Colours the Python parser cannot read (color-mix, light-dark, system colours): the page
# computes them on a probe element.
CANON_COLORS = r"""(values) => {
const el = document.createElement('div');
el.style.display = 'none';
document.documentElement.appendChild(el);
const out = {};
for (const v of values) {
  el.style.color = '';
  el.style.color = v;
  if (el.style.color) out[v] = getComputedStyle(el).color;
}
el.remove();
return out;
}"""
