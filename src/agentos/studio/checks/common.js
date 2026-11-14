// Helpers shared by the in-page scripts: how an element is named, grouped and found again.
// Pasted in place of the COMMON marker inside each script by agentos.studio.checks.scripts.
  const cap = (s, n = 80) => {
    s = (s || '').replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  };

  const component = (el) => {
    const root = el.closest('[data-component],[data-slot]');
    if (!root) {
      const src = el.closest('[data-source]');
      return src ? { name: null, source: src.getAttribute('data-source'), root: null } : null;
    }
    const name = root.getAttribute('data-component') || root.getAttribute('data-slot');
    const srcEl = el.closest('[data-source]');
    const source = srcEl && (srcEl === root || root.contains(srcEl)) ? srcEl.getAttribute('data-source')
      : root.getAttribute('data-source');
    return { name, source: source || null, root };
  };

  // An id that names a thing, not a place in a list: "row-5", "item3" and generated ids change when
  // rows are added or reordered, so they never anchor a finding.
  const stableId = (id) => id && /^[A-Za-z][\w-]*$/.test(id) && !/\d{3,}|^:r|[a-f0-9]{8,}|\d$/i.test(id);
  const seg = (el) => {
    let s = el.tagName.toLowerCase();
    const slot = el.getAttribute('data-slot');
    const tid = el.getAttribute('data-testid');
    if (tid) s += `[data-testid="${tid}"]`;
    else if (slot) s += `[data-slot="${slot}"]`;
    else if (el.getAttribute('role')) s += `[role="${el.getAttribute('role')}"]`;
    return s;
  };
  // Where a finding is, without positions: the same for every repeat of an element (one finding
  // per cause) and the same across runs while the structure stays.
  const groupKey = (el, comp) => {
    const parts = [];
    let cur = el;
    const stop = comp && comp.root ? comp.root : null;
    while (cur && cur !== document.body && cur !== document.documentElement && parts.length < 8) {
      if (cur === stop) { parts.push(`<${comp.name}>`); break; }
      if (stableId(cur.id)) { parts.push('#' + cur.id); break; }
      parts.push(seg(cur));
      cur = cur.parentElement;
    }
    return parts.reverse().join(' > ');
  };
  // A selector for this one element.
  const cssEsc = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s);
  const selector = (el) => {
    if (el === document.body) return 'body';
    if (el === document.documentElement) return 'html';
    const parts = [];
    let cur = el;
    while (cur && cur !== document.body && cur !== document.documentElement) {
      if (cur.id && document.querySelectorAll('#' + cssEsc(cur.id)).length === 1) {
        parts.push('#' + cssEsc(cur.id));
        return parts.reverse().join(' > ');
      }
      const tid = cur.getAttribute('data-testid');
      if (tid && document.querySelectorAll(`[data-testid="${tid}"]`).length === 1) {
        parts.push(`[data-testid="${tid}"]`);
        return parts.reverse().join(' > ');
      }
      const tag = cur.tagName.toLowerCase();
      const par = cur.parentElement;
      if (par) {
        const same = [...par.children].filter((c) => c.tagName === cur.tagName);
        parts.push(same.length > 1 ? `${tag}:nth-of-type(${same.indexOf(cur) + 1})` : tag);
      } else parts.push(tag);
      cur = par;
    }
    parts.push('body');
    return parts.reverse().join(' > ');
  };

  const roleOf = (el) => {
    const r = el.getAttribute('role');
    if (r) return r;
    const t = el.tagName;
    if (t === 'A' && el.hasAttribute('href')) return 'link';
    if (t === 'BUTTON' || t === 'SUMMARY') return 'button';
    if (t === 'IMG') return 'img';
    if (t === 'SELECT') return 'combobox';
    if (t === 'TEXTAREA') return 'textbox';
    if (/^H[1-6]$/.test(t)) return 'heading';
    if (t === 'INPUT') {
      const ty = (el.type || 'text').toLowerCase();
      if (['checkbox', 'radio', 'range'].includes(ty)) return ty === 'range' ? 'slider' : ty;
      if (['button', 'submit', 'reset', 'image'].includes(ty)) return 'button';
      return 'textbox';
    }
    return null;
  };

  const ownTextNodes = (el) => {
    const res = [];
    for (const n of el.childNodes) if (n.nodeType === 3 && /\S/.test(n.data)) res.push(n);
    return res;
  };
  const ownText = (el) => ownTextNodes(el).map((n) => n.data).join(' ');

  // Where on the page an element is, to group findings when the page has no component markers
  // (data-component, data-slot): the nearest landmark or titled section ("section Tables"), else
  // the nearest class that names a part ("ncard"). Shown to the reader only; never part of an id,
  // so a heading's text or a class can change without changing the finding.
  const SECTIONING = { HEADER: 'header', NAV: 'nav', MAIN: 'main', ASIDE: 'aside', FOOTER: 'footer', SECTION: 'section',
    ARTICLE: 'article', FORM: 'form', DIALOG: 'dialog', FIELDSET: 'fieldset', TABLE: 'table', FIGURE: 'figure' };
  const ROLE_AREAS = new Set(['banner', 'navigation', 'main', 'complementary', 'contentinfo', 'region', 'dialog',
    'alertdialog', 'search', 'form', 'tabpanel', 'table', 'grid', 'list', 'listbox', 'menu', 'toolbar', 'tablist']);
  const UTILITY = /^(?:flex|grid|block|inline|hidden|contents|relative|absolute|fixed|sticky|static|truncate|container|clearfix|sr-only|visually-hidden|row|col|stack|wrap|wrapper|inner|outer|content|body|item|grow|shrink|trunc|mono|num|fit|small|muted|faint|tabular|caption|label|sub|clickable|active|selected|open|current|disabled|checked|focus|hover|dark|light|on|off|(?:is|has|js|u|t|p|m|px|py|pt|pb|pl|pr|mx|my|mt|mb|ml|mr|w|h|min|max|gap|space|text|font|leading|tracking|bg|border|rounded|shadow|opacity|z|items|justify|self|order|col|row|overflow|top|left|right|bottom|inset|ring|fill|stroke|cursor|select|pointer|transition|duration|ease|animate|transform|scale|rotate|translate|origin|sm|md|lg|xl|hide|show|only)(?:-.*)?)$/i;
  const partClass = (el) => {
    for (const c of el.classList || []) {
      if (c.length < 3 || c.length > 32 || !/^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$/i.test(c) || UTILITY.test(c)) continue;
      return c;
    }
    return null;
  };
  const headingIn = (sec) => {
    for (const h of sec.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading],caption,legend,figcaption')) {
      let owner = h.parentElement;
      while (owner && owner !== sec && !(SECTIONING[owner.tagName] || ROLE_AREAS.has(owner.getAttribute('role')))) owner = owner.parentElement;
      if (owner === sec) { const t = (h.textContent || '').replace(/\s+/g, ' ').trim(); if (t) return t; }
    }
    return null;
  };
  const areaOf = (el) => {
    // an element's own class is most often its style (hint, trunc); a container's names the part
    let cls = null;
    const own = partClass(el);
    for (let cur = el; cur && cur !== document.body && cur !== document.documentElement; cur = cur.parentElement) {
      if (!cls && cur !== el) cls = partClass(cur);
      const role = cur.getAttribute('role');
      const kind = role && ROLE_AREAS.has(role) ? role : SECTIONING[cur.tagName];
      if (!kind) continue;
      let name = cur.getAttribute('aria-label');
      if (!name && cur.getAttribute('aria-labelledby')) {
        name = cur.getAttribute('aria-labelledby').split(/\s+/).map((i) => document.getElementById(i))
          .filter(Boolean).map((n) => n.textContent).join(' ');
      }
      const how = name ? 'landmark' : 'heading';
      name = (name || headingIn(cur) || '').replace(/\s+/g, ' ').trim();
      if (name) return { name: `${kind} "${cap(name, 40)}"`, how };
    }
    cls = cls || own;
    return cls ? { name: '.' + cls, how: 'class' } : null;
  };

