/*
 * The agentos service worker. Two jobs, and nothing else:
 *
 * 1. An offline page. Only that page and its icon are ever stored. No API response, and nothing
 *    that holds a session, is cached: every request other than a failed page load goes straight
 *    to the network as if this worker were not here.
 * 2. The push notification. No notification approves anything. An approval carries Review, which
 *    opens it in the panel, and Deny, which is one tap but only with the signed-in session and the
 *    content hash the cloud sent. The words on it are the cloud's (what kind of thing, and where);
 *    the node's own summary never reaches a lock screen.
 */
const CACHE = 'agentos-offline-v1';
const OFFLINE = '/offline.html';
const ICON = '/icons/icon-192.png';
const BADGE = '/icons/badge-96.png';

/** Replaced by the mock's worker, which has no network of its own. */
self.agentosFetch = self.agentosFetch || ((input, init) => fetch(input, init));

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll([OFFLINE, ICON]))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  // The mock answers every request itself, page loads included.
  if (self.AGENTOS_MOCK) return;
  const request = event.request;
  if (request.mode !== 'navigate' || request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/') || url.pathname.startsWith('/v1/')) return;
  event.respondWith(fetch(request).catch(() => caches.match(OFFLINE).then((page) => page || Response.error())));
});

/** Only a path on this site. A push can never send the panel somewhere else. */
function safePath(value, fallback) {
  return typeof value === 'string' && /^\/(?![/\\])/.test(value) && !/[\u0000-\u001f\\]/.test(value) ? value : fallback;
}

function text(value, fallback) {
  return typeof value === 'string' && value ? value.slice(0, 200) : fallback;
}

async function showPush(payload) {
  if (!payload || payload.v !== 1 || typeof payload.type !== 'string') {
    // Every push must show something. A shape this worker does not know gets a plain line.
    return self.registration.showNotification('agentos', { body: 'Something needs you.', tag: 'agentos', icon: ICON, badge: BADGE, data: { url: '/' } });
  }
  if (payload.type === 'dismiss') {
    const open = await self.registration.getNotifications({ tag: payload.tag });
    open.forEach((notification) => notification.close());
    return undefined;
  }
  const home = typeof payload.org === 'string' ? `/${encodeURIComponent(payload.org)}` : '/';
  const data = {
    type: payload.type,
    org: payload.org,
    id: payload.id,
    tag: payload.tag,
    body: text(payload.body, ''),
    url: safePath(payload.url, home),
    fallback_url: safePath(payload.fallback_url, home),
    content_hash: payload.type === 'approval' && typeof payload.content_hash === 'string' ? payload.content_hash : null,
    links_expire_at: Number(payload.links_expire_at) || null,
  };
  const actions = [];
  if (payload.type === 'approval' || payload.type === 'batch') actions.push({ action: 'review', title: 'Review' });
  // Deny only with the content hash it is bound to. Questions have none. Never an Approve.
  if (data.content_hash && typeof data.id === 'string') actions.push({ action: 'deny', title: 'Deny' });
  return self.registration.showNotification(text(payload.title, 'agentos'), {
    body: data.body,
    tag: typeof payload.tag === 'string' ? payload.tag : undefined,
    renotify: payload.type !== 'test' && typeof payload.tag === 'string',
    requireInteraction: payload.urgent === true,
    icon: ICON,
    badge: BADGE,
    actions,
    data,
  });
}
self.agentosShowPush = showPush;

self.addEventListener('push', (event) => {
  let payload = null;
  try {
    payload = event.data ? event.data.json() : null;
  } catch {
    payload = null;
  }
  event.waitUntil(showPush(payload));
});

/** Where Review goes: the approval while the link lasts, Needs you after. */
function reviewTarget(data) {
  const fresh = !data.links_expire_at || Date.now() / 1000 < data.links_expire_at;
  return fresh ? data.url : data.fallback_url;
}

/** Focus an open panel and take it there, or open a new window. */
async function openPanel(path) {
  const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  const panel = windows.find((client) => new URL(client.url).origin === self.location.origin);
  if (panel) {
    await panel.focus();
    // The panel moves itself, so nothing open there reloads.
    panel.postMessage({ type: 'agentos-open', path });
    return undefined;
  }
  return self.clients.openWindow(path);
}

/**
 * One tap, with the signed-in session and the hash. Anything other than a plain yes opens the
 * approval instead, so the person sees why: signed out, a step-up, a change, already decided.
 */
async function deny(data) {
  try {
    const session = await self.agentosFetch('/api/session', { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!session.ok) return openPanel(reviewTarget(data));
    const { csrf } = await session.json();
    const path = `/api/orgs/${encodeURIComponent(data.org)}/approvals/${encodeURIComponent(data.id)}/deny`;
    const response = await self.agentosFetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-Agentos-CSRF': csrf },
      body: JSON.stringify({ content_hash: data.content_hash }),
    });
    if (!response.ok) return openPanel(reviewTarget(data));
    return self.registration.showNotification('Denied', {
      body: data.body ? `${data.body}. Nothing was done.` : 'Nothing was done.',
      tag: data.tag,
      silent: true,
      icon: ICON,
      badge: BADGE,
      data: { type: 'denied', url: data.fallback_url },
    });
  } catch {
    return openPanel(reviewTarget(data));
  }
}

self.addEventListener('notificationclick', (event) => {
  const data = event.notification.data || {};
  event.notification.close();
  if (event.action === 'deny' && data.type === 'approval' && data.content_hash) {
    event.waitUntil(deny(data));
    return;
  }
  event.waitUntil(openPanel(reviewTarget(data)));
});
