"""The scripts the guard runs in a page's isolated world to read what a press can reach."""

from __future__ import annotations

# What a press can reach, read the way the browser resolves it: the composed path (through shadow
# roots and slots), every link (`a` and `area`, HTML or SVG) and submit button on it, a label's
# control, and for a click also the rendered links, submit buttons, labels and frames inside the
# element (a press anywhere on it can land on one of them). A submitter goes to its form owner
# with its formaction, formmethod and formtarget; Enter in a field is implicit submission, through
# the form's default button. Form attributes are read through the prototype, so a control named
# "action" cannot stand in for the form's action. Runs in Camoufox's isolated world: the page's
# scripts cannot patch what it calls. It changes nothing.
_RESOLVE_JS = r"""
  const XLINK = 'http://www.w3.org/1999/xlink', XHTML = 'http://www.w3.org/1999/xhtml';
  const FORM = HTMLFormElement.prototype;
  const prop = (proto, name, obj) => Object.getOwnPropertyDescriptor(proto, name).get.call(obj);
  const shadowOf = (n) => n.openOrClosedShadowRoot || n.shadowRoot || null;
  const up = (n) => n.openOrClosedAssignedSlot || n.assignedSlot || n.parentNode || n.host || null;
  const out = [], seen = new Set(), keys = new Set();
  const push = (t) => {
    const key = [t.kind, t.url, t.method, t.why].join(' ');
    if (!keys.has(key)) { keys.add(key); out.push(t); }
  };
  const isLink = (n) => (n.localName === 'a' || n.localName === 'area') &&
    (n.hasAttribute('href') || n.hasAttributeNS(XLINK, 'href'));
  const linkUrl = (a) => {
    try {
      if (typeof a.href === 'string') return a.href;
      const v = a.href && a.href.baseVal;
      const raw = typeof v === 'string' ? v : (a.getAttribute('href') ?? a.getAttributeNS(XLINK, 'href'));
      return typeof raw === 'string' ? new URL(raw, a.baseURI).href : null;
    } catch (e) { return null; }
  };
  const isSubmitter = (n) => n.namespaceURI === XHTML &&
    ((n.localName === 'button' && n.type === 'submit') ||
     (n.localName === 'input' && (n.type === 'submit' || n.type === 'image')));
  const formOf = (n) => { try { return n.form || null; } catch (e) { return null; } };
  const submit = (form, sub) => {
    try {
      const has = (a) => sub && sub.hasAttribute(a);
      push({kind: 'form', url: has('formaction') ? sub.formAction : prop(FORM, 'action', form),
            method: String((has('formmethod') ? sub.formMethod : prop(FORM, 'method', form)) || 'get').toLowerCase(),
            target: (has('formtarget') ? sub.formTarget : prop(FORM, 'target', form)) || ''});
    } catch (e) { push({kind: 'unreadable', why: 'a form whose action cannot be read'}); }
  };
  const visit = (n) => {
    if (isLink(n)) push({kind: 'link', url: linkUrl(n), target: n.getAttribute('target') || ''});
    if (isSubmitter(n)) { const f = formOf(n); if (f) submit(f, n); }
    if (n.localName === 'label' && n.namespaceURI === XHTML && n.control) chain(n.control);
  };
  const chain = (start) => {
    for (let n = start; n; n = up(n)) {
      if (n.nodeType !== 1 || seen.has(n)) continue;
      seen.add(n);
      visit(n);
    }
  };
  const implicit = (field) => {
    const form = formOf(field);
    if (!form) return;
    const root = form.getRootNode();
    const first = Array.from(root.querySelectorAll('button, input')).find((c) => isSubmitter(c) && formOf(c) === form);
    if (first) submit(form, first);
    submit(form, null);
  };
  const LIMIT = 20000;
  let count = 0;
  const inside = (root) => {
    const todo = [root];
    while (todo.length) {
      const r = todo.pop();
      const all = r.nodeType === 1 ? [r, ...r.querySelectorAll('*')] : Array.from(r.querySelectorAll('*'));
      for (const e of all) {
        if (++count > LIMIT) { push({kind: 'unreadable', why: 'an element too large to read'}); return; }
        const sr = shadowOf(e);
        if (sr) todo.push(sr);
        if (e.getClientRects().length === 0) continue;
        visit(e);
        if (['iframe', 'frame', 'object', 'embed'].includes(e.localName) && e.namespaceURI === XHTML) {
          let doc = null;
          try { doc = e.contentDocument; } catch (err) { doc = null; }
          if (doc && doc.documentElement) todo.push(doc.documentElement);
          else push({kind: 'unreadable', why: 'a frame the guard cannot read'});
        }
        const map = e.localName === 'img' && e.useMap ? e.useMap.replace(/^#/, '') : '';
        if (map) {
          for (const m of e.getRootNode().querySelectorAll('map')) {
            if (m.name === map || m.id === map) for (const a of m.querySelectorAll('area')) if (isLink(a)) visit(a);
          }
        }
      }
    }
  };
  const deepHit = (x, y) => {
    let hit = document.elementFromPoint(x, y);
    while (hit) {
      const sr = shadowOf(hit);
      const inner = sr ? sr.elementFromPoint(x, y) : null;
      if (!inner || inner === hit) break;
      hit = inner;
    }
    return hit;
  };
"""

# For a click or a key press on `el` (mode "click" or "key"): {targets: [{kind: link | form |
# unreadable, url, method, target, why}], framed: is the element inside a frame}.
TARGET_JS = """(el, mode) => {""" + _RESOLVE_JS + """
  chain(el);
  for (const label of el.labels || []) chain(label);
  if (mode === 'key') {
    const tag = el.namespaceURI === XHTML ? el.localName : '';
    if (tag === 'select' || (tag === 'input' && !isSubmitter(el))) implicit(el);
  } else {
    inside(el);
  }
  return {targets: out.length > 500 ? [{kind: 'unreadable', why: 'more links than the guard reads'}] : out,
          framed: window !== window.top};
}"""

# Evaluated on a frame element in its parent document, for points in the parent's viewport: what
# covers each point. {state: "frame", x, y} (the point reaches the frame; x, y inside it),
# {state: "cover", targets} (another element takes the press: what it leads to) or {state: "none"}.
COVER_JS = """(fe, points) => {""" + _RESOLVE_JS + """
  const r = fe.getBoundingClientRect(), cs = getComputedStyle(fe);
  const ox = r.left + fe.clientLeft + parseFloat(cs.paddingLeft || '0');
  const oy = r.top + fe.clientTop + parseFloat(cs.paddingTop || '0');
  return points.map(([x, y]) => {
    const hit = deepHit(x, y);
    if (!hit) return {state: 'none'};
    if (hit === fe) return {state: 'frame', x: x - ox, y: y - oy};
    out.length = 0;
    keys.clear();
    seen.clear();
    chain(hit);
    if (['iframe', 'frame', 'object', 'embed'].includes(hit.localName)) push({kind: 'unreadable', why: 'a frame laid over the target'});
    return {state: 'cover', targets: out.slice()};
  });
}"""

# The press points a check tries on a framed target: the middle of its box, where human.click aims.
COVER_POINTS = (0.3, 0.5, 0.7)
