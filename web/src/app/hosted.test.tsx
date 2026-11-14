/**
 * The hosted app, where the cloud does not serve some routes the computer does. Every one of those
 * routes answers 404 here, as the cloud really does, and each screen must read that as "not here":
 * a section hides or says so calmly. No error state, no toast, no skeleton left spinning, and no
 * control that could only reach one of those routes.
 *
 * The list is `cloud-gaps.json`, which the hosted edition's own tests keep in step with its API.
 */
import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { toast } from 'sonner';
import gaps from '@/test/cloud-gaps.json';
import { renderApp } from '@/test/render';
import { server } from '@/test/setup';

type Method = 'get' | 'post' | 'put' | 'patch' | 'delete';
const VERBS: Record<string, Method> = { GET: 'get', POST: 'post', PUT: 'put', PATCH: 'patch', DELETE: 'delete' };
const CLOUD_HOSTED = { magic_link: true, google: true, passkey: true };

/** A cloud that does not serve these: each one answers the way the cloud answers an unknown route. */
function notServedHere() {
  return [
    http.get('/api/auth/providers', () => HttpResponse.json(CLOUD_HOSTED)),
    ...gaps.map(({ method, path }) =>
      http[VERBS[method] ?? 'get'](path, () => HttpResponse.json({ error: { code: 'not_found', message: 'Not found.' } }, { status: 404 })),
    ),
  ];
}

const toasts = { error: vi.spyOn(toast, 'error'), plain: vi.spyOn(toast, 'message') };

/** Waits for the page to draw: its title, or the calm card for a part the cloud does not show. */
async function waitForPage() {
  await waitFor(() => expect(document.querySelector('h1') ?? screen.queryByText('Shown on your computer'), 'the page did not draw').not.toBeNull(), {
    timeout: 10_000,
  });
}

/** Waits until nothing is still loading, then reports anything that reads as a failure. */
async function expectCalm(label: string) {
  await waitFor(
    () => {
      expect(document.querySelector('[aria-label="Loading"]'), `${label}: still loading`).toBeNull();
      expect(document.querySelectorAll('.skeleton'), `${label}: a skeleton left spinning`).toHaveLength(0);
    },
    { timeout: 6000 },
  );
  expect(screen.queryByRole('alert'), `${label}: an error state`).toBeNull();
  expect(screen.queryByText('Could not load this'), `${label}: an error state`).toBeNull();
  expect(toasts.error, `${label}: a toast`).not.toHaveBeenCalled();
  expect(toasts.plain, `${label}: a toast`).not.toHaveBeenCalled();
}

const ON_HOSTED: [string, string][] = [
  ['Home', '/northwind-group'],
  ['Settings, memory', '/northwind-group/settings/memory'],
  ['History, proof', '/northwind-group/activity/proof'],
  ['a business', '/northwind-group/workspaces/aster'],
  ['Needs you', '/northwind-group/needs-you'],
  ['Accounts', '/northwind-group/accounts'],
  ['Environments', '/northwind-group/environments'],
  ['an environment', '/northwind-group/environments/e7k2'],
  ['Accounts, engagement', '/northwind-group/accounts/engagement'],
  ['an account', '/northwind-group/lanes/aster/instagram'],
  ['an account, its engagement', '/northwind-group/lanes/aster/instagram/engagement'],
  ['an account, its sign-in', '/northwind-group/logins'],
  ['Routines', '/northwind-group/flows'],
  ['a new routine', '/northwind-group/flows/new'],
  ['Email', '/northwind-group/email'],
  ['Email, connect a mailbox', '/northwind-group/email/connect'],
  ['Email, leads', '/northwind-group/email/leads'],
  ['Email, new mail', '/northwind-group/email/new-mail'],
  ['Site APIs', '/northwind-group/site-apis'],
  ['Site APIs, teach one', '/northwind-group/site-apis/new'],
  ['History', '/northwind-group/activity'],
  ['Machines', '/northwind-group/nodes'],
  ['Machines, new address', '/northwind-group/machines'],
  ['Runs', '/northwind-group/runs'],
  ['a run', '/northwind-group/runs/job-7f3a2c91'],
  ['Checks', '/northwind-group/checks'],
  ['a check', '/northwind-group/checks/run_8d2f'],
  ['Brand', '/northwind-group/brand'],
  ['a product profile', '/northwind-group/brand/shop'],
  ['a machine', '/northwind-group/nodes/hetzner-1'],
  ['a machine, its lanes', '/northwind-group/nodes/hetzner-1?tab=lanes'],
  ['a machine, its installed programs', '/northwind-group/nodes/hetzner-1?tab=installed'],
  ['a machine, its health', '/northwind-group/nodes/hetzner-1?tab=health'],
  ['a machine, its schedules', '/northwind-group/nodes/hetzner-1?tab=schedules'],
  ['a machine, its browsers', '/northwind-group/nodes/hetzner-1?tab=browsers'],
  ['a machine, its backups', '/northwind-group/nodes/hetzner-1?tab=backups'],
  ['a machine, its environments', '/northwind-group/nodes/hetzner-1?tab=environments'],
  ['a machine, its identity', '/northwind-group/nodes/hetzner-1?tab=identity'],
  ['a machine, its logs', '/northwind-group/nodes/hetzner-1?tab=logs'],
  ['a machine, its connection', '/northwind-group/nodes/hetzner-1?tab=connection'],
  ['a machine, its resources', '/northwind-group/nodes/hetzner-1?tab=resources'],
  ['a machine, its settings', '/northwind-group/nodes/hetzner-1?tab=settings'],
  ['Settings', '/northwind-group/settings'],
  ['Settings, members', '/northwind-group/settings/members'],
  ['Settings, invites', '/northwind-group/settings/invites'],
  ['Settings, workspaces', '/northwind-group/settings/workspaces'],
  ['Settings, access', '/northwind-group/settings/access'],
  ['Settings, grants', '/northwind-group/settings/grants'],
  ['Settings, what needs a yes', '/northwind-group/settings/yes'],
  ['Settings, standing approvals', '/northwind-group/settings/standing'],
  ['Settings, notifications', '/northwind-group/settings/notifications'],
  ['Settings, webhooks', '/northwind-group/settings/webhooks'],
  ['Settings, billing', '/northwind-group/settings/billing'],
  ['Settings, retention', '/northwind-group/settings/retention'],
  ['Settings, audit log', '/northwind-group/settings/audit'],
  ['Settings, danger zone', '/northwind-group/settings/danger'],
  ['Settings, AI models', '/northwind-group/settings/brains'],
  ['Settings, add an AI model', '/northwind-group/settings/brains/add'],
  ['Settings, workers', '/northwind-group/settings/workers'],
  ['Settings, vault', '/northwind-group/settings/vault'],
  ['Settings, profile', '/northwind-group/settings/account'],
  ['Settings, passkeys', '/northwind-group/settings/passkeys'],
  ['Settings, devices', '/northwind-group/settings/devices'],
];

