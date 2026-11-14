/**
 * S3 in a real browser: the installable app, push through the real service worker, the Review
 * page, the new settings at phone width, and focus on a busy Take over.
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const ORG = '/northwind-group';

async function start(page: Page, path: string, scenario = 'still') {
  await page.goto('/sign-in');
  await page.evaluate((value) => {
    sessionStorage.setItem('agentos.mock.scenario', value);
    localStorage.removeItem('agentos.mock.signed-out');
  }, scenario);
  await page.goto(path);
}

const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

test('the app is installable: manifest, icons and an offline page that caches nothing else', async ({ page, request }) => {
  await start(page, ORG);
  const manifest = await (await request.get('/manifest.webmanifest')).json();
  expect(manifest).toMatchObject({ name: 'agentos', start_url: '/', display: 'standalone' });
  const sizes = manifest.icons.map((icon: { sizes: string; purpose: string }) => `${icon.sizes} ${icon.purpose}`);
  expect(sizes).toEqual(expect.arrayContaining(['192x192 any', '512x512 any', '512x512 maskable']));
  for (const icon of manifest.icons) expect((await request.get(icon.src)).ok()).toBe(true);
  expect(await page.locator('link[rel="apple-touch-icon"]').getAttribute('href')).toBe('/icons/apple-touch-icon.png');
  const offline = await request.get('/offline.html');
  expect(await offline.text()).toContain('You are offline');
  // The worker is the app's own, with the mock's inside in this mode.
  const worker = await page.evaluate(async () => (await navigator.serviceWorker.ready).active?.scriptURL ?? '');
  expect(worker).toMatch(/\/sw-mock\.js$/);
  const cached = await page.evaluate(async () => {
    const names = await caches.keys();
    const urls: string[] = [];
    for (const name of names) for (const entry of await (await caches.open(name)).keys()) urls.push(new URL(entry.url).pathname);
    return urls.sort();
  });
  expect(cached).toEqual(['/icons/icon-192.png', '/offline.html']);
});

test('push: on for this device, a test arrives through the service worker, and an approval offers Review and Deny only', async ({ page, context }) => {
  await context.grantPermissions(['notifications']);
  // Headless Chrome has no push service, so the browser's subscription is the one stand-in here.
  await page.addInitScript(() => {
    const fake = {
      endpoint: 'https://push.example/e2e',
      options: { applicationServerKey: null },
      toJSON: () => ({ endpoint: 'https://push.example/e2e', keys: { p256dh: 'p256dh', auth: 'auth' } }),
      unsubscribe: async () => true,
    };
    let current: typeof fake | null = null;
    PushManager.prototype.subscribe = async function () {
      current = fake;
      return fake as unknown as PushSubscription;
    };
    PushManager.prototype.getSubscription = async function () {
      return current as unknown as PushSubscription | null;
    };
  });
  await start(page, `${ORG}/settings/notifications`);
  const toggle = page.getByRole('switch', { name: 'Push on this device' });
  await toggle.click();
  await expect(page.getByText('Push is on for this device')).toBeVisible();
  await expect(page.getByText('This device', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Send a test', exact: true }).click();
  await expect.poll(() => page.evaluate(async () => (await (await navigator.serviceWorker.ready).getNotifications()).map((item) => item.title))).toContain(
    'Notifications are working',
  );
  const actions = await page.evaluate(async () => {
    const registration = await navigator.serviceWorker.ready;
    registration.active?.postMessage({
      type: 'agentos-mock-push',
      payload: {
        v: 1,
        type: 'approval',
        org: 'northwind-group',
        id: 'ap_e2e',
        tag: 'ap-e2e',
        title: 'Something is ready to publish',
        body: 'Aster Coffee · Instagram',
        url: '/northwind-group/review/ap_e2e?e=9999999999&s=mock',
        fallback_url: '/northwind-group/needs-you',
        content_hash: 'sha256:e2e',
        links_expire_at: 9999999999,
        urgent: true,
      },
    });
    for (let tries = 0; tries < 50; tries += 1) {
      const [shown] = await registration.getNotifications({ tag: 'ap-e2e' });
      if (shown) return { title: shown.title, body: shown.body, actions: (shown as Notification & { actions: { action: string }[] }).actions.map((item) => item.action) };
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return null;
  });
  expect(actions).toEqual({ title: 'Something is ready to publish', body: 'Aster Coffee · Instagram', actions: ['review', 'deny'] });
});

test('a notification tap moves the open panel to the exact approval', async ({ page }) => {
  await start(page, `${ORG}/needs-you`);
  await expect(page.getByRole('heading', { name: 'Needs you', level: 1 })).toBeVisible();
  const id = await page.evaluate(async () => {
    const response = await fetch('/api/orgs/northwind-group/approvals');
    const items = (await response.json()) as { id: string; effect: string }[];
    return items.find((item) => item.effect === 'commit')!.id;
  });
  const e = Math.floor(Date.now() / 1000) + 1800;
  // What the worker sends the panel when Review is tapped.
  await page.evaluate(
    ([path]) => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', { data: { type: 'agentos-open', path } })),
    [`/northwind-group/review/${id}?e=${e}&s=mock`],
  );
  await expect(page).toHaveURL(new RegExp(`/review/${id}`));
  await expect(page.getByRole('heading', { name: 'Review', level: 1 })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Post it' })).toBeVisible();
  // Never somewhere else, whatever the message says.
  await page.evaluate(() => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', { data: { type: 'agentos-open', path: '//evil.example' } })));
  await page.waitForTimeout(200);
  expect(new URL(page.url()).host).toBe(new URL(page.url()).host);
  await expect(page).toHaveURL(new RegExp(`/review/${id}`));
});

test('an expired Review link lands calmly and offers Needs you', async ({ page }) => {
  await start(page, `${ORG}/review/ap_UNKNOWN?e=1&s=mock`);
  await expect(page.getByRole('heading', { name: /This link has run out|This request is not here/ })).toBeVisible();
  await page.getByRole('link', { name: /Open Needs you/ }).click();
  await expect(page).toHaveURL(/needs-you/);
});

test('Take over keeps keyboard focus while its request runs, in Chrome', async ({ page }) => {
  // Without `still` the mock takes a quarter second to answer, long enough to look.
  await start(page, `${ORG}/lanes/aster/instagram`, '');
  const take = page.getByRole('button', { name: /^Take over/ });
  await expect(take).toBeEnabled();
  await take.focus();
  const record = page.evaluate(
    () =>
      new Promise<string[]>((resolve) => {
        const seen: string[] = [];
        const tick = () => {
          const active = document.activeElement;
          const busy = active?.getAttribute('aria-busy') === 'true';
          seen.push(busy ? 'busy-button' : active === document.body ? 'body' : (active?.tagName ?? 'none'));
          if (document.body.textContent?.includes('You have control')) resolve(seen);
          else requestAnimationFrame(tick);
        };
        tick();
      }),
  );
  await page.keyboard.press('Enter');
  const seen = await record;
  expect(seen).toContain('busy-button');
  expect(seen).not.toContain('body');
});

test.describe('at 375 wide', () => {
  test.use({ viewport: { width: 375, height: 812 } });

  for (const path of ['settings/notifications', 'settings/webhooks', 'settings/standing', 'settings/retention']) {
    test(`${path} fits the phone`, async ({ page }) => {
      await start(page, `${ORG}/${path}`);
      await expect(page.getByRole('heading', { level: 2 }).first()).toBeVisible();
      await page.waitForTimeout(400);
      expect(await overflow(page)).toBe(0);
    });
  }
});
