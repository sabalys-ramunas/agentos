/**
 * The service worker, run as written in public/sw.js against a fake worker scope. What matters:
 * no notification approves, Deny needs the session and the hash, and nothing is cached but the
 * offline page.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';

type Listener = (event: Record<string, unknown>) => void;

interface Shown {
  title: string;
  options: { body?: string; tag?: string; actions?: { action: string; title: string }[]; data?: Record<string, unknown>; requireInteraction?: boolean };
}

function load({ fetch = vi.fn(), windows = [] as { url: string; focus: () => Promise<void>; postMessage: (message: unknown) => void }[] } = {}) {
  const listeners = new Map<string, Listener>();
  const shown: Shown[] = [];
  const closed: string[] = [];
  const opened: string[] = [];
  const self: Record<string, unknown> = {
    location: { origin: 'https://panel.example' },
    addEventListener: (type: string, listener: Listener) => listeners.set(type, listener),
    skipWaiting: () => Promise.resolve(),
    registration: {
      showNotification: async (title: string, options: Shown['options']) => void shown.push({ title, options }),
      getNotifications: async ({ tag }: { tag: string }) => [{ close: () => closed.push(tag) }],
    },
    clients: {
      claim: () => Promise.resolve(),
      matchAll: async () => windows,
      openWindow: async (path: string) => void opened.push(path),
    },
  };
  const source = readFileSync(join(__dirname, '../../public/sw.js'), 'utf8');
  runInNewContext(source, { self, fetch, caches: {}, URL, Response, console, Date, JSON, Number, Promise, encodeURIComponent });
  async function fire(type: string, event: Record<string, unknown>) {
    let work: Promise<unknown> = Promise.resolve();
    listeners.get(type)!({ ...event, waitUntil: (promise: Promise<unknown>) => (work = promise), respondWith: () => undefined });
    await work;
  }
  const push = (payload: unknown) => fire('push', { data: { json: () => payload } });
  const click = (notification: Shown, action = '') => fire('notificationclick', { action, notification: { data: notification.options.data, close: () => undefined } });
  return { push, click, fire, shown, closed, opened, listeners };
}

const approval = {
  v: 1,
  type: 'approval',
  org: 'northwind-group',
  id: 'ap_1',
  tag: 'ap-tag',
  title: 'Something is ready to publish',
  body: 'Aster Coffee · Instagram',
  url: '/northwind-group/review/ap_1?e=9999999999&s=sig',
  fallback_url: '/northwind-group/needs-you',
  content_hash: 'sha256:abc',
  links_expire_at: 9_999_999_999,
  urgent: false,
};

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });

describe('the service worker', () => {
  it('shows an approval with Review and Deny, and never an Approve', async () => {
    const worker = load();
    await worker.push(approval);
    const [note] = worker.shown;
    expect(note?.title).toBe('Something is ready to publish');
    expect(note?.options.body).toBe('Aster Coffee · Instagram');
    expect(note?.options.actions?.map((item) => item.action)).toEqual(['review', 'deny']);
    expect(JSON.stringify(note)).not.toMatch(/approve"/i);
  });

  it('offers no Deny without a content hash, as for a question', async () => {
    const worker = load();
    await worker.push({ ...approval, content_hash: null });
    expect(worker.shown[0]?.options.actions?.map((item) => item.action)).toEqual(['review']);
  });

  it('keeps an urgent one on screen until it is handled', async () => {
    const worker = load();
    await worker.push({ ...approval, urgent: true });
    expect(worker.shown[0]?.options.requireInteraction).toBe(true);
  });

  it('takes a notification back when the approval is decided elsewhere', async () => {
    const worker = load();
    await worker.push({ v: 1, type: 'dismiss', org: 'northwind-group', tag: 'ap-tag' });
    expect(worker.closed).toEqual(['ap-tag']);
    expect(worker.shown).toHaveLength(0);
  });

  it('opens the exact approval on Review, and Needs you once the link has run out', async () => {
    const worker = load();
    await worker.push(approval);
    await worker.click(worker.shown[0]!, 'review');
    await worker.push({ ...approval, links_expire_at: 1 });
    await worker.click(worker.shown[1]!);
    expect(worker.opened).toEqual(['/northwind-group/review/ap_1?e=9999999999&s=sig', '/northwind-group/needs-you']);
  });

  it('moves an open panel instead of opening another window', async () => {
    const post = vi.fn();
    const focus = vi.fn(async () => undefined);
    const worker = load({ windows: [{ url: 'https://panel.example/northwind-group', focus, postMessage: post }] });
    await worker.push(approval);
    await worker.click(worker.shown[0]!);
    expect(focus).toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith({ type: 'openajar-open', path: approval.url });
    expect(worker.opened).toEqual([]);
  });

  it('never sends the panel off this site, whatever the push says', async () => {
    const worker = load();
    await worker.push({ ...approval, url: 'https://evil.example/', fallback_url: '//evil.example' });
    await worker.click(worker.shown[0]!);
    expect(worker.opened).toEqual(['/northwind-group']);
  });

  it('denies with one tap through the signed-in session, the CSRF token and the hash', async () => {
    const fetch = vi.fn(async (input: string, _init?: RequestInit) => (input === '/api/session' ? ok({ csrf: 'csrf-1' }) : ok({ id: 'ap_1', state: 'denied' })));
    const worker = load({ fetch });
    await worker.push(approval);
    await worker.click(worker.shown[0]!, 'deny');
    const [, deny] = fetch.mock.calls;
    expect(deny?.[0]).toBe('/api/orgs/northwind-group/approvals/ap_1/deny');
    expect(deny?.[1]).toMatchObject({ method: 'POST', credentials: 'same-origin', headers: { 'X-Agentos-CSRF': 'csrf-1' } });
    expect(JSON.parse(String(deny?.[1]?.body))).toEqual({ content_hash: 'sha256:abc' });
    expect(worker.shown[1]?.title).toBe('Denied');
    expect(worker.opened).toEqual([]);
  });

  it.each([
    ['signed out', () => new Response('{}', { status: 401 }), null],
    ['changed', () => ok({ csrf: 'c' }), new Response('{}', { status: 409 })],
    ['a step-up', () => ok({ csrf: 'c' }), new Response('{}', { status: 403 })],
    ['expired', () => ok({ csrf: 'c' }), new Response('{}', { status: 410 })],
  ])('opens the approval instead of denying when %s, and does not retry', async (_name, session, decision) => {
    const fetch = vi.fn(async (input: string) => (input === '/api/session' ? session() : decision!));
    const worker = load({ fetch });
    await worker.push(approval);
    await worker.click(worker.shown[0]!, 'deny');
    expect(worker.opened).toEqual([approval.url]);
    expect(fetch.mock.calls.filter(([input]) => String(input).endsWith('/deny')).length).toBeLessThanOrEqual(1);
  });

  it('answers only failed page loads from the cache, never the API', () => {
    const worker = load({ fetch: vi.fn(async () => new Response('')) });
    const answered: string[] = [];
    const fetchListener = worker.listeners.get('fetch')!;
    for (const [url, mode] of [
      ['https://panel.example/api/session', 'cors'],
      ['https://panel.example/api/orgs/x/approvals', 'navigate'],
      ['https://panel.example/assets/app.js', 'no-cors'],
      ['https://panel.example/northwind-group', 'navigate'],
    ] as const) {
      fetchListener({ request: { url, mode, method: 'GET' }, respondWith: () => answered.push(url) });
    }
    expect(answered).toEqual(['https://panel.example/northwind-group']);
  });
});