describe('the hosted app, where the cloud does not serve some routes', () => {
  beforeEach(() => {
    server.use(...notServedHere());
    toasts.error.mockClear();
    toasts.plain.mockClear();
  });

  it('answers 404 for every route the cloud does not serve, so the screens below really see it', async () => {
    const answers = await Promise.all(
      gaps.map(({ method, path }) =>
        fetch(path.replace(':org', 'northwind-group').replace(/:\w+/g, 'x'), { method }).then((response) => response.status),
      ),
    );
    expect(answers.filter((status) => status !== 404)).toEqual([]);
  });

  it.each(ON_HOSTED)(
    '%s stays calm when every route the cloud does not serve answers 404',
    async (label: string, path: string) => {
      renderApp(path);
      await waitForPage();
      await expectCalm(label);
    },
    30_000,
  );

  it('keeps the controls only a computer has off the hosted app', async () => {
    renderApp('/northwind-group/lanes/aster/instagram');
    await screen.findByRole('heading', { level: 1 }, { timeout: 6000 });
    await waitFor(() => expect(document.querySelectorAll('.skeleton')).toHaveLength(0), { timeout: 6000 });
    expect(screen.queryByRole('button', { name: /^Freeze/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Make a watch link' })).toBeNull();
  }, 15_000);

  it('keeps a computer\'s own freeze, health and browser controls off the hosted app', async () => {
    renderApp('/northwind-group/nodes/hetzner-1?tab=health');
    await screen.findByRole('heading', { level: 1 }, { timeout: 6000 });
    const health = within(await screen.findByRole('region', { name: 'Health' }, { timeout: 6000 }));
    // The rounds answer first: the cloud has none, so the card says where they are, and no check can be run.
    expect((await screen.findAllByText('Shown on your computer', undefined, { timeout: 6000 })).length).toBeGreaterThan(0);
    expect(health.queryByRole('button', { name: 'Run the full check' })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Freeze/ }), 'freeze').toBeNull();
  }, 15_000);

  it('shows no remove or lock control for the vault, which only the computer changes', async () => {
    renderApp('/northwind-group/settings/vault');
    await screen.findByRole('heading', { level: 1 }, { timeout: 6000 });
    await screen.findByText('aster-instagram', undefined, { timeout: 6000 });
    expect(screen.queryByRole('button', { name: /^Remove / })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Lock' })).toBeNull();
  }, 15_000);

  it('offers no mailbox connection, which is made on the computer that holds the password', async () => {
    renderApp('/northwind-group/email/connect');
    expect(await screen.findByText('Shown on your computer', undefined, { timeout: 6000 })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Find my mail' })).toBeNull();
  }, 15_000);
});
