// A theme snapshot: the page's own colour custom properties and the painted colours of each
// visible element, keyed by a DOM path that is the same for the same page in another theme.
(opts) => {
  const MAX = opts.max_elements || 4000;
  const html = document.documentElement;
  const body = document.body || html;
  /*COMMON*/
  const canvas = typeof OffscreenCanvas !== 'undefined' ? new OffscreenCanvas(1, 1) : document.createElement('canvas');
  canvas.width = 1; canvas.height = 1;
  const g = canvas.getContext('2d', { willReadFrequently: true });
  const memo = new Map();
  const norm = (v) => {
    if (!v) return null;
    v = v.trim();
    if (memo.has(v)) return memo.get(v);
    let out = null;
    if (window.CSS && CSS.supports('color', v) && !/^(inherit|initial|unset|currentcolor|revert)/i.test(v)) {
      g.clearRect(0, 0, 1, 1);
      g.fillStyle = '#000';
      g.fillStyle = v;
      g.fillRect(0, 0, 1, 1);
      const d = g.getImageData(0, 0, 1, 1).data;
      out = d[3] === 0 ? null : `${d[0]},${d[1]},${d[2]},${Math.round(d[3] / 255 * 100) / 100}`;
    }
    memo.set(v, out);
    return out;
  };
  // Custom property names: from the page's own (readable) style sheets and inline styles.
  const names = new Set();
  const walk = (rules) => {
    for (const r of rules) {
      if (r.style) for (const p of r.style) if (p.startsWith('--')) names.add(p);
      if (r.cssRules) walk(r.cssRules);
    }
  };
  let unreadable = 0;
  for (const sh of document.styleSheets) {
    try { walk(sh.cssRules); } catch (e) { unreadable++; }
  }
  for (const el of [html, body]) for (const p of el.style) if (p.startsWith('--')) names.add(p);
  // A custom property's computed value is its text: light-dark(#fff, #000) reads the same in both
  // themes. Its colour is read off a probe element in the same place, which resolves light-dark(),
  // color-mix() and system colours in that element's colour scheme.
  const probe = (host) => {
    const p = document.createElement('span');
    p.setAttribute('aria-hidden', 'true');
    p.style.cssText = 'position:absolute!important;width:0!important;height:0!important;overflow:hidden!important;visibility:hidden!important';
    host.appendChild(p);
    return p;
  };
  const probes = new Map([[html, probe(html)], [body, probe(body)]]);
  const resolved = (el, raw) => {
    raw = (raw || '').trim();
    if (!raw || !(window.CSS && CSS.supports('color', raw)) || /^(inherit|initial|unset|currentcolor|revert)/i.test(raw)) return norm(raw);
    if (!/light-dark\(|color-mix\(|var\(/i.test(raw)) return norm(raw);
    const p = probes.get(el);
    p.style.setProperty('color', raw, 'important');
    const v = norm(getComputedStyle(p).color);
    p.style.removeProperty('color');
    return v;
  };
  const vars = {};
  try {
    for (const n of names) {
      for (const el of [html, body]) {
        const v = resolved(el, getComputedStyle(el).getPropertyValue(n));
        if (v) { vars[el === html ? n : `body ${n}`] = v; }
      }
    }
  } finally {
    for (const p of probes.values()) p.remove();
  }
  const path = (el) => {
    const parts = [];
    for (let cur = el; cur && cur !== html; cur = cur.parentElement) {
      const p = cur.parentElement;
      const i = p ? [...p.children].indexOf(cur) : 0;
      parts.push(`${cur.tagName.toLowerCase()}:${i}`);
    }
    return parts.reverse().join('/');
  };
  if (opts.describe) {
    // Name the elements at these paths (the leaks found by comparing two snapshots).
    const want = new Set(opts.describe);
    const res = {};
    for (const el of body.querySelectorAll('*')) {
      const p = path(el);
      if (!want.has(p)) continue;
      const c = component(el);
      const r = el.getBoundingClientRect();
      res[p] = {
        sel: selector(el), group: groupKey(el, c), comp: c ? { name: c.name, source: c.source } : null, area: c && c.name ? null : areaOf(el),
        role: roleOf(el), name: cap(ownText(el) || el.getAttribute('aria-label') || ''), tag: el.tagName.toLowerCase(),
        rect: { x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height },
      };
    }
    return { described: res };
  }
  // Left out: pictures (aria-hidden, inert, role=img: a drawn page keeps its own colours) and the
  // regions the page or the caller marks as another product (data-studio="ignore", `ignore`).
  const sels = ['[data-studio="ignore"]'];
  for (const q of opts.ignore || []) { try { document.querySelector(q); sels.push(q); } catch (e) { /* said by page.js */ } }
  const OUT_SEL = '[aria-hidden=true],[inert],[role=img],' + sels.join(',');
  // The colour of the surface an element sits on: its own background, else the nearest one under it.
  const bgMemo = new Map();
  const surface = (el) => {
    if (!el || el.nodeType !== 1) return null;
    if (bgMemo.has(el)) return bgMemo.get(el);
    const v = norm(getComputedStyle(el).backgroundColor);
    const res = v && parseFloat(v.split(',')[3]) >= 0.5 ? v : surface(el.parentElement);
    bgMemo.set(el, res);
    return res;
  };
  const els = {};
  let n = 0, leftOut = 0;
  for (const el of body.querySelectorAll('*')) {
    if (n >= MAX) break;
    if (['SCRIPT', 'STYLE', 'TEMPLATE', 'NOSCRIPT'].includes(el.tagName)) continue;
    if (el.closest(OUT_SEL)) { leftOut++; continue; }
    if (el.checkVisibility && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    n++;
    const s = getComputedStyle(el);
    const ps = el.parentElement ? getComputedStyle(el.parentElement) : null;
    const rec = {};
    const hasText = [...el.childNodes].some((c) => c.nodeType === 3 && /\S/.test(c.data));
    if (hasText) {
      rec.color = norm(s.color);
      rec.inherits = !!ps && ps.color === s.color;
      rec.on = surface(el);
    }
    const bg = norm(s.backgroundColor);
    if (bg) {
      rec.bg = bg;
      // a small box with its own fill and text (initials on an avatar, a letter mark): a mark
      if (r.width <= 64 && r.height <= 64 && el.textContent.trim().length <= 3 && el.textContent.trim()) rec.mark = true;
    }
    if (parseFloat(s.borderTopWidth) > 0 && s.borderTopStyle !== 'none') {
      rec.border = norm(s.borderTopColor);
      rec.border_inherits = s.borderTopColor === s.color;
    }
    if (!Object.keys(rec).length) continue;
    els[path(el)] = rec;
  }
  return { vars, els, left_out: leftOut, unreadable_sheets: unreadable, scheme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light' };
}
