import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import type { Role } from '@/api/types';
import { overrides } from '@/api/client';
import { db, resetDb } from '@/mocks/handlers';
import { writeTechnical } from '@/lib/technical';
import { server } from '@/test/setup';
import { renderApp } from '@/test/render';

const data = () => db.orgs.get('northwind-group')!;
const asRole = (role: Role) => {
  data().role = role;
};
const steppedUp = () => {
  db.stepUpUntil = new Date(Date.now() + 600_000).toISOString();
};
const ROUTINES = '/northwind-group/flows';
const routine = (id: string) => `/northwind-group/flows/${id}`;
const ACCOUNT = '/northwind-group/lanes/aster/gmail';
const setting = (id: string) => data().askFirst.find((item) => item.id === id);
/** The row in the list: the same routine also has a tile on the line through now. */
const row = async (name: string) => (await screen.findAllByRole('link', { name: new RegExp(`^${name}`) })).map((link) => link.closest('li, tr')).find(Boolean) as HTMLElement;

// The whole list renders in jsdom, which a loaded machine takes its time over.
vi.setConfig({ testTimeout: 20_000 });

beforeEach(() => resetDb());

describe('on a routine', () => {
  it('says plainly how each routine runs, on the list and on its own page', async () => {
    const { user } = renderApp(ROUTINES);
    expect(within(await row('Weekly newsletter')).getAllByText('Asks you first').length).toBeGreaterThan(0);
    expect(within(await row('Invoice reminders')).getAllByText('Runs on its own').length).toBeGreaterThan(0);
    await user.click(within(await row('Invoice reminders')).getByRole('link'));
    expect(await screen.findByRole('heading', { name: 'Runs on its own' })).toBeInTheDocument();
    expect(screen.getByText("Money and things that can't be undone always ask you, whatever this switch says.")).toBeInTheDocument();
  });

  it('turns on at once, stays on once the computer agrees, and names who did it', async () => {
    const { user } = renderApp(routine('harbor.xero.invoice-reminders'));
    const control = await screen.findByRole('switch', { name: /Ask me first for/ });
    await user.click(control);
    expect(await screen.findByRole('heading', { name: 'Asks you first' })).toBeInTheDocument();
    await waitFor(() => expect(setting('harbor.xero.invoice-reminders')?.ask_first).toBe(true));
    expect(await screen.findByText(/^You turned this on/)).toBeInTheDocument();
    expect(control).toHaveFocus();
  });

  it('asks a person to confirm before it turns off, and puts everything back if they stop', async () => {
    const { user } = renderApp(routine('aster.gmail.newsletter'));
    const control = await screen.findByRole('switch', { name: /Ask me first for/ });
    expect(control).toBeChecked();
    await user.click(control);
    const dialog = await screen.findByRole('dialog', { name: /Confirm it is you/ });
    expect(within(dialog).getByText(/It will act without waiting for anyone/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Runs on its own', hidden: true })).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(await screen.findByRole('heading', { name: 'Asks you first' })).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: /Ask me first for/ })).toBeChecked();
    expect(setting('aster.gmail.newsletter')?.ask_first).toBe(true);
    expect(screen.queryByText(/^Back to /)).not.toBeInTheDocument();
  });

  it('turns off for an owner who has confirmed, and offers to undo it', async () => {
    steppedUp();
    const { user } = renderApp(routine('aster.gmail.newsletter'));
    await user.click(await screen.findByRole('switch', { name: /Ask me first for/ }));
    expect(await screen.findByRole('heading', { name: 'Runs on its own' })).toBeInTheDocument();
    await waitFor(() => expect(setting('aster.gmail.newsletter')?.ask_first).toBe(false));
  });

  it('rolls back with a line when the computer refuses', async () => {
    steppedUp();
    server.use(
      http.put('/api/orgs/:org/flows/:id/ask-first', () => HttpResponse.json({ error: { code: 'forbidden', message: 'Turn it off on the computer itself.' } }, { status: 403 })),
    );
    const { user } = renderApp(routine('aster.gmail.newsletter'));
    const control = await screen.findByRole('switch', { name: /Ask me first for/ });
    await user.click(control);
    expect(await screen.findByText('Back to Asks you first. Turn it off on the computer itself.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Asks you first' })).toBeInTheDocument();
    expect(control).toBeChecked();
    expect(control).toHaveFocus();
  });

  it('shows who set it when an agent did, and that only a person can undo it', async () => {
    renderApp(routine('harbor.blog.publish'));
    expect(await screen.findByText(/^Claude Code turned this on, 3h ago\. Only a person can turn it off\.$/)).toBeInTheDocument();
  });

  it('gives the reason a routine an agent saved started asking, where it says who set it', async () => {
    renderApp(routine('aster.gmail.welcome-series'));
    expect(await screen.findByText(/^Claude Code turned this on, 5h ago\. A person set Gmail newsletter to ask first, so this new routine on the same account started that way\. Only a person can turn it off\.$/)).toBeInTheDocument();
  });

  it('renders a computer\'s reason as text', async () => {
    data().askFirst.find((item) => item.id === 'aster.gmail.welcome-series')!.why = '<img src=x onerror=alert(1)>';
    renderApp(routine('aster.gmail.welcome-series'));
    expect(await screen.findByText(/<img src=x onerror=alert\(1\)>/)).toBeInTheDocument();
    expect(document.querySelector('img[src="x"]')).toBeNull();
  });

  it('offers no switch on money or on what cannot be undone, and says why', async () => {
    renderApp(routine('harbor.xero.pay-suppliers'));
    expect(await screen.findByRole('heading', { name: 'Asks you first' })).toBeInTheDocument();
    expect(screen.getByText('It spends money, so it asks you every time.')).toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: /Ask me first/ })).not.toBeInTheDocument();
  });

  it('sends a person to the account when the account is what asks', async () => {
    data().askFirst.push({ scope: 'lane', id: 'harbor/xero', ask_first: true, set_by: { kind: 'user', id: 'usr_DEMO' }, set_at: new Date().toISOString() });
    renderApp(routine('harbor.xero.invoice-reminders'));
    expect(await screen.findByText('Its account asks you first, so this routine does too.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Change it on the account' })).toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: /Ask me first/ })).not.toBeInTheDocument();
  });
});

