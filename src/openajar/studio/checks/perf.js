// Page health from the performance timeline: layout shift (CLS by session window), long tasks and
// the largest contentful paint element. Reads buffered entries only; observes for a short moment.
async (opts) => {
  /*COMMON*/
  const supported = (window.PerformanceObserver && PerformanceObserver.supportedEntryTypes) || [];
  const read = (type) => new Promise((resolve) => {
    if (!supported.includes(type)) { resolve(null); return; }
    const got = [];
    let po;
    try {
      po = new PerformanceObserver((l) => { got.push(...l.getEntries()); });
      po.observe({ type, buffered: true });
    } catch (e) { resolve(null); return; }
    setTimeout(() => { po.disconnect(); resolve(got); }, opts.wait_ms || 60);
  });
  const sel = (el) => (el && el.nodeType === 1 ? selector(el) : null);
  const comp = (el) => {
    const c = el && el.nodeType === 1 ? component(el) : null;
    return c ? { name: c.name, source: c.source } : null;
  };
  const group = (el) => (el && el.nodeType === 1 ? groupKey(el, component(el)) : null);
  // An image on first view that is still loading has no largest-paint entry yet, so the largest
  // paint read now would name something smaller (a lazy image does not hold up the load event, and
  // a slow server or a busy machine delays it more). Wait for those images, up to images_ms, and
  // for the frame that paints them.
  const vw = window.innerWidth, vh = window.innerHeight;
  const onView = [...document.images].filter((im) => {
    const r = im.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.top < vh && r.bottom > 0 && r.left < vw && r.right > 0;
  });
  const loading = onView.filter((im) => !im.complete);
  if (loading.length && opts.images_ms) {
    const settled = (im) => new Promise((resolve) => {
      if (im.complete) { resolve(); return; }
      im.addEventListener('load', resolve, { once: true });
      im.addEventListener('error', resolve, { once: true });
    });
    await Promise.race([Promise.all(loading.map(settled)),
      new Promise((resolve) => setTimeout(resolve, opts.images_ms))]);
    await new Promise((resolve) => {
      const done = setTimeout(resolve, 100);  // no frames in a hidden tab: do not wait for one
      requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(done); resolve(); }));
    });
  }
  // An image that has just loaded gets its largest-paint entry a frame or more later, and a busy
  // machine (or a tab with no frames) can be slower than the two frames above: when a loaded image
  // is bigger than the largest paint so far, wait for its entry, up to paint_ms. That includes one
  // that loaded just before this pass began, which the wait above does not hold for (on a busy CI
  // runner the largest paint was then still the text under a lazy hero). The wait ends at once when
  // the image has its entry, or something as big already holds the largest paint.
  const loaded = onView.filter((im) => im.complete && im.naturalWidth > 0);
  if (loaded.length && opts.paint_ms && supported.includes('largest-contentful-paint')) {
    const seen = (im) => {
      const r = im.getBoundingClientRect();
      return Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    };
    const biggest = Math.max(...loaded.map(seen));
    await new Promise((resolve) => {
      let po = null;
      const finish = () => { clearTimeout(end); if (po) po.disconnect(); resolve(); };
      const end = setTimeout(finish, opts.paint_ms);
      try {
        po = new PerformanceObserver((l) => {
          if (l.getEntries().some((e) => loaded.includes(e.element) || e.size >= biggest * 0.9)) finish();
        });
        po.observe({ type: 'largest-contentful-paint', buffered: true });
      } catch (e) { finish(); }
    });
  }
  const pending = loading.filter((im) => !im.complete).length;
  const [allShifts, allTasks, frames, lcp] = await Promise.all([
    read('layout-shift'), read('longtask'), read('long-animation-frame'), read('largest-contentful-paint'),
  ]);
  // Studio's own full-page screenshot grows the viewport to the page's height for a moment, and a
  // layout sized to the viewport (100vh) moves: what happened from then on is studio's doing.
  const cut = typeof window.__openajarStudioCaptureAt === 'number' ? window.__openajarStudioCaptureAt : Infinity;
  const shifts = allShifts && allShifts.filter((e) => e.startTime < cut);
  const tasks = allTasks && allTasks.filter((e) => e.startTime < cut);
  const res = { supported: { layout_shift: shifts !== null, long_tasks: tasks !== null, lcp: lcp !== null },
    after_capture: allShifts ? allShifts.length - shifts.length : 0, images_pending: pending };
  if (shifts) {
    // CLS: the largest session window (gaps under 1 s, windows at most 5 s), inputs excluded.
    let best = 0, bestEntries = [], cur = 0, curEntries = [], start = 0, last = 0;
    for (const e of shifts) {
      if (e.hadRecentInput) continue;
      if (curEntries.length && (e.startTime - last >= 1000 || e.startTime - start >= 5000)) {
        cur = 0; curEntries = [];
      }
      if (!curEntries.length) start = e.startTime;
      cur += e.value; curEntries.push(e); last = e.startTime;
      if (cur > best) { best = cur; bestEntries = curEntries.slice(); }
    }
    // A shift while web fonts load (from the page's start to just after the last font arrived) is
    // most likely a font swapping in (font-display: swap): it happens on a cold cache and not on a
    // warm one, so it varies between runs. It can also be the page's own work in that time, so the
    // finding says it is a heuristic.
    const FONT_RES = /\.(woff2?|ttf|otf)(\?|#|$)|fonts\.(googleapis|gstatic)\.com|use\.typekit\.net|fonts\.bunny\.net/i;
    const spans = (performance.getEntriesByType ? performance.getEntriesByType('resource') : [])
      .filter((r) => FONT_RES.test(r.name)).map((r) => [r.startTime, r.responseEnd]);
    // the review browser's pins record when document.fonts loaded (fonts a stylesheet of another
    // origin brings in are often missing from resource timing)
    for (const [, b] of window.__openajarStudioFontSpans || []) spans.push([0, b === null ? Infinity : b]);
    let swapValue = 0;
    if (spans.length) {
      const from = Math.min(...spans.map((x) => x[0])), to = Math.max(...spans.map((x) => x[1])) + 400;
      for (const e of bestEntries) if (e.startTime >= from && e.startTime <= to) swapValue += e.value;
    }
    const fontEnds = spans;
    const nodes = new Map();
    for (const e of bestEntries) {
      for (const s of e.sources || []) {
        const n = s.node && s.node.nodeType === 1 ? s.node : s.node && s.node.parentElement;
        if (!n) continue;
        const k = sel(n);
        const prev = nodes.get(k) || { sel: k, group: group(n), comp: comp(n), value: 0, moved_px: 0 };
        prev.value += e.value / Math.max(1, (e.sources || []).length);
        const d = Math.hypot(s.currentRect.x - s.previousRect.x, s.currentRect.y - s.previousRect.y);
        prev.moved_px = Math.max(prev.moved_px, Math.round(d));
        nodes.set(k, prev);
      }
    }
    res.layout_shift = {
      cls: Math.round(best * 10000) / 10000, shifts: shifts.length,
      font_swap_share: best ? Math.round(swapValue / best * 100) / 100 : 0, fonts_loaded: fontEnds.length,
      sources: [...nodes.values()].sort((a, b) => b.value - a.value).slice(0, 5),
    };
  }
  if (tasks) {
    const durs = tasks.map((t) => t.duration);
    const blocking = durs.reduce((s, d) => s + Math.max(0, d - 50), 0);
    const scripts = [];
    for (const f of frames || []) {
      for (const s of f.scripts || []) {
        if (s.duration < 50) continue;
        scripts.push({ source: (s.sourceURL || '').slice(0, 160), invoker: (s.invoker || '').slice(0, 120), ms: Math.round(s.duration) });
      }
    }
    scripts.sort((a, b) => b.ms - a.ms);
    res.long_tasks = {
      count: durs.length, longest_ms: Math.round(Math.max(0, ...durs)), blocking_ms: Math.round(blocking),
      scripts: scripts.slice(0, 5),
    };
  }
  if (lcp && lcp.length) {
    const e = lcp[lcp.length - 1];
    const el = e.element;
    res.lcp = {
      ms: Math.round(e.startTime), sel: sel(el), tag: el ? el.tagName.toLowerCase() : null,
      lazy: !!(el && el.tagName === 'IMG' && el.loading === 'lazy'), url: (e.url || '').slice(0, 160),
      comp: comp(el), group: group(el), size: e.size,
    };
  }
  return res;
}
