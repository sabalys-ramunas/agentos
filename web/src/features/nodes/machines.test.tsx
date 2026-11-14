import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { db } from '@/mocks/handlers';
import { holdRunningJobs } from '@/mocks/jobs';
import { renderApp } from '@/test/render';
import { server } from '@/test/setup';

const ORG = 'northwind-group';
const org = () => db.orgs.get(ORG)!;
/** Text that a component splits over several elements (a sentence with names, a command in a code block). */
const whole = (tag: string, text: string | RegExp) => (_: string, element: Element | null) =>
  element?.tagName === tag && (typeof text === 'string' ? element.textContent === text : text.test(element.textContent ?? ''));
const table = () => screen.findByRole('table', { name: 'Every machine' }, { timeout: 10_000 });

describe('Machines', () => {
  it('lists every machine and every VM the owner rents, under a sentence naming what matters', async () => {
    // The row names the seeded run that is going: keep it going, or the mock ends it after a few reads.
    holdRunningJobs(org());
    renderApp(`/${ORG}/nodes`);
    const grid = await table();
    await waitFor(() => expect(within(grid).getAllByRole('row')).toHaveLength(5));
    const rows = within(grid).getAllByRole('row').slice(1);
    expect(rows.map((row) => row.getAttribute('aria-label'))).toEqual(['home-mini, Online', 'hetzner-1, Online', 'vm_7f3a9c21, Ready', 'vm_2c8e4b10, Starting']);
    expect(screen.getByRole('heading', { level: 1, name: 'Machines' })).toBeInTheDocument();
    expect(screen.getByText(whole('P', /^2 online\. hetzner-1 is the busiest at \d+%\. harbor\/ci-box is a disposable VM with \d+ min left, one of 2 rented\.$/))).toBeInTheDocument();
    // The VM's time left and what it is for are the node's own words.
    expect(within(rows[2]!).getByText('Run the shop end-to-end tests on a clean machine')).toBeInTheDocument();
    // The running run shows on the machine it runs on. The runs are their own read, which can land after
    // the machines (on CI the row still said "An agent in 1 account"), so wait for it.
    const busiest = within(grid).getByRole('row', { name: 'hetzner-1, Online' });
    expect(await within(busiest).findByText('pnpm vitest run')).toBeInTheDocument();
  });

  it('opens a machine beside the table at its own address, with its lanes and a link to each run', async () => {
    const { user, router } = renderApp(`/${ORG}/nodes`);
    await user.click(within(await table()).getByRole('row', { name: 'hetzner-1, Online' }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/${ORG}/nodes/hetzner-1`));
    const panel = await screen.findByRole('dialog', { name: 'hetzner-1' });
    expect(await within(panel).findByRole('region', { name: 'Lanes' }, { timeout: 5_000 })).toBeInTheDocument();
    const runs = within(panel).getByRole('list', { name: 'Runs on hetzner-1' });
    expect(within(runs).getAllByRole('link')[0]).toHaveAttribute('href', expect.stringMatching(/\/runs\/job-/));
    // Every tab of the full page is one click away.
    const everything = within(panel).getByRole('region', { name: 'Everything about it' });
    expect(within(everything).getAllByRole('link')).toHaveLength(12);
    for (const tab of ['Logs', 'Installed', 'Health', 'Backups', 'Settings']) {
      expect(within(everything).getByRole('link', { name: new RegExp(`^${tab}`) })).toHaveAttribute('href', expect.stringContaining(`tab=${tab.toLowerCase()}`));
    }
    await user.keyboard('{Escape}');
    await waitFor(() => expect(router.state.location.pathname).toBe(`/${ORG}/nodes`));
  });

  it('keeps the sort in the address', async () => {
    const { user, router } = renderApp(`/${ORG}/nodes`);
    await user.click(within(await table()).getByRole('button', { name: /CPU/ }));
    await waitFor(() => expect(router.state.location.search).toMatchObject({ sort: 'load', dir: 'desc' }));
    const ids = within(await table()).getAllByRole('row').slice(1).map((row) => row.getAttribute('aria-label')?.split(',')[0]);
    expect(ids.slice(0, 2)).toEqual(['hetzner-1', 'home-mini']);
  });

  it('shows a VM with its price, its time left and the real command to delete it', async () => {
    renderApp(`/${ORG}/nodes/vm_7f3a9c21`);
    const panel = await screen.findByRole('dialog', { name: 'harbor/ci-box' }, { timeout: 10_000 });
    expect(within(panel).getByText('€0.0119 an hour')).toBeInTheDocument();
    expect(within(panel).getByText(whole('CODE', 'agentos vm destroy vm_7f3a9c21'))).toBeInTheDocument();
  });

  it('never asks for VMs for someone who is not the owner, so no refusal shows', async () => {
    org().role = 'admin';
    renderApp(`/${ORG}/nodes`);
    const rows = within(await table()).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('makes one machine useful: what is on it now, and the real ways to add another', async () => {
    const data = org();
    data.nodes = data.nodes.filter((node) => node.id === 'home-mini');
    data.vms = [];
    data.org.plan = 'self_hosted';
    renderApp(`/${ORG}/nodes`);
    expect(within(await table()).getAllByRole('row')).toHaveLength(2);
    expect(screen.getByText(whole('P', /^home-mini is your one machine at \d+% CPU/))).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'On home-mini now' })).toBeInTheDocument();
    expect(screen.getByText(whole('CODE', 'agentos vm setup'))).toBeInTheDocument();
    expect(screen.getByText(whole('CODE', 'agentos connect root@my-server --install'))).toBeInTheDocument();
  });

  it('says plainly when there is no machine yet', async () => {
    const data = org();
    data.nodes = [];
    data.vms = [];
    renderApp(`/${ORG}/nodes`);
    expect(await screen.findByRole('heading', { name: 'No machines yet' }, { timeout: 10_000 })).toBeInTheDocument();
  });

  it('says so when the machines cannot be read, and offers to try again', async () => {
    server.use(http.get('/api/orgs/:org/nodes', () => HttpResponse.json({ error: { code: 'internal', message: 'boom' } }, { status: 500 })));
    renderApp(`/${ORG}/nodes`);
    expect(await screen.findByText('Could not load this', {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });

  it('says when a machine in the address is not there', async () => {
    renderApp(`/${ORG}/nodes/gone-box`);
    expect(await screen.findByRole('heading', { name: 'No machine called gone-box' }, { timeout: 10_000 })).toBeInTheDocument();
  });

  it('keeps the full page, and its way back to the machine beside the table', async () => {
    renderApp(`/${ORG}/nodes/hetzner-1?tab=health`);
    expect(await screen.findByRole('tab', { name: /Health/ }, { timeout: 10_000 })).toHaveAttribute('aria-selected', 'true');
    const back = within(screen.getByRole('main')).getByRole('link', { name: 'Machines' });
    expect(back).toHaveAttribute('href', `/${ORG}/machines/hetzner-1`);
  }, 15_000);
});
