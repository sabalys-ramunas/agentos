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
  const [allShifts, allTasks, frames, lcp] = await Promise.all([
    read('layout-shift'), read('longtask'), read('long-animation-frame'), read('largest-contentful-paint'),
  ]);
  // Studio's own full-page screenshot grows the viewport to the page's height for a moment, and a
  // layout sized to the viewport (100vh) moves: what happened from then on is studio's doing.
  const cut = typeof window.__agentosStudioCaptureAt === 'number' ? window.__agentosStudioCaptureAt : Infinity;
  const shifts = allShifts && allShifts.filter((e) => e.startTime < cut);
  const tasks = allTasks && allTasks.filter((e) => e.startTime < cut);
  const res = { supported: { layout_shift: shifts !== null, long_tasks: tasks !== null, lcp: lcp !== null },
    after_capture: allShifts ? allShifts.length - shifts.length : 0 };
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
    for (const [, b] of window.__agentosStudioFontSpans || []) spans.push([0, b === null ? Infinity : b]);
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
