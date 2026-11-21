// Small fetch wrapper for the controller API: JSON in and out, CSRF on changes.
let csrf = null;

export async function session() {
  const res = await fetch('/api/session', { credentials: 'same-origin' });
  if (!res.ok) return null;
  const data = await res.json();
  csrf = data.csrf;
  return data;
}

export async function api(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (method !== 'GET' && csrf) headers['X-Agentos-CSRF'] = csrf;
  const res = await fetch(path, {
    method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = data.error || {};
    throw new Error(`${err.code || res.status}: ${err.message || 'the request failed'}`);
  }
  return data;
}

// Build elements with text only: node-supplied strings never become HTML.
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
