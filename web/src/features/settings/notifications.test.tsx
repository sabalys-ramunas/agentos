import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { server } from '@/test/setup';

const PATH = '/northwind-group/settings/notifications';
const COMPUTER = { magic_link: false, google: false, passkey: true, code: true };
const org = () => db.orgs.get('northwind-group')!;
const pushCard = async () => within((await screen.findByRole('heading', { name: 'Push' })).closest('div.rounded-xl') as HTMLElement);

/** A browser that can take push: Notification, PushManager and a ready service worker. */
function stubPushBrowser(permission: NotificationPermission = 'default') {
  const subscription = {
    options: { applicationServerKey: null },
    toJSON: () => ({ endpoint: 'https://push.example/sub-1', keys: { p256dh: 'p256dh-key', auth: 'auth-key' } }),
    unsubscribe: vi.fn(async () => true),
  };
  let current: typeof subscription | null = null;
  const pushManager = {
    getSubscription: vi.fn(async () => current),
    subscribe: vi.fn(async () => (current = subscription)),
  };
  const notification = Object.assign(function Notification() {}, {
    permission,
    requestPermission: vi.fn(async () => {
      notification.permission = 'granted';
      return 'granted' as const;
    }),
  });
  Object.assign(window, { Notification: notification, PushManager: function PushManager() {} });
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: { ready: Promise.resolve({ pushManager }), addEventListener() {}, removeEventListener() {} },
  });
  return { pushManager, subscription, notification };
}

afterEach(() => {
  // Only what these tests added. The setup's own stubs stay.
  Reflect.deleteProperty(window, 'Notification');
  Reflect.deleteProperty(window, 'PushManager');
  Reflect.deleteProperty(navigator, 'serviceWorker');
});

