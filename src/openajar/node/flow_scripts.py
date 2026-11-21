"""The read-only scripts the flow driver runs in the page: element paths in the composed DOM tree
(shadow roots included), roles and names, element state and the ancestors a recorded locator
anchors on. They never mark or change the page.
"""

from __future__ import annotations

# ---------------------------------------------------------------- browser scripts (read only)

# Path of an element in the composed tree: child indexes from the document down, with -1 for
# a step into a shadow root. Stable while the DOM does not change; never stored on the page.
_PATH_FN = """
const pathOf = (el) => {
  const p = [];
  let n = el;
  while (n) {
    const parent = n.parentNode;
    if (!parent) break;
    p.push(Array.prototype.indexOf.call(parent.childNodes, n));
    if (parent.nodeType === 11 && parent.host) { p.push(-1); n = parent.host; } else { n = parent; }
  }
  return p.reverse().join('/');
};
"""

PATHS_JS = "(els) => { " + _PATH_FN + " return els.map(pathOf); }"
ATTR_ROWS_JS = "(els, name) => { " + _PATH_FN + " return els.map((e) => [pathOf(e), e.getAttribute(name)]); }"
# The same rows up to a limit, read in the page so that the rest never cross to the node.
LIMITED_ROWS_JS = "(els, {limit, chars, name}) => { " + _PATH_FN + """
  const rows = [];
  let size = 0;
  for (const el of els) {
    if (rows.length >= limit || size >= chars) break;
    const row = [pathOf(el), name ? el.getAttribute(name) : null];
    size += row[0].length + (row[1] || '').length;
    rows.push(row);
  }
  return rows;
}"""
PATH_JS = "(el) => { " + _PATH_FN + " return pathOf(el); }"

# The ARIA role of an element found by CSS: the explicit role, else the implicit one for the tags
# that matter as anchors and targets (role-matched elements take the role from Playwright).
ROLE_JS = """
(el) => {
  const explicit = (el.getAttribute('role') || '').trim().split(/\\s+/)[0];
  if (explicit) return explicit.toLowerCase();
  const tag = el.localName;
  const named = () => el.hasAttribute('aria-label') || el.hasAttribute('aria-labelledby') || el.hasAttribute('title');
  const sectioned = () => !!(el.parentElement && el.parentElement.closest('article, aside, main, nav, section'));
  switch (tag) {
    case 'a': case 'area': return el.hasAttribute('href') ? 'link' : '';
    case 'article': return 'article';
    case 'aside': return 'complementary';
    case 'button': return 'button';
    case 'dialog': return 'dialog';
    case 'details': case 'fieldset': case 'optgroup': return 'group';
    case 'footer': return sectioned() ? '' : 'contentinfo';
    case 'header': return sectioned() ? '' : 'banner';
    case 'form': return named() ? 'form' : '';
    case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6': return 'heading';
    case 'img': return el.getAttribute('alt') === '' ? 'presentation' : 'img';
    case 'input': {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (['button', 'submit', 'reset', 'image'].includes(t)) return 'button';
      if (t === 'checkbox' || t === 'radio') return t;
      if (t === 'range') return 'slider';
      if (t === 'number') return 'spinbutton';
      if (t === 'search') return el.hasAttribute('list') ? 'combobox' : 'searchbox';
      if (['email', 'tel', 'text', 'url', ''].includes(t)) return el.hasAttribute('list') ? 'combobox' : 'textbox';
      return '';
    }
    case 'li': return 'listitem';
    case 'main': return 'main';
    case 'nav': return 'navigation';
    case 'ol': case 'ul': case 'menu': return 'list';
    case 'option': return 'option';
    case 'p': return 'paragraph';
    case 'progress': return 'progressbar';
    case 'search': return 'search';
    case 'section': return named() ? 'region' : '';
    case 'select': return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
    case 'table': return 'table';
    case 'tr': return 'row';
    case 'td': return 'cell';
    case 'th': return 'columnheader';
    case 'textarea': return 'textbox';
    default: return el.isContentEditable ? 'textbox' : 'generic';
  }
}
"""

