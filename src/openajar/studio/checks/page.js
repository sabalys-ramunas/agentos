// The measured layout checks, run in the page in one pass. Returns raw records per check; the
// Python side (openajar.studio.checks.run) turns them into findings. Everything is read from the
// live layout; the page is scrolled for hit testing and put back where it was. One probe adds and
// removes cloned <img> elements in the same task (health.images), so nothing is ever painted.
//
// Coordinates: rects are in document CSS px at the scroll position the page had on entry.
(opts) => {
  const want = new Set(opts.checks || []);
  const MAX = opts.max_elements || 4000;
  const html = document.documentElement;
  const body = document.body || html;
  const vw = html.clientWidth;
  const vh = html.clientHeight;
  const sx0 = window.scrollX;
  const sy0 = window.scrollY;
  const out = { checks: {}, meta: { vw, vh, dpr: window.devicePixelRatio || 1, notes: [] } };

  // --- small helpers ------------------------------------------------------------------------
  const csMap = new Map();
  const cs = (el) => {
    let s = csMap.get(el);
    if (!s) { s = getComputedStyle(el); csMap.set(el, s); }
    return s;
  };
  const r2 = (n) => Math.round(n * 100) / 100;
  const docRect = (r) => ({ x: r2(r.left + sx0), y: r2(r.top + sy0), w: r2(r.width), h: r2(r.height) });
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK', 'TITLE',
    'BR', 'WBR', 'OPTION', 'OPTGROUP', 'DATALIST', 'PARAM', 'SOURCE', 'TRACK']);
  /*COMMON*/

  const shown = (el) => {
    if (el.checkVisibility) {
      return el.checkVisibility({ opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true });
    }
    const s = cs(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity) > 0;
  };

  // Screen-reader-only and other visually hidden boxes: a box of 2px or less that clips.
  const vhMemo = new Map();
  const hiddenVisually = (el) => {
    if (vhMemo.has(el)) return vhMemo.get(el);
    let res = false;
    if (el && el.nodeType === 1 && el !== body && el !== html) {
      const r = el.getBoundingClientRect();
      const s = cs(el);
      const clips = s.overflow !== 'visible' || (s.clip && s.clip !== 'auto') || s.clipPath !== 'none';
      if ((r.width <= 2 || r.height <= 2) && clips) res = true;
      else if (s.clipPath && /inset\(\s*50%/.test(s.clipPath)) res = true;
      else res = hiddenVisually(el.parentElement);
    }
    vhMemo.set(el, res);
    return res;
  };

  // Inside fixed or sticky chrome (headers, bars, floating buttons, top-layer dialogs).
  const fxMemo = new Map();
  const fixedContext = (el) => {
    if (!el || el.nodeType !== 1) return false;
    if (fxMemo.has(el)) return fxMemo.get(el);
    const p = cs(el).position;
    const res = p === 'fixed' || p === 'sticky' || fixedContext(el.parentElement);
    fxMemo.set(el, res);
    return res;
  };

  const OVERLAY_SEL = 'dialog,[role=dialog],[role=alertdialog],[aria-modal=true],[role=menu],[role=listbox],' +
    '[role=tooltip],[popover]';
  const overlayLike = (el) => !!(el.closest && el.closest(OVERLAY_SEL));

  const modal = (() => {
    const cands = [...document.querySelectorAll('dialog[open],[aria-modal=true]')]
      .filter((d) => shown(d) && (d.matches('[aria-modal=true]') || (d.matches && safeMatches(d, ':modal'))));
    return cands.length ? cands[cands.length - 1] : null;
  })();
  function safeMatches(el, sel) { try { return el.matches(sel); } catch (e) { return false; } }
  const inScope = (el) => !modal || modal.contains(el);
  const inert = (el) => !!(el.closest && el.closest('[inert]'));

  // Regions no check judges: another product drawn or embedded in the page, or an exhibit shown on
  // purpose (a failing example). The page marks one with data-studio="ignore" (data-studio-why says
  // what it is); the caller or the profile names more with `ignore` selectors. Each one is counted
  // and named, so coverage says what was left out. They still take up room: an ignored region that
  // covers the page's own text or controls, or makes the page scroll sideways, is still reported.
  const ignoreSels = [];
  const badIgnore = [];
  for (const q of opts.ignore || []) {
    try { document.querySelector(q); ignoreSels.push(q); } catch (e) { badIgnore.push(String(q).slice(0, 80)); }
  }
  const IGNORE_SEL = ['[data-studio="ignore"]', ...ignoreSels].join(',');
  const ignMemo = new Map();
  const ignored = (el) => {
    if (!el || el.nodeType !== 1) return false;
    if (ignMemo.has(el)) return ignMemo.get(el);
    const v = !!el.closest(IGNORE_SEL);
    ignMemo.set(el, v);
    return v;
  };
  // Pictures: content hidden from screen readers (aria-hidden), shown as one image (role=img) or
  // out of reach (inert). Text in them is part of a picture, not text to read.
  const PICTURE_SEL = '[aria-hidden=true],[inert],[role=img]';
  const picture = (el) => !!(el.closest && el.closest(PICTURE_SEL));

  const INTERACTIVE_SEL = 'a[href],button,input:not([type=hidden]),select,textarea,summary,' +
    '[role=button],[role=link],[role=checkbox],[role=radio],[role=switch],[role=tab],[role=menuitem],' +
    '[role=menuitemcheckbox],[role=menuitemradio],[role=option],[role=slider],[role=combobox],[role=textbox],' +
    '[role=searchbox],[role=spinbutton]';
  const isInteractive = (el) => safeMatches(el, INTERACTIVE_SEL);
  const isDisabled = (el) => !!(el.closest && el.closest(':disabled,[aria-disabled=true]'));

  const rangeRects = (node, a, b) => {
    const r = document.createRange();
    if (a === undefined) r.selectNodeContents(node); else { r.setStart(node, a); r.setEnd(node, b); }
    return [...r.getClientRects()].filter((q) => q.width > 0.5 && q.height > 0.5);
  };
  // The rects of an element's own text, without leading and trailing white space.
  const trimmed = (n) => {
    const a = n.data.search(/\S/);
    const b = n.data.length - n.data.match(/\s*$/)[0].length;
    return [Math.max(0, a), Math.max(a, b)];
  };
  const textRects = (el) => ownTextNodes(el).flatMap((n) => { const [a, b] = trimmed(n); return rangeRects(n, a, b); });

  const info = (el) => {
    const comp = component(el);
    const r = el.getBoundingClientRect();
    const name = el.getAttribute('aria-label') || ownText(el) || el.getAttribute('alt') || el.getAttribute('title') || '';
    return {
      sel: selector(el),
      group: groupKey(el, comp),
      comp: comp ? { name: comp.name, source: comp.source } : null,
      area: comp && comp.name ? null : areaOf(el),
      role: roleOf(el),
      name: cap(name),
      rect: docRect(r),
      tag: el.tagName.toLowerCase(),
    };
  };

  const parseRgba = (s) => {
    const m = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/.exec(s);
    if (m) {
      let a = m[4] === undefined ? 1 : m[4].endsWith('%') ? parseFloat(m[4]) / 100 : parseFloat(m[4]);
      return [+m[1], +m[2], +m[3], a];
    }
    return null;
  };
  let probeCtx = null;
  const colorMemo = new Map();
  // Any CSS colour to sRGB [r, g, b, a]. Non-rgb forms (oklch, color(), named) go through a 1px canvas.
  const toRgba = (s) => {
    if (!s) return null;
    if (colorMemo.has(s)) return colorMemo.get(s);
    let v = parseRgba(s);
    if (!v) {
      if (s === 'transparent') v = [0, 0, 0, 0];
      else {
        try {
          if (!probeCtx) {
            const c = typeof OffscreenCanvas !== 'undefined' ? new OffscreenCanvas(1, 1) : document.createElement('canvas');
            c.width = 1; c.height = 1;
            probeCtx = c.getContext('2d', { willReadFrequently: true });
          }
          probeCtx.clearRect(0, 0, 1, 1);
          probeCtx.fillStyle = '#000';
          probeCtx.fillStyle = s;
          probeCtx.fillRect(0, 0, 1, 1);
          const d = probeCtx.getImageData(0, 0, 1, 1).data;
          v = [d[0], d[1], d[2], r2(d[3] / 255)];
        } catch (e) { v = null; }
      }
    }
    colorMemo.set(s, v);
    return v;
  };

  // Clip box: the intersection of the padding boxes of the ancestors that clip `el` (overflow
  // hidden or clip), following the containing block for absolute and fixed boxes. `self` adds the
  // element's own box. Viewport-relative.
  const isCB = (s) => s.position !== 'static' || s.transform !== 'none' || s.filter !== 'none' ||
    /paint|layout|strict|content/.test(s.contain || '');
  // With `scroll`, a scroll box (overflow auto or scroll) clips too: content scrolled out of it is
  // out of view, not covered. A clip-path inset() clips the element and everything in it.
  const insetBox = (cur, s) => {
    const m = /^inset\(([^)]*)\)/.exec(s.clipPath || '');
    if (!m) return null;
    const parts = m[1].split(/\s+round\s+/)[0].trim().split(/\s+/);
    const r = cur.getBoundingClientRect();
    const v = (t, size) => (t.endsWith('%') ? parseFloat(t) / 100 * size : parseFloat(t) || 0);
    const [t, rr = t, b = t, l = rr] = parts;
    return { l: r.left + v(l, r.width), t: r.top + v(t, r.height), r: r.right - v(rr, r.width), b: r.bottom - v(b, r.height) };
  };
  const clipBox = (el, self, scroll) => {
    let box = { l: -Infinity, t: -Infinity, r: Infinity, b: Infinity };
    const by = [];
    let needCB = false;
    let cur = self ? el : el.parentElement;
    if (!self) {
      const p = cs(el).position;
      if (p === 'fixed') return { box, by };
      if (p === 'absolute') needCB = true;
    }
    while (cur && cur !== html) {
      const s = cs(cur);
      const counts = !needCB || isCB(s) || cur === el;
      if (counts) {
        const ox = s.overflowX, oy = s.overflowY;
        const cx = ox === 'hidden' || ox === 'clip' || (scroll && (ox === 'auto' || ox === 'scroll'));
        const cy = oy === 'hidden' || oy === 'clip' || (scroll && (oy === 'auto' || oy === 'scroll'));
        if (cx || cy) {
          const r = cur.getBoundingClientRect();
          const l = r.left + cur.clientLeft, t = r.top + cur.clientTop;
          if (cx) { box.l = Math.max(box.l, l); box.r = Math.min(box.r, l + cur.clientWidth); }
          if (cy) { box.t = Math.max(box.t, t); box.b = Math.min(box.b, t + cur.clientHeight); }
          by.push(cur);
        }
        if (needCB && cur !== el && isCB(s)) needCB = false;
      }
      if (scroll && s.clipPath && s.clipPath !== 'none') {
        const q = insetBox(cur, s);
        if (q) {
          box.l = Math.max(box.l, q.l); box.r = Math.min(box.r, q.r); box.t = Math.max(box.t, q.t); box.b = Math.min(box.b, q.b);
          by.push(cur);
        }
      }
      if (cur !== el || self) {
        if (s.position === 'fixed') break;
        if (s.position === 'absolute' && cur !== el) needCB = true;
      }
      cur = cur.parentElement;
    }
    return { box, by };
  };
  const visFrac = (q, box, vtol) => {
    // How much of rect q lies inside box. Vertically a line that is at least `vtol` inside counts
    // as whole: a font's content area is taller than its glyphs.
    const w = Math.max(0, Math.min(q.right, box.r + 1) - Math.max(q.left, box.l - 1));
    let fh = Math.max(0, Math.min(q.bottom, box.b) - Math.max(q.top, box.t)) / q.height;
    if (vtol !== undefined) fh = fh >= vtol ? 1 : fh <= 1 - vtol ? fh * 0.5 / (1 - vtol) : fh;
    return (w / q.width) * fh;
  };

  // A row of three or more like items, cut sideways by `stop`, is a carousel or a scroller: by design.
  const inRow = (el, stop) => {
    for (let c = el; c && c !== stop; c = c.parentElement) {
      const p = c.parentElement;
      if (!p) break;
      const sibs = [...p.children].filter((x) => x.tagName === c.tagName);
      if (sibs.length >= 3) {
        const tops = sibs.slice(0, 3).map((x) => Math.round(x.getBoundingClientRect().top));
        if (Math.max(...tops) - Math.min(...tops) <= 2) return true;
      }
    }
    return false;
  };

  // A fade at the edge of a box: a gradient layer (a ::before or ::after, or a child that takes no
  // pointer events or is hidden from screen readers) laid over one edge of it. A feed or a list that
  // fades out this way is cut on purpose. Returns the viewport rects of the fades.
  const fadeMemo = new Map();
  const px0 = (v) => (v === 'auto' || v === '' || v == null ? null : parseFloat(v));
  const gradient = (s) => /gradient\(/.test(s.backgroundImage || '');
  const fades = (box) => {
    if (fadeMemo.has(box)) return fadeMemo.get(box);
    const out = [];
    const B = box.getBoundingClientRect();
    const P = { l: B.left + box.clientLeft, t: B.top + box.clientTop };
    P.r = P.l + (box.clientWidth || B.width); P.b = P.t + (box.clientHeight || B.height);
    const atEdge = (q) => q.right - q.left >= 4 && q.bottom - q.top >= 4 && (
      Math.abs(q.bottom - P.b) <= 2 || Math.abs(q.top - P.t) <= 2 || Math.abs(q.left - P.l) <= 2 || Math.abs(q.right - P.r) <= 2) &&
      ((q.right - q.left) >= 0.6 * (P.r - P.l) || (q.bottom - q.top) >= 0.6 * (P.b - P.t));
    for (const pseudo of ['::before', '::after']) {
      const s = getComputedStyle(box, pseudo);
      if (!s.content || s.content === 'none' || s.content === 'normal' || s.display === 'none') continue;
      if (!['absolute', 'fixed', 'sticky'].includes(s.position) || !gradient(s)) continue;
      const L = px0(s.left), R = px0(s.right), T = px0(s.top), Bo = px0(s.bottom), W = px0(s.width), H = px0(s.height);
      const x1 = L !== null ? P.l + L : (R !== null && W !== null ? P.r - R - W : P.l);
      const x2 = R !== null ? P.r - R : (W !== null ? x1 + W : P.r);
      const y1 = T !== null ? P.t + T : (Bo !== null && H !== null ? P.b - Bo - H : P.t);
      const y2 = Bo !== null ? P.b - Bo : (H !== null ? y1 + H : P.b);
      const q = { left: x1, right: x2, top: y1, bottom: y2 };
      if (atEdge(q)) out.push(q);
    }
    for (const c of box.children) {
      const s = cs(c);
      if (!['absolute', 'sticky'].includes(s.position) || !gradient(s)) continue;
      if (s.pointerEvents !== 'none' && c.getAttribute('aria-hidden') !== 'true') continue;
      const q = c.getBoundingClientRect();
      if (atEdge(q)) out.push(q);
    }
    fadeMemo.set(box, out);
    return out;
  };
  const masked = (box) => {
    const s = cs(box);
    return (s.maskImage && s.maskImage !== 'none') || (s.webkitMaskImage && s.webkitMaskImage !== 'none');
  };
  // The fade over these rects, if any ancestor of `el` fades them out.
  const underFade = (el, rects) => {
    for (let a = el.parentElement; a && a !== body && a !== html; a = a.parentElement) {
      const f = fades(a);
      if (f.length && rects.some((q) => f.some((g) => q.left < g.right && q.right > g.left && q.top < g.bottom && q.bottom > g.top))) return true;
    }
    return false;
  };

  // A box cut on purpose: one faded out with a mask or a fade at its edge, or a "read more" box
  // with a control that expands it (aria-expanded or aria-controls next to it).
  const collapsible = (box) => {
    if (masked(box) || fades(box).length) return true;
    if (box.id && document.querySelector(`[aria-controls~="${cssEsc(box.id)}"]`)) return true;
    const p = box.parentElement;
    if (!p) return false;
    return [...p.querySelectorAll('[aria-expanded]')].some((c) => !box.contains(c));
  };

  // --- the elements --------------------------------------------------------------------------
  const allRaw = body.querySelectorAll('*');
  const all = [];
  for (const el of allRaw) {
    if (all.length >= MAX) break;
    if (SKIP_TAGS.has(el.tagName)) continue;
    if (el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    all.push(el);
  }
  out.meta.elements = allRaw.length;
  out.meta.checked = all.length;
  out.meta.capped = allRaw.length > all.length + 0 && all.length >= MAX;
  out.meta.modal = modal ? selector(modal) : null;
  out.meta.shadow_roots = all.filter((el) => el.shadowRoot).length;
  out.meta.frames = all.filter((el) => el.tagName === 'IFRAME' || el.tagName === 'FRAME').length;

  // Text inside content hidden from screen readers (an illustration, avatar initials) is part of a
  // picture, not text to read: no text check judges it. Counted, so coverage can say so.
  // Text drawn too small to read (a thumbnail of a page, a miniature) is part of a drawing too.
  const textEls = [];
  const interactive = [];
  let decorativeText = 0, tinyText = 0, ignoredText = 0, ignoredControls = 0;
  for (const el of all) {
    if (!shown(el) || hiddenVisually(el)) continue;
    const off = ignored(el);
    if (el.tagName !== 'svg' && ownTextNodes(el).length && !['TEXTAREA', 'SELECT'].includes(el.tagName)) {
      const rects = textRects(el);
      if (rects.length) {
        if (off) ignoredText++;
        else if (picture(el)) decorativeText++;
        else if (Math.max(...rects.map((q) => q.height)) < 4) tinyText++;
        else textEls.push({ el, rects });
      }
    }
    if (isInteractive(el)) {
      if (off) ignoredControls++;
      else if (!el.closest('[role=img]')) interactive.push(el);   // a control in a picture is part of it
    }
  }
  out.meta.decorative_text = decorativeText;
  out.meta.tiny_text = tinyText;
  {
    const roots = [...document.querySelectorAll(IGNORE_SEL)].filter((r) => !(r.parentElement && r.parentElement.closest(IGNORE_SEL)));
    const docArea = Math.max(1, (document.scrollingElement || html).scrollWidth * (document.scrollingElement || html).scrollHeight);
    let area = 0;
    const named = roots.map((r) => {
      const q = r.getBoundingClientRect();
      area += q.width * q.height;
      const how = r.matches('[data-studio="ignore"]') ? 'data-studio="ignore"' : 'ignore: ' + ignoreSels.find((x) => r.matches(x));
      const why = (r.getAttribute('data-studio-why') || '').replace(/\s+/g, ' ').trim().slice(0, 60);
      return { sel: selector(r), how, why };
    });
    out.meta.ignored = { regions: named.slice(0, 6), count: roots.length, text: ignoredText, controls: ignoredControls,
      area_share: r2(Math.min(1, area / docArea)), bad: badIgnore };
  }

  const run = (id, fn) => {
    if (!want.has(id)) return;
    try {
      const res = fn();
      out.checks[id] = Object.assign({ status: 'ran', items: [] }, res || {});
    } catch (e) {
      out.checks[id] = { status: 'failed', reason: String(e && e.message || e).slice(0, 200), items: [] };
    }
  };

  // --- layout.page_overflow ------------------------------------------------------------------
  run('layout.page_overflow', () => {
    const se = document.scrollingElement || html;
    const docW = Math.max(se.scrollWidth, body.scrollWidth);
    const hs = cs(html), bs = cs(body);
    const clip = (v) => v === 'hidden' || v === 'clip';
    const viewportClips = clip(hs.overflowX) || (hs.overflowX === 'visible' && clip(bs.overflowX));
    if (docW <= vw + 1 || viewportClips) return { items: [], measured: { page_width: docW, viewport: vw } };
    const clipsX = new Map();
    const ancClips = (el) => {
      const p = el.parentElement;
      if (!p || p === html) return false;
      if (clipsX.has(p)) return clipsX.get(p);
      const s = cs(p);
      const v = (s.overflowX !== 'visible' && p !== body) || ancClips(p);
      clipsX.set(p, v);
      return v;
    };
    const cands = new Set();
    const right = new Map();
    for (const el of all) {
      const s = cs(el);
      if (s.position === 'fixed' || s.display === 'none') continue;
      if (ancClips(el)) continue;
      let rr = el.getBoundingClientRect().right + sx0;
      if (s.overflowX === 'visible') {
        for (const q of textRects(el)) rr = Math.max(rr, q.right + sx0);
      }
      if (rr > vw + 1) { cands.add(el); right.set(el, rr); }
    }
    const outer = [...cands].filter((el) => {
      for (let p = el.parentElement; p; p = p.parentElement) if (cands.has(p)) return false;
      return true;
    });
    const vis = outer.filter((el) => shown(el) && !hiddenVisually(el));
    const pool = vis.length ? vis : outer;
    pool.sort((a, b) => right.get(b) - right.get(a));
    const measured = { page_width: docW, viewport: vw, overflow_px: r2(docW - vw), causes: pool.length };
    if (!pool.length) return { items: [{ sel: 'html', group: 'html', comp: null, role: null, name: '', rect: null, tag: 'html', measured: Object.assign({ cause: 'unknown' }, measured) }] };
    const el = pool[0];
    const it = info(el);
    it.measured = Object.assign({ element_right: r2(right.get(el)), visible: shown(el) && !hiddenVisually(el) }, measured);
    it.width = r2(el.getBoundingClientRect().width);
    return { items: [it] };
  });

  // --- layout.clipped (controls cut by an overflow-hidden ancestor) --------------------------
  const clippedControls = new Set();
  run('layout.clipped', () => {
    const items = [];
    for (const el of interactive) {
      if (!inScope(el) || inert(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      const { box, by } = clipBox(el, false);
      if (!by.length) continue;
      const vis = visFrac(r, box);
      if (vis <= 0.01 || vis >= 0.98) continue;
      const cut = {
        left: Math.max(0, box.l - r.left), right: Math.max(0, r.right - box.r),
        top: Math.max(0, box.t - r.top), bottom: Math.max(0, r.bottom - box.b),
      };
      if (Math.max(cut.left, cut.right, cut.top, cut.bottom) < 2) continue;
      // A cut through invisible padding (a ghost button pulled out with a negative margin) shows
      // nothing missing: report it only when the content box or a painted surface is cut.
      {
        const s = cs(el);
        const px = (v) => parseFloat(v) || 0;
        const content = {
          left: r.left + px(s.borderLeftWidth) + px(s.paddingLeft), right: r.right - px(s.borderRightWidth) - px(s.paddingRight),
          top: r.top + px(s.borderTopWidth) + px(s.paddingTop), bottom: r.bottom - px(s.borderBottomWidth) - px(s.paddingBottom),
        };
        const contentCut = content.left < box.l - 1 || content.right > box.r + 1 || content.top < box.t - 1 || content.bottom > box.b + 1;
        const bg = toRgba(s.backgroundColor);
        const surface = (bg && bg[3] > 0.05) || s.backgroundImage !== 'none' ||
          ['Top', 'Right', 'Bottom', 'Left'].some((side) => px(s[`border${side}Width`]) > 0 && s[`border${side}Style`] !== 'none') ||
          ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName);
        if (!contentCut && !surface) continue;
      }
      if ((cut.left >= 2 || cut.right >= 2) && cut.top < 2 && cut.bottom < 2 && inRow(el, by[0])) continue;
      clippedControls.add(el);
      const it = info(el);
      it.measured = { visible_share: r2(vis), cut_px: Object.fromEntries(Object.entries(cut).filter(([, v]) => v >= 1).map(([k, v]) => [k, r2(v)])) };
      it.by = selector(by[0]);
      items.push(it);
    }
    return { items };
  });

  // --- text.truncated -------------------------------------------------------------------------
  // Where the full text of a cut text can be read: a title or label on it or the box that cuts it,
  // a tooltip attribute (data-tooltip, data-tip, data-full and the like), or the element its
  // aria-describedby names. Compared on letters and digits only, so "2 jobs, 4 browsers" in a title
  // reads "2 jobs · 4 browsers" in full.
  const alnum = (x) => (x || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
  const TIP_ATTRS = /^(title|aria-label|aria-description|data-(?:tooltip|tip|title|full|text|label|hint|original-title|bs-original-title|tippy-content|balloon))$/;
  // A title that says the same in other words ("2 CPUs" for "2 CPU", "jobs" for "job") counts when
  // every word of the text starts a word of the title.
  const words = (x) => (x || '').toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  const says = (value, want, ws) => {
    if (alnum(value).includes(want)) return true;
    const vs = words(value);
    return ws.length >= 2 && ws.every((w) => vs.some((v) => v.startsWith(w)));
  };
  const fullTextVia = (el, full, stop) => {
    const want = alnum(full);
    if (!want) return null;
    const ws = words(full);
    let depth = 0;
    for (let a = el; a && a !== body && depth < 6; a = a.parentElement, depth++) {
      for (const at of a.attributes) {
        if (TIP_ATTRS.test(at.name) && says(at.value, want, ws)) return at.name;
      }
      const ids = (a.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
      if (ids.some((i) => { const d = document.getElementById(i); return d && alnum(d.textContent).includes(want); })) return 'aria-describedby';
      if (a === stop) break;
    }
    return null;
  };
  run('text.truncated', () => {
    const items = [];
    for (const { el, rects } of textEls) {
      if (!inScope(el)) continue;
      const ctl = el.closest(INTERACTIVE_SEL);
      if (ctl && clippedControls.has(ctl)) continue;
      const s = cs(el);
      const { box, by } = clipBox(el, true);
      if (!by.length) continue;
      let area = 0, visA = 0, sideways = false, vertical = false;
      for (const q of rects) {
        const a = q.width * q.height; area += a; visA += a * visFrac(q, box, 0.7);
        if (q.left < box.l - 1 || q.right > box.r + 1) sideways = true;
        if (visFrac(q, { l: -Infinity, r: Infinity, t: box.t, b: box.b }, 0.7) < 0.999) vertical = true;
      }
      if (!area) continue;
      if (by.some(collapsible)) continue;
      if (sideways && !vertical && s.textOverflow !== 'ellipsis' && inRow(el, by[by.length - 1])) continue;
      let hidden = 1 - visA / area;
      // text-overflow is not inherited: the block that clips draws the ellipsis for the text in it
      let ellipsis = false, ellBox = el;
      for (let a = el; a; a = a.parentElement) {
        const as = cs(a);
        if (as.textOverflow === 'ellipsis' && as.overflowX !== 'visible') { ellipsis = true; ellBox = a; break; }
        if (a === by[0] || !as.display.startsWith('inline')) break;
      }
      const clamp = s.webkitLineClamp && s.webkitLineClamp !== 'none';
      if (ellipsis) {
        // the box that draws the ellipsis: the element itself, or the block an inline text sits in
        const bs = cs(ellBox);
        const cw = ellBox.clientWidth - parseFloat(bs.paddingLeft) - parseFloat(bs.paddingRight);
        if (ellBox === el) {
          const tw = rects.reduce((m, q) => Math.max(m, q.right), -Infinity) - rects.reduce((m, q) => Math.min(m, q.left), Infinity);
          if (tw > cw + 0.5) hidden = Math.max(hidden, 1 - cw / tw);
        } else if (cw > 0 && ellBox.scrollWidth > ellBox.clientWidth + 1) {
          hidden = Math.max(hidden, 1 - ellBox.clientWidth / ellBox.scrollWidth);
        }
      }
      if (hidden < 0.02 || visA <= 0) continue;
      // What is shown: characters whose box is inside the clip box, in reading order.
      const nodes = ownTextNodes(el);
      const full = nodes.map((n) => n.data).join('').replace(/\s+/g, ' ').trim();
      let shownChars = 0, total = 0, done = false;
      for (const n of nodes) {
        const len = n.data.length;
        if (done) { total += len; continue; }
        const charOk = (i) => {
          const rr = rangeRects(n, i, i + 1);
          if (!rr.length) return true;
          return rr.every((q) => visFrac(q, box, 0.7) >= 0.95) && (!ellipsis || rr[0].right <= box.r - 0);
        };
        let lo = 0, hi = len;
        if (charOk(len - 1)) lo = len;
        else {
          while (lo < hi) { const mid = (lo + hi) >> 1; if (charOk(mid)) lo = mid + 1; else hi = mid; }
          done = true;
        }
        shownChars += lo; total += len;
      }
      const shownText = nodes.map((n) => n.data).join('').slice(0, shownChars).replace(/\s+/g, ' ').trim();
      const via = fullTextVia(el, full, by[0]);
      const reachable = !!via;
      const kind = ellipsis ? 'ellipsis' : clamp ? 'clamp' : 'cut';
      // A deliberate ellipsis or clamp whose full text is reachable is a design choice, not a fault.
      if ((kind === 'ellipsis' || kind === 'clamp') && reachable) continue;
      const it = info(el);
      it.name = cap(full);
      it.measured = {
        kind, shown: cap(shownText, 60), hidden_share: r2(hidden),
        visible_chars: shownChars, total_chars: total, reachable,
      };
      if (via) it.measured.full_text_in = via;
      it.by = selector(by[0]);
      items.push(it);
    }
    return { items };
  });

  // --- hit testing: occlusion, text over text, what is behind text ---------------------------
  const paints = (e) => {
    const s = cs(e);
    if (['IMG', 'VIDEO', 'CANVAS', 'IFRAME', 'svg', 'INPUT', 'TEXTAREA', 'SELECT', 'OBJECT', 'EMBED'].includes(e.tagName)) return true;
    if (e.closest('svg')) return true;
    const bg = toRgba(s.backgroundColor);
    if (bg && bg[3] > 0.05) return true;
    if (s.backgroundImage && s.backgroundImage !== 'none') return true;
    if (ownTextNodes(e).length) return true;
    return false;
  };
  const paintsBox = (e) => {
    // Paints a surface (not just glyphs) at the point.
    const s = cs(e);
    if (['IMG', 'VIDEO', 'CANVAS', 'IFRAME', 'svg', 'OBJECT', 'EMBED'].includes(e.tagName) || e.closest('svg')) return true;
    const bg = toRgba(s.backgroundColor);
    if (bg && bg[3] > 0.05) return true;
    return !!(s.backgroundImage && s.backgroundImage !== 'none');
  };

  const needOcc = want.has('layout.occluded');
  const needOverlap = want.has('layout.text_overlap');
  const needContrast = want.has('contrast.text');
  const points = []; // {x, y (doc or viewport when fixed), fixed, kind, subj, idx}
  const occSubjects = new Map(); // el -> {kind, n, covered, covers: Map}
  if (needOcc || needContrast) {
    // Only the part of an element its clipping ancestors let through can be covered: a point cut
    // away by overflow is clipped (layout.clipped and text.truncated say so), and a point scrolled
    // out of a scroll box or cut by a clip-path is out of view: neither is occluded.
    const add = (el, kind, pts, fixed) => {
      const rec = { kind, n: 0, covered: 0, covers: new Map(), behind: false, stacked: 0 };
      occSubjects.set(el, rec);
      const { box } = clipBox(el, kind === 'text', true);
      const ox = fixed ? 0 : sx0, oy = fixed ? 0 : sy0;
      for (const [x, y] of pts) {
        const vx = x - ox, vy = y - oy;
        if (vx < box.l - 1 || vx > box.r + 1 || vy < box.t - 1 || vy > box.b + 1) continue;
        points.push({ x, y, fixed, subj: el, rec }); rec.n++;
      }
    };
    const seen = new Set();
    for (const el of interactive) {
      if (!inScope(el) || inert(el) || isDisabled(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) continue;
      const fx = fixedContext(el);
      const ox = fx ? 0 : sx0, oy = fx ? 0 : sy0;
      const pts = [[0.5, 0.5], [0.25, 0.25], [0.75, 0.25], [0.25, 0.75], [0.75, 0.75]]
        .map(([a, b]) => [r.left + r.width * a + ox, r.top + r.height * b + oy]);
      add(el, 'control', pts, fx);
      seen.add(el);
    }
    for (const { el, rects } of textEls) {
      if (seen.has(el) || !inScope(el)) continue;
      if (el.closest(INTERACTIVE_SEL) && seen.has(el.closest(INTERACTIVE_SEL))) continue;
      const fx = fixedContext(el);
      const ox = fx ? 0 : sx0, oy = fx ? 0 : sy0;
      let pts;
      if (rects.length === 1) {
        const q = rects[0];
        pts = [0.1, 0.3, 0.5, 0.7, 0.9].map((a) => [q.left + q.width * a + ox, q.top + q.height / 2 + oy]);
      } else {
        const pick = rects.length <= 5 ? rects : [0, 1, 2, 3, 4].map((i) => rects[Math.round(i * (rects.length - 1) / 4)]);
        pts = pick.map((q) => [q.left + q.width / 2 + ox, q.top + q.height / 2 + oy]);
      }
      add(el, 'text', pts, fx);
    }
  }

  // Text over text: candidate pairs from line rects, then confirmed by hit testing.
  const overlapPairs = [];
  if (needOverlap) {
    const lines = [];
    for (const { el, rects } of textEls) {
      if (fixedContext(el) || !inScope(el)) continue;
      // Only the part of each line that is not clipped away can overlap anything.
      const { box } = clipBox(el, true, true);
      for (const q of rects) {
        const l = Math.max(q.left, box.l), r = Math.min(q.right, box.r);
        const t = Math.max(q.top, box.t), b = Math.min(q.bottom, box.b);
        if (r - l < 1 || b - t < 1) continue;
        lines.push({ el, l: l + sx0, t: t + sy0, r: r + sx0, b: b + sy0 });
      }
    }
    lines.sort((a, b) => a.t - b.t);
    const seenPair = new Set();
    for (let i = 0; i < lines.length && overlapPairs.length < 200; i++) {
      const A = lines[i];
      for (let j = i + 1; j < lines.length && lines[j].t < A.b; j++) {
        const B = lines[j];
        if (A.el === B.el) continue;
        const ix = Math.min(A.r, B.r) - Math.max(A.l, B.l);
        const iy = Math.min(A.b, B.b) - Math.max(A.t, B.t);
        if (ix < 4 || iy <= 0) continue;
        const ha = A.b - A.t, hb = B.b - B.t;
        if (iy < 0.35 * Math.min(ha, hb)) continue;
        const area = ix * iy, minA = Math.min((A.r - A.l) * ha, (B.r - B.l) * hb);
        if (area < 0.2 * minA && ix < 24) continue;
        const key = A.el === B.el ? '' : [selector(A.el), selector(B.el)].sort().join('|');
        if (seenPair.has(key)) continue;
        seenPair.add(key);
        overlapPairs.push({
          a: A.el, b: B.el, x: Math.max(A.l, B.l) + ix / 2, y: Math.max(A.t, B.t) + iy / 2,
          area: r2(area), confirmed: null,
        });
      }
    }
  }

  const windows = [];
  const scrollMax = Math.max(0, (document.scrollingElement || html).scrollHeight - vh);
  windows.push(sy0);
  for (let y = 0; y <= scrollMax && windows.length < 16; y += vh) if (!windows.includes(y)) windows.push(Math.min(y, scrollMax));
  if (scrollMax > 0 && !windows.includes(scrollMax) && windows.length < 16) windows.push(scrollMax);
  out.meta.windows_capped = (scrollMax / vh) + 1 > 15;

  const relation = (subj, e) => (e === subj || subj.contains(e)) ? 'self' : e.contains(subj) ? 'ancestor' : 'other';
  // Two layers stacked on purpose (a before and after view, a crossfade): the subject and the cover
  // are in sibling layers of the same box, one of them positioned over the other, both holding
  // content of the same kind. The lower one is hidden by design, not by a fault.
  const stackMemo = new Map();
  const stacked = (subj, cover) => {
    let p = cover.parentElement;
    while (p && !p.contains(subj)) p = p.parentElement;
    if (!p) return false;
    const childOf = (el) => { let c = el; while (c && c.parentElement !== p) c = c.parentElement; return c; };
    const a = childOf(subj), b = childOf(cover);
    if (!a || !b || a === b) return false;
    const key = a;
    if (stackMemo.has(key) && stackMemo.get(key).has(b)) return stackMemo.get(key).get(b);
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    const near = (x, y) => Math.abs(x - y) <= 2;
    const content = (x) => !!(ownTextNodes(x).length || x.querySelector('img,svg,video,canvas,picture') || (x.textContent || '').trim());
    const v = near(ra.left, rb.left) && near(ra.top, rb.top) && near(ra.width, rb.width) && near(ra.height, rb.height) &&
      ra.width >= 40 && ra.height >= 40 && a.tagName === b.tagName && content(a) && content(b) &&
      [a, b].some((x) => ['absolute', 'fixed'].includes(cs(x).position));
    if (!stackMemo.has(key)) stackMemo.set(key, new Map());
    stackMemo.get(key).set(b, v);
    return v;
  };
  const testPoint = (p, vx, vy) => {
    if (vx < 0 || vy < 0 || vx >= vw || vy >= vh) return false;
    const stack = document.elementsFromPoint(vx, vy);
    const rec = p.rec, subj = p.subj;
    let covered = null;
    let i = 0;
    for (; i < stack.length; i++) {
      const e = stack[i];
      const rel = relation(subj, e);
      if (rel !== 'other') break;
      if (e === html || e === body) break;
      if (fixedContext(e) || overlayLike(e)) { covered = null; i = stack.length; break; }
      if (rec.kind === 'control') {
        if (e.tagName === 'LABEL' && subj.labels && [...subj.labels].includes(e)) break;
        covered = e; break;
      }
      if (paints(e)) {
        // Text drawn by the cover over this text is text over text, reported once by that check.
        if (!paintsBox(e) && ownTextNodes(e).length) { covered = 'text'; break; }
        covered = e; break;
      }
    }
    if (covered && covered !== 'text' && stacked(subj, covered)) { rec.stacked++; covered = null; }
    if (covered && covered !== 'text') {
      rec.covered++;
      rec.covers.set(covered, (rec.covers.get(covered) || 0) + 1);
    }
    // What is under the text, down to its own ancestors: anything that paints there and is not an
    // ancestor sends the contrast check to the pixel route.
    if (rec.kind === 'text' && !covered && i < stack.length) {
      for (let k = i + 1; k < stack.length; k++) {
        const e = stack[k];
        if (e.contains(subj)) {
          const bg = toRgba(cs(e).backgroundColor);
          if (bg && bg[3] >= 0.999) break;
          continue;
        }
        if (paintsBox(e)) { rec.behind = true; break; }
      }
    }
    return true;
  };

  if (points.length || overlapPairs.length) {
    const pending = new Set(points);
    const pendingPairs = new Set(overlapPairs);
    let first = true;
    for (const y0 of windows) {
      if (!pending.size && !pendingPairs.size) break;
      if (!first || window.scrollY !== y0) window.scrollTo({ left: sx0, top: y0, behavior: 'instant' });
      const ay = window.scrollY, ax = window.scrollX;
      for (const p of [...pending]) {
        if (p.fixed) {
          if (first) { testPoint(p, p.x, p.y); pending.delete(p); }
          continue;
        }
        const vy = p.y - ay, vx = p.x - ax;
        if (vy < 0 || vy >= vh) continue;
        testPoint(p, vx, vy);
        pending.delete(p);
      }
      for (const pr of [...pendingPairs]) {
        const vy = pr.y - ay, vx = pr.x - ax;
        if (vy < 0 || vy >= vh || vx < 0 || vx >= vw) continue;
        pendingPairs.delete(pr);
        const stack = document.elementsFromPoint(vx, vy);
        let firstT = null, between = false;
        pr.confirmed = false; // both texts must be hit at the point, with nothing opaque between
        for (const e of stack) {
          const isA = e === pr.a, isB = e === pr.b;
          if (!firstT) {
            if (isA || isB) firstT = isA ? 'a' : 'b';
            else if (e.contains(pr.a) && e.contains(pr.b)) break;
            continue;
          }
          if ((firstT === 'a' && isB) || (firstT === 'b' && isA)) { pr.confirmed = !between; break; }
          if (paintsBox(e)) between = true;
        }
      }
      first = false;
    }
    window.scrollTo({ left: sx0, top: sy0, behavior: 'instant' });
    out.meta.unreached_points = pending.size;
  }

  run('layout.occluded', () => {
    const items = [];
    const minShare = opts.occluded_min_share || 0.4;
    let stackedN = 0;
    for (const [el, rec] of occSubjects) {
      if (rec.n && rec.stacked / rec.n >= minShare) stackedN++;
      if (!rec.n || rec.covered / rec.n < minShare) continue;
      let cover = null, best = 0;
      for (const [c, n] of rec.covers) if (n > best) { best = n; cover = c; }
      const it = info(el);
      const ci = info(cover);
      it.measured = { covered_share: r2(rec.covered / rec.n), points: rec.n, kind: rec.kind, cover: ci.sel };
      it.cover = { sel: ci.sel, group: ci.group, comp: ci.comp, tag: ci.tag };
      items.push(it);
    }
    return { items, stacked: stackedN };
  });

  run('layout.text_overlap', () => {
    const items = [];
    for (const pr of overlapPairs) {
      if (pr.confirmed !== true) continue;
      const a = info(pr.a), b = info(pr.b);
      const [first, second] = a.sel < b.sel ? [a, b] : [b, a];
      first.measured = { overlap_px2: pr.area, other: second.sel, other_text: second.name };
      first.other = { sel: second.sel, group: second.group, comp: second.comp };
      items.push(first);
    }
    return { items, unconfirmed: overlapPairs.filter((p) => p.confirmed === null).length };
  });

  // --- contrast.text --------------------------------------------------------------------------
  // The visible label of a control that is disabled: a <label> of it, or the text its
  // aria-labelledby names. WCAG 1.4.3 does not ask contrast of an inactive control.
  const off = (c) => !!c && (c.disabled === true || c.getAttribute('aria-disabled') === 'true' || !!(c.closest && c.closest(':disabled')));
  const labelsDisabled = (el) => {
    const lab = el.closest('label');
    if (lab && (off(lab.control) || [...lab.querySelectorAll('input,select,textarea,button')].some(off))) return true;
    for (let a = el, d = 0; a && d < 3; a = a.parentElement, d++) {
      if (!a.id) continue;
      for (const c of document.querySelectorAll(`[aria-labelledby~="${cssEsc(a.id)}"]`)) if (off(c)) return true;
    }
    return false;
  };
  run('contrast.text', () => {
    const items = [];
    const jobs = [];
    const notJudged = {};
    const skip = (why) => { notJudged[why] = (notJudged[why] || 0) + 1; };
    // A ::before or ::after that paints a surface sends the text to the pixel route only when it
    // can lie under the text: positioned out of the flow, over the text's lines. An in-flow one (a
    // bullet, an icon before the label) and a positioned one elsewhere (a 2 px bar at a row's edge)
    // are beside the text, not behind it.
    const pseudoMemo = new Map();
    const pseudoBoxes = (a) => {
      if (pseudoMemo.has(a)) return pseudoMemo.get(a);
      const boxes = [];
      for (const p of ['::before', '::after']) {
        const s = getComputedStyle(a, p);
        if (!s.content || s.content === 'none' || s.content === 'normal' || s.display === 'none') continue;
        const bg = toRgba(s.backgroundColor);
        if (!((bg && bg[3] > 0.05) || (s.backgroundImage && s.backgroundImage !== 'none'))) continue;
        if (!['absolute', 'fixed'].includes(s.position)) continue;
        const B = a.getBoundingClientRect();
        const P = { l: B.left + a.clientLeft, t: B.top + a.clientTop };
        P.r = P.l + (a.clientWidth || B.width); P.b = P.t + (a.clientHeight || B.height);
        const L = px0(s.left), R = px0(s.right), T = px0(s.top), Bo = px0(s.bottom), W = px0(s.width), H = px0(s.height);
        const x1 = L !== null ? P.l + L : (R !== null && W !== null ? P.r - R - W : P.l);
        const x2 = R !== null ? P.r - R : (W !== null ? x1 + W : P.r);
        const y1 = T !== null ? P.t + T : (Bo !== null && H !== null ? P.b - Bo - H : P.t);
        const y2 = Bo !== null ? P.b - Bo : (H !== null ? y1 + H : P.b);
        // a fixed pseudo or one whose box cannot be read: assume it may lie under the text
        boxes.push(s.position === 'fixed' ? null : { left: x1, right: x2, top: y1, bottom: y2 });
      }
      pseudoMemo.set(a, boxes);
      return boxes;
    };
    const pseudoUnder = (a, rects) => pseudoBoxes(a).some((g) => g === null ||
      rects.some((q) => q.left < g.right && q.right > g.left && q.top < g.bottom && q.bottom > g.top));
    const rootDark = /dark/.test(cs(html).colorScheme || '') && matchMedia('(prefers-color-scheme: dark)').matches;
    // Layers hit testing cannot see (pointer-events: none) that paint over or under some text.
    const ghosts = all.filter((e) => cs(e).pointerEvents === 'none' && cs(e).position !== 'static' &&
      paintsBox(e) && shown(e)).map((e) => [e, e.getBoundingClientRect()]);
    const ghostOver = (el, rects) => ghosts.some(([g, gr]) => !g.contains(el) && !el.contains(g) &&
      rects.some((q) => q.left < gr.right && q.right > gr.left && q.top < gr.bottom && q.bottom > gr.top));
    // A separator or bullet alone ("·", "|", "/", "•") is not text to read: WCAG asks contrast of text.
    const SEPARATOR = /^[\s·•‧∙|/\\¦‖\u2014\u2013\-:;,.~*>»›<«‹+]{1,3}$/u;
    for (const { el, rects } of textEls) {
      if (!inScope(el) || isDisabled(el)) continue;
      if (labelsDisabled(el)) { skip('the label of a disabled control (WCAG 1.4.3 exempts inactive controls)'); continue; }
      const s = cs(el);
      if (SEPARATOR.test(ownText(el).trim())) { skip('separator glyphs, not text'); continue; }
      if (underFade(el, rects)) { skip('under a fade at the edge of its box, by design'); continue; }
      if (s.backgroundClip === 'text' || s.webkitBackgroundClip === 'text') { skip('gradient text'); continue; }
      const fill = s.webkitTextFillColor;
      let fg = toRgba(fill && fill !== s.color ? fill : s.color);
      if (!fg) { skip('colour not read'); continue; }
      if (fg[3] === 0) continue; // transparent text paints nothing
      const size = parseFloat(s.fontSize);
      const weight = parseInt(s.fontWeight, 10) || 400;
      const large = size >= 24 || (size >= 18.66 && weight >= 700);
      let complex = null;
      let opacity = 1;
      let cssOk = true;   // false when a filter or blend mode changes the painted text colour
      const layers = [];
      let reachedOpaque = false;
      for (let a = el; a; a = a.parentElement) {
        const as = cs(a);
        if (as.filter !== 'none') { complex = complex || 'filter'; cssOk = false; }
        if (as.mixBlendMode !== 'normal') { complex = complex || 'blend'; cssOk = false; }
        if (as.backdropFilter && as.backdropFilter !== 'none') complex = complex || 'backdrop';
        const op = parseFloat(as.opacity);
        if (op < 1) { opacity *= op; complex = complex || 'opacity'; }
        if (as.backgroundImage && as.backgroundImage !== 'none') { complex = complex || 'image'; }
        if (pseudoUnder(a, rects)) complex = complex || 'pseudo';
        const bg = toRgba(as.backgroundColor);
        if (bg && bg[3] > 0) {
          layers.push(bg);
          if (bg[3] >= 0.999) { reachedOpaque = true; break; }
        }
      }
      const rec = occSubjects.get(el);
      if (rec && rec.behind) complex = complex || 'behind';
      if (!complex && ghosts.length && ghostOver(el, rects)) complex = 'behind';
      if (!reachedOpaque && rootDark) complex = complex || 'canvas';
      const it = info(el);
      it.fg = fg; it.large = large; it.font = { size_px: size, weight };
      it.name = cap(ownText(el));
      if (!complex) {
        it.layers = layers; // top first; the last one is opaque, else the canvas is white
        items.push(it);
      } else {
        it.route = complex;
        it.fixed = fixedContext(el);
        it.rects = rects.map((q) => ({
          x: r2(q.left + (it.fixed ? 0 : sx0)), y: r2(q.top + (it.fixed ? 0 : sy0)), w: r2(q.width), h: r2(q.height),
        }));
        // every opacity above the text, not only up to its first opaque background: the pixel route
        // sees them all
        let groupOpacity = 1;
        for (let a = el; a; a = a.parentElement) groupOpacity *= parseFloat(cs(a).opacity);
        it.opacity = r2(groupOpacity);
        it.trust_css = cssOk;
        jobs.push(it);
      }
    }
    return { items, jobs, not_judged: notJudged };
  });

  // --- target.size (WCAG 2.5.8) ---------------------------------------------------------------
  run('target.size', () => {
    const min = opts.target_min || 24;
    const items = [];
    const targets = [];
    for (const el of interactive) {
      if (!inScope(el) || inert(el) || isDisabled(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      // A target inside another target is part of it.
      const outerT = el.parentElement && el.parentElement.closest(INTERACTIVE_SEL);
      if (outerT) continue;
      targets.push({ el, r, cx: r.left + r.width / 2, cy: r.top + r.height / 2 });
    }
    if (targets.length > 1500) out.meta.notes.push('target.size: first 1500 targets');
    const T = targets.slice(0, 1500);
    const small = (t) => t.r.width < min - 0.01 || t.r.height < min - 0.01;
    for (const t of T) {
      if (!small(t)) continue;
      const el = t.el, s = cs(el);
      // Inline exception: a link in a sentence.
      if (el.tagName === 'A' && s.display === 'inline') {
        let block = el.parentElement;
        while (block && block !== body && cs(block).display.startsWith('inline')) block = block.parentElement;
        // A sentence: text in the same block that is not itself part of a link or control.
        let sentence = false;
        if (block) {
          const tw = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
          for (let n = tw.nextNode(); n && !sentence; n = tw.nextNode()) {
            if (!/\S/.test(n.data)) continue;
            const owner = n.parentElement && n.parentElement.closest(INTERACTIVE_SEL);
            if (!owner || !block.contains(owner)) sentence = true;
          }
        }
        if (sentence) continue;
      }
      // User agent exception: a native control the page did not restyle.
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) && s.appearance !== 'none') continue;
      let conflict = null, gap = Infinity;
      for (const o of T) {
        if (o === t || o.el.contains(el) || el.contains(o.el)) continue;
        let d;
        if (small(o)) d = Math.hypot(o.cx - t.cx, o.cy - t.cy) - min;
        else {
          const dx = Math.max(o.r.left - t.cx, 0, t.cx - o.r.right);
          const dy = Math.max(o.r.top - t.cy, 0, t.cy - o.r.bottom);
          d = Math.hypot(dx, dy) - min / 2;
        }
        if (d < gap) { gap = d; if (d < 0) conflict = o; }
      }
      if (!conflict) continue;
      const it = info(el);
      it.measured = { w: r2(t.r.width), h: r2(t.r.height), nearest: selector(conflict.el), spacing_short_px: r2(-gap) };
      items.push(it);
    }
    const aaa = [];
    if (opts.target_aaa) {
      for (const t of T) {
        if (t.r.width >= 44 && t.r.height >= 44) continue;
        if (items.some((i) => i.sel === selector(t.el))) continue;
        const it = info(t.el);
        it.measured = { w: r2(t.r.width), h: r2(t.r.height) };
        aaa.push(it);
      }
    }
    return { items, aaa };
  });

  // --- name.missing ---------------------------------------------------------------------------
  run('name.missing', () => {
    const items = [];
    const NAMED_ROLES = new Set(['button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'menuitemcheckbox',
      'menuitemradio', 'option', 'slider', 'combobox', 'textbox', 'searchbox', 'spinbutton', 'img']);
    const hiddenAT = (el) => !!el.closest('[aria-hidden=true],[inert]');
    const hasContentName = (el, depth = 0) => {
      if (depth > 30) return false;
      for (const pseudo of ['::before', '::after']) {
        const c = getComputedStyle(el, pseudo).content;
        if (c && c !== 'none' && c !== 'normal' && c.replace(/["'\s]/g, '').length) return true;
      }
      for (const n of el.childNodes) {
        if (n.nodeType === 3 && /\S/.test(n.data)) return true;
        if (n.nodeType !== 1) continue;
        if (n.getAttribute('aria-hidden') === 'true') continue;
        const s = cs(n);
        if (s.display === 'none' || s.visibility === 'hidden') continue;
        if (n.getAttribute('aria-label') && n.getAttribute('aria-label').trim()) return true;
        if (n.tagName === 'IMG' && (n.getAttribute('alt') || '').trim()) return true;
        if (n.tagName.toLowerCase() === 'svg' && (n.querySelector('title')?.textContent || '').trim()) return true;
        if (n.tagName === 'INPUT' && (n.value || '').trim()) return true;
        if (hasContentName(n, depth + 1)) return true;
      }
      return false;
    };
    const labelledBy = (el) => {
      const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
      return ids.some((id) => { const r = document.getElementById(id); return r && r.textContent.trim(); });
    };
    for (const el of all) {
      const role = roleOf(el);
      if (!role || !NAMED_ROLES.has(role) || ignored(el) || (el.parentElement && el.parentElement.closest('[role=img]'))) continue;
      if (el.tagName === 'A' && !el.hasAttribute('href') && !el.getAttribute('role')) continue;
      if (['presentation', 'none'].includes(el.getAttribute('role'))) continue;
      const s = cs(el);
      if (s.display === 'none' || s.visibility === 'hidden' || hiddenAT(el)) continue;
      if (el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
      if ((el.getAttribute('aria-label') || '').trim() || labelledBy(el) || (el.getAttribute('title') || '').trim()) continue;
      let named = false;
      const t = el.tagName;
      if (t === 'IMG') {
        if (el.hasAttribute('alt')) continue; // alt="" marks a decorative image
      } else if (t === 'INPUT' || t === 'SELECT' || t === 'TEXTAREA') {
        const ty = (el.type || '').toLowerCase();
        if (el.labels && [...el.labels].some((l) => l.textContent.trim() || hasContentName(l))) named = true;
        else if (['submit', 'reset'].includes(ty)) named = true; // the browser names these
        else if (ty === 'button' && (el.value || '').trim()) named = true;
        else if (ty === 'image' && (el.getAttribute('alt') || el.value || '').trim()) named = true;
        else if ((el.getAttribute('placeholder') || '').trim()) named = true;
      } else if (t.toLowerCase() === 'svg') {
        named = !!(el.querySelector('title')?.textContent || '').trim();
      } else {
        named = hasContentName(el);
      }
      if (named) continue;
      const it = info(el);
      it.measured = { role, name: '' };
      items.push(it);
    }
    return { items };
  });

  // --- health.fonts (what the page itself says) -----------------------------------------------
  run('health.fonts', () => {
    const items = [];
    const faces = document.fonts ? [...document.fonts] : [];
    for (const f of faces) {
      if (f.status !== 'error') continue;
      items.push({ family: f.family.replace(/^["']|["']$/g, ''), weight: f.weight, style: f.style });
    }
    return { items, status: document.fonts ? document.fonts.status : 'unsupported', faces: faces.length };
  });

  // --- health.images --------------------------------------------------------------------------
  run('health.images', () => {
    const items = [];
    const imgs = [...document.images].filter((im) => shown(im) && !hiddenVisually(im) && !ignored(im));
    // Does the page reserve the box before the image loads? Clone without a source, measure, remove.
    const probes = [];
    for (const im of imgs) {
      const r = im.getBoundingClientRect();
      if (r.width < 16 || r.height < 16) continue;
      if (im.hasAttribute('width') && im.hasAttribute('height')) continue;
      if (cs(im).aspectRatio && cs(im).aspectRatio !== 'auto') continue;
      const c = im.cloneNode(false);
      for (const a of ['src', 'srcset', 'sizes', 'loading', 'id']) c.removeAttribute(a);
      c.setAttribute('alt', '');
      c.setAttribute('aria-hidden', 'true');
      probes.push([im, c, r]);
    }
    for (const [im, c] of probes) im.after(c);
    const sizes = probes.map(([, c]) => c.getBoundingClientRect());
    for (const [, c] of probes) c.remove();
    probes.forEach(([im, , r], i) => {
      const q = sizes[i];
      if (q.width >= 1 && q.height >= 1) return;
      const it = info(im);
      it.problem = 'no_dimensions';
      it.measured = { rendered: [r2(r.width), r2(r.height)], reserved: [r2(q.width), r2(q.height)], src: cap(im.currentSrc, 120) };
      items.push(it);
    });
    const dpr = window.devicePixelRatio || 1;
    for (const im of imgs) {
      if (!im.complete || !im.naturalWidth) continue;
      const src = im.currentSrc || im.src || '';
      if (/\.svg(\?|#|$)/i.test(src) || src.startsWith('data:image/svg')) continue;
      const r = im.getBoundingClientRect();
      if (r.width < 48 || r.height < 48) continue;
      const fx = im.naturalWidth / (r.width * dpr), fy = im.naturalHeight / (r.height * dpr);
      const f = Math.min(fx, fy);
      // Retina practice serves 2x the rendered size at the screen's scale: flag from 3x of it.
      if (f >= (opts.oversize || 3) && im.naturalWidth >= 400) {
        const it = info(im);
        it.problem = 'oversized';
        it.measured = { natural: [im.naturalWidth, im.naturalHeight], rendered: [r2(r.width), r2(r.height)], dpr, factor: r2(f), src: cap(src, 120) };
        items.push(it);
      }
    }
    // Lazy loading on the image most likely to be the largest paint (the perf pass checks the real one).
    let largest = null, la = 0;
    for (const im of imgs) {
      const r = im.getBoundingClientRect();
      if (r.top >= vh || r.bottom <= 0) continue;
      const a = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
      if (a > la) { la = a; largest = im; }
    }
    let lazyGuess = null;
    if (largest && largest.loading === 'lazy' && la >= 0.1 * vw * vh) {
      lazyGuess = info(largest);
      lazyGuess.problem = 'lazy_largest';
      lazyGuess.measured = { loading: 'lazy', visible_area_share: r2(la / (vw * vh)), src: cap(largest.currentSrc, 120) };
    }
    return { items, lazy_guess: lazyGuess, images: imgs.length };
  });

  return out;
}
