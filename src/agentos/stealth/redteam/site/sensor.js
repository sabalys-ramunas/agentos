// The red-team sensor: what a commercial anti-bot script records on a page, sent to /collect.
// Every input event (with its timing against the animation frame), the window's focus, and on the
// landing page a few active tests (real CPU parallelism, GPU speed, frame rate) that a spoofed
// fingerprint cannot answer by changing a getter. Runs in the page's own world, as a vendor's would.
(() => {
  const pageId = Math.random().toString(36).slice(2, 10);
  const t0 = performance.now();
  const events = [];
  let sent = 0, seq = 0, lastRaf = 0, frames = [];
  const raf = (t) => { if (frames.length < 400) frames.push(t); lastRaf = t; requestAnimationFrame(raf); };
  requestAnimationFrame(raf);

  const idOf = (el) => {
    if (!el || !el.tagName) return null;
    return el.id || el.getAttribute("name") || el.getAttribute("data-rt") || el.tagName.toLowerCase();
  };
  const rec = (e) => {
    const now = performance.now();
    const o = { t: e.type, ts: Math.round(e.timeStamp * 10) / 10, raf: Math.round((now - lastRaf) * 10) / 10,
                tr: e.isTrusted, f: document.hasFocus(), v: document.visibilityState === "visible" };
    if ("clientX" in e) {
      o.x = e.clientX; o.y = e.clientY; o.sx = e.screenX; o.sy = e.screenY;
      o.mx = e.movementX; o.my = e.movementY; o.b = e.buttons;
      if (e.pointerType !== undefined) { o.pt = e.pointerType; o.pr = e.pressure; }
    }
    if (e.type === "wheel") { o.dx = e.deltaX; o.dy = e.deltaY; o.dm = e.deltaMode; }
    if (e.touches !== undefined) {  // a finger has a size and a force; injected touches often have neither
      const t0 = (e.changedTouches && e.changedTouches[0]) || {};
      Object.assign(o, { x: t0.clientX, y: t0.clientY, rx: t0.radiusX, ry: t0.radiusY, fo: t0.force, ra: t0.rotationAngle,
                         nt: e.touches.length });
    }
    if (e.type.startsWith("key")) {
      o.k = e.key.length === 1 ? (/[a-z]/i.test(e.key) ? (e.key === e.key.toUpperCase() ? "A" : "a") : "c") : e.key;
      o.c = e.code; o.sh = e.shiftKey; o.cl = e.getModifierState && e.getModifierState("CapsLock");
      o.rep = e.repeat; o.ch = e.key.length === 1 ? e.key.codePointAt(0) : null;
    }
    if (e.type === "input" || e.type === "beforeinput") {
      o.it = e.inputType; o.ch = e.data ? e.data.codePointAt(0) : null; o.n = e.data ? [...e.data].length : 0;
      o.comp = e.isComposing;
    }
    if (e.type === "scroll") { o.sy2 = Math.round(scrollY); o.tg = e.target === document ? "doc" : idOf(e.target); }
    else if (e.target && e.target !== window && e.target !== document) o.tg = idOf(e.target);
    events.push(o);
  };
  for (const type of ["mousemove", "mousedown", "mouseup", "click", "mouseover", "wheel", "keydown", "keyup",
                      "beforeinput", "input", "pointerdown", "pointerup", "touchstart", "touchmove", "touchend", "paste", "compositionstart",
                      "compositionend", "contextmenu", "dblclick", "selectstart"]) {
    addEventListener(type, rec, { capture: true, passive: true });
  }
  document.addEventListener("scroll", rec, { capture: true, passive: true });
  // A real mouse reports faster than the screen refreshes, so Firefox merges several moves into one
  // pointermove (getCoalescedEvents) and pointerrawupdate fires more often than pointermove. Moves a
  // driver injects as synthesized events are never merged.
  const pointer = { moves: 0, merged: 0, raw: 0 };
  addEventListener("pointermove", (e) => { pointer.moves++;
    if (e.getCoalescedEvents && e.getCoalescedEvents().length > 1) pointer.merged++; }, { capture: true, passive: true });
  addEventListener("pointerrawupdate", () => { pointer.raw++; }, { capture: true, passive: true });
  addEventListener("focus", (e) => { if (e.target === window) rec(e); }, true);
  addEventListener("blur", (e) => { if (e.target === window) rec(e); }, true);
  document.addEventListener("visibilitychange", rec);

  const nav = (performance.getEntriesByType("navigation")[0] || {});
  const meta = { page: pageId, path: location.pathname + location.search, referrer: document.referrer,
                 navType: nav.type || null, focusAtLoad: document.hasFocus(), visibleAtLoad: document.visibilityState };
  const env = {};

  const flush = (final) => {
    const batch = events.slice(sent);
    env.pointer = pointer;
    const body = JSON.stringify({ page: pageId, seq: seq++, final: !!final, meta, env,
                                  frames: final || seq % 5 === 0 ? frames : undefined, events: batch,
                                  elapsed: Math.round(performance.now() - t0) });
    sent = events.length;
    if (final && navigator.sendBeacon) navigator.sendBeacon("/collect", new Blob([body], { type: "text/plain" }));
    else fetch("/collect", { method: "POST", body, keepalive: true, headers: { "content-type": "text/plain" } })
      .catch(() => {});
  };
  setInterval(() => { if (events.length > sent) flush(false); }, 1500);
  addEventListener("pagehide", () => flush(true));
  window.__rtFlush = () => flush(true);

  const navi = navigator;
  Object.assign(env, {
    userAgent: navi.userAgent, platform: navi.platform, language: navi.language, languages: navi.languages,
    cores: navi.hardwareConcurrency, webdriver: navi.webdriver, timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    screen: [screen.width, screen.height, screen.availWidth, screen.availHeight], dpr: devicePixelRatio,
    inner: [innerWidth, innerHeight], outer: [outerWidth, outerHeight],
  });

  if (!document.body.hasAttribute("data-deep")) return;

  // Active tests: a fingerprint can lie in a getter, not in how fast the machine is.
  const work = `self.onmessage = (e) => { const t = performance.now(); let x = 0;
    for (let i = 0; i < e.data; i++) x = (x + Math.sqrt(i) * 1.0000001) % 1e9;
    postMessage([performance.now() - t, x]); };`;
  const url = URL.createObjectURL(new Blob([work], { type: "text/javascript" }));
  const runWorkers = (n, iters) => new Promise((resolve) => {
    const ws = [], times = []; let done = 0; const start = performance.now();
    for (let i = 0; i < n; i++) {
      const w = new Worker(url); ws.push(w);
      w.onmessage = (e) => { times.push(e.data[0]); if (++done === n) { ws.forEach((x) => x.terminate());
        resolve(performance.now() - start); } };
      w.postMessage(iters);
    }
  });
  const cpu = async () => {
    const claimed = Math.max(1, Math.min(navi.hardwareConcurrency || 1, 16));
    const iters = 6e6;
    await runWorkers(1, iters);                        // warm the JIT
    const one = await runWorkers(1, iters);
    const many = await runWorkers(claimed, iters);
    return { claimed, oneMs: Math.round(one), manyMs: Math.round(many),
             effective: Math.round((claimed * one / many) * 100) / 100 };
  };
  const gpu = () => {
    const c = document.createElement("canvas"); c.width = 512; c.height = 512;
    const gl = c.getContext("webgl");
    if (!gl) return { available: false };
    const info = gl.getExtension("WEBGL_debug_renderer_info");
    const renderer = info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
    const p = gl.createProgram();
    gl.attachShader(p, sh(gl.VERTEX_SHADER, "attribute vec2 a; void main(){ gl_Position = vec4(a, 0., 1.); }"));
    gl.attachShader(p, sh(gl.FRAGMENT_SHADER, `precision highp float; uniform float u;
      void main(){ vec2 q = gl_FragCoord.xy / 512.; float v = 0.;
        for (int i = 0; i < 64; i++) { v += sin(q.x * float(i) + u) * cos(q.y * float(i) - u); }
        gl_FragColor = vec4(fract(v), q, 1.); }`));
    gl.linkProgram(p); gl.useProgram(p);
    const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(p, "a"); gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    const u = gl.getUniformLocation(p, "u"); const px = new Uint8Array(4);
    const draw = (i) => { gl.uniform1f(u, i); gl.drawArrays(gl.TRIANGLES, 0, 3); gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px); };
    draw(0);
    const n = 12, start = performance.now();
    for (let i = 1; i <= n; i++) draw(i);
    return { available: true, renderer, msPerDraw: Math.round((performance.now() - start) / n * 100) / 100 };
  };
  const timer = () => {
    const seen = new Set(); let last = performance.now();
    for (let i = 0; i < 20000 && seen.size < 50; i++) { const t = performance.now(); if (t !== last) { seen.add(Math.round((t - last) * 1000)); last = t; } }
    return Math.min(...seen) / 1000;
  };
  (async () => {
    try { env.gpu = gpu(); } catch (e) { env.gpu = { error: String(e) }; }
    try { env.timerMs = timer(); } catch (e) { env.timerMs = null; }
    try { env.cpu = await cpu(); } catch (e) { env.cpu = { error: String(e) }; }
    env.deepDone = true;
    flush(false);
  })();
})();