# The target's own state. `exclude` holds the paths of nested elements with the target's role:
# their text is not the target's. Password values are never read, only their length.
STATE_JS = """
(el, {exclude, maxText, maxAttrs}) => {
  """ + _PATH_FN + """
  const path = pathOf(el);
  const skip = new Set(exclude);
  const parts = [];
  const lineParts = [];   // the same text with its line breaks: br, block edges, pre-* newlines
  let size = 0;
  const push = (t, l) => { if (size < maxText) { parts.push(t); lineParts.push(l); size += t.length; } };
  const keepsBreaks = (st) => st.whiteSpace.startsWith('pre') || st.whiteSpace === 'break-spaces';
  const walk = (node, nodePath, visible, pre) => {
    const kids = node.childNodes;
    for (let i = 0; i < kids.length && size < maxText; i++) {
      const c = kids[i];
      if (c.nodeType === 3) { if (visible) push(c.data, pre ? c.data : c.data.replace(/\\s+/g, ' ')); continue; }
      if (c.nodeType !== 1) continue;
      const tag = c.localName;
      if (['script', 'style', 'template', 'noscript', 'textarea'].includes(tag)) continue;
      const cp = nodePath + '/' + i;
      if (skip.has(cp)) { push(' ', ' '); continue; }
      const st = getComputedStyle(c);
      if (st.display === 'none') continue;
      const block = tag === 'br' || !st.display.startsWith('inline');
      if (block) push(' ', '\\n');
      walk(c, cp, st.visibility === 'visible', keepsBreaks(st));
      if (block && tag !== 'br') push(' ', '\\n');
    }
    if (!kids.length && node.shadowRoot) walk(node.shadowRoot, nodePath + '/-1', visible, pre);
  };
  const rootStyle = getComputedStyle(el);
  walk(el, path, rootStyle.visibility === 'visible', keepsBreaks(rootStyle));
  const text = parts.join('').replace(/\\s+/g, ' ').trim();
  const lines = lineParts.join('');
  const attrs = {};
  for (const a of Array.from(el.attributes).slice(0, maxAttrs)) attrs[a.name] = a.value.slice(0, 500);
  const tag = el.localName;
  const type = (el.getAttribute('type') || '').toLowerCase();
  let value = null;
  let sensitive = false;
  if (tag === 'input') {
    if (type === 'password') { sensitive = true; attrs['value_length'] = String(el.value.length); }
    else if (type === 'file') value = Array.from(el.files || [], (f) => f.name).join(', ');
    else value = el.value;
    if (type === 'checkbox' || type === 'radio') attrs['checked'] = String(el.checked);
  } else if (tag === 'textarea') {
    value = el.value;
  } else if (tag === 'select') {
    value = Array.from(el.selectedOptions, (o) => (o.label || o.textContent || '').trim()).join(', ');
  } else if (el.isContentEditable) {
    value = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
  } else {
    value = el.getAttribute('value');
  }
  if (el.matches(':disabled')) attrs['disabled'] = 'true';
  return {path, text, lines, value, attrs, sensitive};
}
"""

_IS_CONNECTED_JS = "(el) => el.isConnected"

_UPLOAD_KIND_JS = """
(el) => {
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return {
    fileInput: el.localName === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'file',
    visible: r.width >= 2 && r.height >= 2 && st.visibility === 'visible' && st.display !== 'none',
  };
}
"""

# Anchor-capable ancestors of an element, nearest first, with their name from the author
# (aria-labelledby, aria-label, title): the only names these container roles can have.
_ANCESTORS_JS = """
(el, roles) => {
  """ + _PATH_FN + """
  const roleOf = """ + ROLE_JS.strip() + """;
  const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();
  const out = [];
  let n = el.parentElement || (el.parentNode && el.parentNode.host);
  while (n && n.nodeType === 1 && n !== document.documentElement && n !== document.body) {
    const role = roleOf(n);
    if (roles.includes(role)) {
      let name = '';
      const ids = n.getAttribute('aria-labelledby');
      if (ids) {
        const root = n.getRootNode();
        name = clean(ids.split(/\\s+/).map((id) => {
          const ref = root.getElementById ? root.getElementById(id) : document.getElementById(id);
          return ref ? ref.textContent : '';
        }).join(' '));
      }
      if (!name) name = clean(n.getAttribute('aria-label'));
      if (!name) name = clean(n.getAttribute('title'));
      out.push({role, name, path: pathOf(n)});
    }
    n = n.parentElement || (n.parentNode && n.parentNode.host);
  }
  return out;
}
"""


_STABLE_ATTRS_JS = """
(el) => ({id: el.id || '', testid: el.getAttribute('data-testid') || '', name: el.getAttribute('name') || '',
          tag: el.localName})
"""