describe('by role', () => {
  it.each<Role>(['viewer', 'approver'])('shows a %s the state and no control', async (role) => {
    asRole(role);
    renderApp(routine('aster.gmail.newsletter'));
    expect(await screen.findByRole('heading', { name: 'Asks you first' })).toBeInTheDocument();
    expect(screen.getByText(/An owner or admin can change this\./)).toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: /Ask me first/ })).not.toBeInTheDocument();
  });

  it('shows the list to a viewer with no switch in any row', async () => {
    asRole('viewer');
    renderApp(ROUTINES);
    expect(within(await row('Weekly newsletter')).getAllByText('Asks you first').length).toBeGreaterThan(0);
    expect(screen.queryByRole('switch', { name: /Ask me first/ })).not.toBeInTheDocument();
  });

  it('lets a member turn it on and says who can turn it off', async () => {
    asRole('member');
    const { user } = renderApp(routine('harbor.xero.invoice-reminders'));
    const control = await screen.findByRole('switch', { name: /Ask me first for/ });
    await user.click(control);
    expect(await screen.findByRole('heading', { name: 'Asks you first' })).toBeInTheDocument();
    expect(await screen.findByText(/An owner or admin can turn this off\./)).toBeInTheDocument();
    await waitFor(() => expect(setting('harbor.xero.invoice-reminders')?.ask_first).toBe(true));
    await user.click(screen.getByRole('switch', { name: /Ask me first for/ }));
    expect(screen.queryByRole('dialog', { name: /Confirm it is you/ })).not.toBeInTheDocument();
    expect(setting('harbor.xero.invoice-reminders')?.ask_first).toBe(true);
  });

  it('has the server refuse a viewer, whatever the panel shows', async () => {
    asRole('viewer');
    const reply = await fetch('/api/orgs/northwind-group/flows/harbor.xero.invoice-reminders/ask-first', { method: 'PUT', body: JSON.stringify({ ask_first: true }) });
    expect(reply.status).toBe(403);
  });
});

