// The pixel route for contrast. Four screenshots of the same clip, taken by Playwright:
//   R: the page as it is
//   T: every glyph transparent (what is under the text)
//   K, W: every glyph forced black, then white (where the glyphs are, and how much they cover)
// Coverage per pixel is |W - K| / 255. The background is T at the glyph cores (coverage near its
// maximum), so pictures, gradients, blurs and layers under the text are all included.
// The text colour comes from CSS (the job's `fg`, with its alpha and the opacity above it), not from
// R: at the core of small text R is still a blend of text and background (anti-aliasing), which reads
// far lower than the real contrast. R is used to check the CSS colour: R predicted from it is
// T + coverage * alpha * (fg - T). When R disagrees wildly, something else paints the text (a filter
// or a blend mode); then R is read only where the strokes are solid, else the text is not judged.
// The images are decoded off-DOM (createImageBitmap on an OffscreenCanvas); nothing is drawn in the page.
async (args) => {
  const decode = async (b64) => {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const bmp = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
    const c = new OffscreenCanvas(bmp.width, bmp.height);
    const g = c.getContext('2d', { willReadFrequently: true });
    g.drawImage(bmp, 0, 0);
    return { w: bmp.width, h: bmp.height, d: g.getImageData(0, 0, bmp.width, bmp.height).data };
  };
  const [R, T, K, W] = await Promise.all([args.R, args.T, args.K, args.W].map(decode));
  const lin = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  const lum = (d, i) => 0.2126 * lin(d[i]) + 0.7152 * lin(d[i + 1]) + 0.0722 * lin(d[i + 2]);
  const hex = (d, i) => '#' + [d[i], d[i + 1], d[i + 2]].map((v) => v.toString(16).padStart(2, '0')).join('');
  const sx = R.w / args.clip.w, sy = R.h / args.clip.h;
  const results = [];
  for (const job of args.jobs) {
    const idx = [];
    let maxc = 0;
    const cov = [];
    for (const q of job.rects) {
      const x0 = Math.max(0, Math.floor(q.x * sx)), y0 = Math.max(0, Math.floor(q.y * sy));
      const x1 = Math.min(R.w, Math.ceil((q.x + q.w) * sx)), y1 = Math.min(R.h, Math.ceil((q.y + q.h) * sy));
      for (let y = y0; y < y1; y++) {
        for (let x = x0; x < x1; x++) {
          const i = (y * R.w + x) * 4;
          const c = (Math.abs(W.d[i] - K.d[i]) + Math.abs(W.d[i + 1] - K.d[i + 1]) + Math.abs(W.d[i + 2] - K.d[i + 2])) / 765;
          if (c < 0.2) continue;
          idx.push(i); cov.push(c);
          if (c > maxc) maxc = c;
        }
      }
    }
    const fg = job.fg || null;           // [r, g, b, a] from CSS, a in 0..1
    const alpha = fg ? (fg[3] === undefined ? 1 : fg[3]) : 1;
    const ratios = [];
    let worst = Infinity, worstAt = -1, worstFg = null;
    let bgMin = Infinity, bgMax = -Infinity, bgMinAt = -1, bgMaxAt = -1;
    let err = 0, cores = 0;
    const cr = (lf, lb) => (Math.max(lf, lb) + 0.05) / (Math.min(lf, lb) + 0.05);
    const linv = (v) => lin(Math.max(0, Math.min(255, v)));
    const lumc = (c) => 0.2126 * linv(c[0]) + 0.7152 * linv(c[1]) + 0.0722 * linv(c[2]);
    const pix = [];
    for (let k = 0; k < idx.length; k++) {
      if (cov[k] < 0.85 * maxc) continue;
      const i = idx[k];
      cores++;
      const lb = lum(T.d, i);
      if (lb < bgMin) { bgMin = lb; bgMinAt = i; }
      if (lb > bgMax) { bgMax = lb; bgMaxAt = i; }
      if (fg) {
        // what CSS says is painted at this pixel, and at full coverage
        let e = 0;
        const full = [0, 1, 2].map((ch) => T.d[i + ch] + alpha * (fg[ch] - T.d[i + ch]) * (job.opacity === undefined ? 1 : job.opacity));
        for (let ch = 0; ch < 3; ch++) {
          const pred = T.d[i + ch] + cov[k] * alpha * (fg[ch] - T.d[i + ch]);
          e += Math.abs(R.d[i + ch] - pred);
        }
        err += e / 765;
        pix.push({ i, full, lb });
      } else pix.push({ i, full: null, lb });
    }
    if (cores < 4) { results.push({ id: job.id, judged: false, why: 'glyphs not found in the screenshot' }); continue; }
    const disagree = fg ? err / cores : 1;
    let from = 'css';
    if (fg && job.trust_css && disagree > (args.max_mismatch || 0.12)) {
      // The screenshot is not the text CSS describes: it moved or changed while it was measured (a
      // live list), or a layer paints over it. Neither reading can be trusted.
      results.push({ id: job.id, judged: false, why: 'the screenshot does not match the text colour CSS gives (the page changed while it was measured, or a layer paints over the text)' });
      continue;
    }
    if (!fg || !job.trust_css) {
      // A filter or blend mode: the CSS colour is not what is painted. Read R where the strokes are solid.
      if (maxc < 0.9) {
        results.push({ id: job.id, judged: false, why: 'a filter or blend mode changes the text colour, and the strokes are too thin to read it from pixels' });
        continue;
      }
      from = 'pixels';
    }
    for (const p of pix) {
      const lf = from === 'css' ? lumc(p.full) : lum(R.d, p.i);
      const ratio = cr(lf, p.lb);
      ratios.push(ratio);
      if (ratio < worst) { worst = ratio; worstAt = p.i; worstFg = from === 'css' ? p.full : null; }
    }
    ratios.sort((a, b) => a - b);
    const below = ratios.filter((r) => r < job.min).length / ratios.length;
    const hexc = (c) => '#' + c.map((v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, '0')).join('');
    results.push({
      id: job.id, judged: true, core_pixels: ratios.length, fg_from: from, css_mismatch: Math.round(disagree * 1000) / 1000,
      worst: Math.round(worst * 100) / 100,
      p10: Math.round(ratios[Math.floor(ratios.length * 0.1)] * 100) / 100,
      median: Math.round(ratios[Math.floor(ratios.length / 2)] * 100) / 100,
      share_below: Math.round(below * 1000) / 1000,
      fg_at_worst: worstFg ? hexc(worstFg) : hex(R.d, worstAt), bg_at_worst: hex(T.d, worstAt),
      bg_darkest: hex(T.d, bgMinAt), bg_lightest: hex(T.d, bgMaxAt),
    });
  }
  return results;
}
