// Lanebench input recorder: the page's own read-only listener (docs/design/browser-and-computer-agents.md 5.6).
// It records pointer moves, presses, wheel and scroll events, key presses (their kind, never the key) and
// text insertions, with times, and posts them to the fixture site. It changes nothing on the page.
(() => {
  const view = Math.random().toString(36).slice(2, 10);
  const origin = performance.timeOrigin;
  const now = () => Math.round((origin + performance.now()) * 10) / 10;
  const at = (e) => Math.round((origin + e.timeStamp) * 10) / 10;  // when the event was made, not handled
  let buf = [];
  const ACT = 'button,a[href],input,textarea,select,label,[role=button],[role=link],[role=checkbox],[role=switch],[role=menuitem]';
  const actionable = (path) => path.find((n) => n && n.nodeType === 1 && n.matches(ACT)) || null;
  const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  const lb = (el) => (el && el.getAttribute('data-lb')) || null;
  const kind = (key) => key.length === 1 ? 'char' : (['Enter', 'Tab', 'Backspace', 'Delete', 'Escape'].includes(key) ? key
    : (['Shift', 'Control', 'Alt', 'Meta'].includes(key) ? 'mod' : (/^(Arrow|Page|Home|End)/.test(key) ? 'nav' : 'other')));
  const editable = (t) => t && t.nodeType === 1 && (t.matches('input,textarea,select') || t.isContentEditable);
  const opts = {capture: true, passive: true};
  // pointermove, with how many moves Firefox folded into it: a busy page gets one event for many
  // points of a glide (n > 1); a teleport is one point (n = 1)
  addEventListener('pointermove', (e) => {
    if (e.pointerType !== 'mouse') return;
    const n = e.getCoalescedEvents ? e.getCoalescedEvents().length : 1;
    buf.push({k: 'move', t: at(e), x: e.clientX, y: e.clientY, n, tr: e.isTrusted});
  }, opts);
  for (const type of ['mousedown', 'mouseup']) {
    addEventListener(type, (e) => {
      const el = actionable(e.composedPath());
      buf.push({k: type === 'mousedown' ? 'down' : 'up', t: at(e), x: e.clientX, y: e.clientY, box: box(el),
                lb: lb(el), tag: el ? el.tagName.toLowerCase() : null, tr: e.isTrusted});
    }, opts);
  }
  // every click: one with no press before it was made by a script (Camoufox marks those trusted).
  // The site's own input.click() for its file picker is not input; window.__lbOwnClick marks it.
  addEventListener('click', (e) => {
    if (!window.__lbOwnClick) buf.push({k: 'click', t: at(e), tr: e.isTrusted, lb: lb(actionable(e.composedPath()))});
  }, opts);
  addEventListener('wheel', (e) => buf.push({k: 'wheel', t: at(e), dy: e.deltaY, tr: e.isTrusted}), opts);
  addEventListener('scroll', (e) => {
    const t = e.target;
    if (editable(t)) return;  // a text area scrolling while it is typed into
    const doc = t === document || t === document.documentElement;
    buf.push({k: 'scroll', t: at(e), doc, y: doc ? scrollY : (t.scrollTop || 0)});
  }, opts);
  addEventListener('keydown', (e) => buf.push({k: 'key', t: at(e), c: kind(e.key), rep: e.repeat, tr: e.isTrusted}), opts);
  addEventListener('input', (e) => buf.push({k: 'input', t: at(e), it: e.inputType || '', tr: e.isTrusted}), opts);
  const send = (beacon) => {
    if (!buf.length) return;
    const body = JSON.stringify({view, path: location.pathname, events: buf});
    buf = [];
    if (beacon && navigator.sendBeacon) navigator.sendBeacon('/__lb/input', body);
    else fetch('/__lb/input', {method: 'POST', body, keepalive: true, headers: {'Content-Type': 'application/json'}}).catch(() => {});
  };
  // how late this page runs its own timers: a page that runs late (a background tab) also stamps
  // its events late, so timing measured here is not to be trusted while it lags
  let last = performance.now(), lag = 0;
  setInterval(() => {
    const t = performance.now();
    lag = Math.max(lag, t - last - 250);
    last = t;
    if (lag > 50) { buf.push({k: 'lag', t: now(), ms: Math.round(lag)}); lag = 0; }
    send(false);
  }, 250);
  addEventListener('pagehide', () => send(true));
})();