describe('on an account', () => {
  it('does not promise more than the switch can do', async () => {
    renderApp(ACCOUNT);
    expect(await screen.findByText('This switch stops posts on sites openajar recognises. A routine is the surest way to control what goes out.')).toBeInTheDocument();
    expect(screen.queryByText(/^Everything agents do/)).not.toBeInTheDocument();
  });

  it('has the same switch, and its routines move with it', async () => {
    const { user, client } = renderApp(ACCOUNT);
    await user.click(await screen.findByRole('switch', { name: /Ask me first for/ }));
    expect(await screen.findByRole('heading', { name: 'Asks you first' })).toBeInTheDocument();
    await waitFor(() => expect(setting('aster/gmail')?.ask_first).toBe(true));
    await waitFor(() => {
      const flows = client.getQueryData<{ id: string; account_asks_first: boolean }[]>(['org', 'northwind-group', 'flows']);
      expect(flows?.find((item) => item.id === 'aster.portal.bean-order')).toBeDefined();
      expect(flows?.find((item) => item.id === 'aster.gmail.newsletter')?.account_asks_first).toBe(true);
    });
  });

  it('shows on each account card, for a person to flip without leaving the list', async () => {
    const { user } = renderApp('/northwind-group/accounts');
    const card = await screen.findByRole('article', { name: /Xero/ });
    await user.click(await within(card).findByRole('switch', { name: /Ask me first for/ }));
    await waitFor(() => expect(setting('harbor/xero')?.ask_first).toBe(true));
    expect(within(card).getByText('Asks you first')).toBeInTheDocument();
  });
});

describe('a computer that cannot do it', () => {
  it('says the truth and draws no switch', async () => {
    const node = data().lanes.find((item) => item.id === 'harbor/xero')!.node;
    data().nodes.find((item) => item.id === node)!.state = 'down';
    renderApp(routine('harbor.xero.invoice-reminders'));
    expect(await screen.findByText('Its computer is offline, so this can change once it is back.')).toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: /Ask me first/ })).not.toBeInTheDocument();
  });
});

describe('overrides on an account', () => {
  const INSTAGRAM = '/northwind-group/lanes/aster/instagram';

  it('shows the checks turned off for this account only with Technical details on', async () => {
    writeTechnical(db.user.id, true);
    renderApp(INSTAGRAM);
    const section = await screen.findByRole('region', { name: 'Overrides' });
    expect(within(section).getByText('country_match')).toBeInTheDocument();
    expect(within(section).getByText(/Reason: The account is in Vilnius/)).toBeInTheDocument();
    expect(within(section).queryByText('shared_address')).toBeNull();
  });

  it('is not drawn with Technical details off, and not for an account with none', async () => {
    renderApp(INSTAGRAM);
    await screen.findByRole('switch', { name: /Ask me first for/ });
    expect(screen.queryByRole('region', { name: 'Overrides' })).toBeNull();
    writeTechnical(db.user.id, true);
    renderApp('/northwind-group/lanes/aster/gmail');
    await screen.findByRole('switch', { name: /Ask me first for/ });
    expect(screen.queryByRole('region', { name: 'Overrides' })).toBeNull();
  });

  it('is never asked of anyone who is not the owner, and the node refuses them', async () => {
    writeTechnical(db.user.id, true);
    asRole('admin');
    renderApp(INSTAGRAM);
    expect(await screen.findByRole('heading', { name: /Ask me first|Asks you first|Runs on its own/ })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Overrides' })).toBeNull();
    await expect(overrides.list('northwind-group')).rejects.toMatchObject({ status: 403 });
  });
});