describe('notification settings', () => {
  it('saves a change at once, and puts it back when the server refuses', async () => {
    server.use(http.put('/api/orgs/:org/settings/notifications', () => HttpResponse.json({ error: { code: 'invalid', message: 'No.' } }, { status: 422 })));
    const { user } = renderApp(PATH);
    const urgent = await screen.findByRole('switch', { name: 'Urgent email' });
    expect(urgent).toBeChecked();
    await user.click(urgent);
    // Rolled back to what was saved, and it says so.
    await waitFor(() => expect(urgent).toBeChecked());
    expect(await screen.findByText(/Your last saved choice is back/)).toBeInTheDocument();
    expect(org().notifications.email_urgent).toBe(true);
  });

  it('keeps the change when the server agrees, and works out where things go from it', async () => {
    const { user } = renderApp(PATH);
    const table = await screen.findByRole('table', { name: 'Where each kind of event goes' });
    const urgentRow = within(table).getByRole('row', { name: /Payments and irreversible actions/ });
    expect(within(urgentRow).getAllByText('At once').length).toBeGreaterThan(0);
    await user.click(screen.getByRole('switch', { name: 'Urgent email' }));
    await waitFor(() => expect(org().notifications.email_urgent).toBe(false));
    expect(within(urgentRow).getByText('Off')).toBeInTheDocument();
    expect(await screen.findByText('Saved')).toBeInTheDocument();
  });

  it('never sends the old webhook_url field', async () => {
    let body: Record<string, unknown> = {};
    server.use(
      http.put('/api/orgs/:org/settings/notifications', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(body);
      }),
    );
    const { user } = renderApp(PATH);
    await user.click(await screen.findByRole('switch', { name: 'Push to my devices' }));
    await waitFor(() => expect(body.push).toBe(false));
    expect(body).not.toHaveProperty('webhook_url');
    expect(body.quiet_hours).toMatchObject({ timezone: 'Europe/Vilnius', urgent_breaks_through: true });
  });

  it('shows the quiet window on the clock of its time zone', async () => {
    renderApp(PATH);
    expect(await screen.findByText(/in Europe\/Vilnius, so quiet hours are (on|not on) now/)).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Urgent comes through' })).toBeChecked();
  });

  it('previews the digest from real numbers, with what waits first', async () => {
    renderApp(PATH);
    expect(await screen.findByText(/Your day on openajar · Northwind Group · \d+ need you/)).toBeInTheDocument();
    expect(screen.getAllByText('An irreversible action needs you').length).toBeGreaterThan(0);
    expect(screen.getByRole('table', { name: 'Each workspace' })).toBeInTheDocument();
  });

  describe('push on this device', () => {
    it('says plainly when this browser cannot take push', async () => {
      renderApp(PATH);
      const card = await pushCard();
      expect(await card.findByText('This browser cannot receive push')).toBeInTheDocument();
      expect(card.queryByRole('switch')).not.toBeInTheDocument();
    });

    it('says when the server has no push, instead of a dead switch', async () => {
      server.use(http.get('/api/orgs/:org/push/config', () => HttpResponse.json({ available: false, reason: 'not_configured', public_key: null })));
      stubPushBrowser();
      renderApp(PATH);
      const card = await pushCard();
      expect(await card.findByText('Phone push is not set up on this server')).toBeInTheDocument();
      expect(card.queryByRole('switch')).not.toBeInTheDocument();
    });

    it('says how this computer reaches a person instead, when it cannot send push itself', async () => {
      server.use(
        http.get('/api/auth/providers', () => HttpResponse.json({ magic_link: false, google: false, passkey: true, code: true })),
        http.get('/api/orgs/:org/push/config', () => HttpResponse.json({ available: false, reason: 'not_configured', public_key: null })),
      );
      org().notifications.push = false;
      stubPushBrowser();
      renderApp(PATH);
      const card = await pushCard();
      expect(await card.findByText(/To reach your phone instead, install the free ntfy app/)).toBeInTheDocument();
      expect(card.queryByRole('switch')).not.toBeInTheDocument();
    });

    it('never shows Push to my devices switched on when the server cannot push', async () => {
      // The live stack: push saved as on, and no push set up on the server.
      server.use(http.get('/api/orgs/:org/push/config', () => HttpResponse.json({ available: false, reason: 'not_configured', public_key: null })));
      org().notifications.push = true;
      renderApp(PATH);
      const channel = await screen.findByRole('switch', { name: 'Push to my devices' });
      await waitFor(() => expect(channel).toBeDisabled());
      expect(channel).not.toBeChecked();
      expect(screen.getByText(/Not available here, so nothing is pushed/)).toBeInTheDocument();
      const table = screen.getByRole('table', { name: 'Where each kind of event goes' });
      const urgent = within(table).getByText('Payments and irreversible actions').closest('tr')!;
      expect(within(urgent).getAllByRole('cell')[0]).toHaveTextContent('Off');
    });

    it('explains how to unblock when the browser blocks notifications', async () => {
      stubPushBrowser('denied');
      renderApp(PATH);
      const card = await pushCard();
      expect(await card.findByText('Notifications are blocked for this site')).toBeInTheDocument();
      expect(card.getByText(/Site settings/)).toBeInTheDocument();
    });

    it('asks the browser on a tap, subscribes, and registers this device', async () => {
      const { pushManager, notification } = stubPushBrowser();
      const before = org().pushDevices.length;
      const { user } = renderApp(PATH);
      const card = await pushCard();
      const toggle = await card.findByRole('switch', { name: 'Push on this device' });
      expect(toggle).not.toBeChecked();
      await user.click(toggle);
      expect(await card.findByText('Push is on for this device')).toBeInTheDocument();
      expect(notification.requestPermission).toHaveBeenCalledTimes(1);
      expect(pushManager.subscribe).toHaveBeenCalledWith(expect.objectContaining({ userVisibleOnly: true }));
      expect(org().pushDevices).toHaveLength(before + 1);
      expect(card.getByText('This device')).toBeInTheDocument();
      // A test goes to every device.
      await user.click(card.getByRole('button', { name: 'Send a test' }));
      expect(await card.findByText(/Sent to your 2 devices/)).toBeInTheDocument();
    });

    it('turns off by unsubscribing the browser and removing the device', async () => {
      const { subscription } = stubPushBrowser();
      const { user } = renderApp(PATH);
      const card = await pushCard();
      await user.click(await card.findByRole('switch', { name: 'Push on this device' }));
      await card.findByText('Push is on for this device');
      await user.click(card.getByRole('switch', { name: 'Push on this device' }));
      expect(await card.findByText('Push is off for this device')).toBeInTheDocument();
      expect(subscription.unsubscribe).toHaveBeenCalled();
      expect(org().pushDevices.map((item) => item.endpoint)).not.toContain('https://push.example/sub-1');
    });

    it('removes another device at once, and brings it back if the server says no', async () => {
      server.use(http.delete('/api/orgs/:org/push/devices/:id', () => HttpResponse.json({ error: { code: 'unavailable', message: 'Down.' } }, { status: 503 })));
      const { user } = renderApp(PATH);
      const card = await pushCard();
      await user.click(await card.findByRole('button', { name: 'Remove' }));
      await waitFor(() => expect(card.getByText('Safari on iPhone')).toBeInTheDocument());
    });
  });
});

describe('notifications on a computer', () => {
  it('offers no email choice at all when the computer sends none, and says so once', async () => {
    server.use(http.get('/api/auth/providers', () => HttpResponse.json(COMPUTER)));
    renderApp(PATH);
    expect(await screen.findByText('This computer sends no email. Approvals still wait in Needs you.')).toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: 'Urgent email' })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Email digest' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Send a test email' })).not.toBeInTheDocument();
  });
});
