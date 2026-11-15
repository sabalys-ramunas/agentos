import { act, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { setPaletteOpen } from '@/components/shell/palette-store';
import { db } from '@/mocks/handlers';
import { server } from '@/test/setup';
import { renderApp } from '@/test/render';

const ORG = 'northwind-group';
const org = () => db.orgs.get(ORG)!;
const T = { timeout: 4000 };

describe('Environments', () => {
  it('lists the open ones first, then the newest five closed, and the rest a click away', async () => {
    const { user, router } = renderApp(`/${ORG}/environments`);
    const open = await screen.findByRole('table', { name: 'Open environments' }, T);
    const rows = within(open).getAllByRole('row').slice(1);
    expect(rows.map((row) => row.getAttribute('aria-label'))).toEqual([
      'Fix the flaky checkout test, Starting',
      'Black Friday offer page, Working',
      'Invoice export to CSV, Idle',
    ]);
    expect(within(rows[1]!).getByText('harbor-supply/shop')).toBeInTheDocument();
    expect(within(rows[1]!).getByText('feat/black-friday-offer')).toBeInTheDocument();
    expect(within(rows[1]!).getByText(/^1 h 4[56] min$/)).toBeInTheDocument();
    expect(screen.getByText(/1 working, 1 starting, 1 idle · €/)).toBeInTheDocument();

    const closed = screen.getByRole('table', { name: 'Closed environments' });
    expect(within(closed).getAllByRole('row')).toHaveLength(6);
    await user.click(screen.getByRole('button', { name: 'Show all 6' }));
    expect(within(closed).getAllByRole('row')).toHaveLength(7);
    expect(within(closed).getByRole('row', { name: 'Search results pagination, Failed' })).toBeInTheDocument();

    // The rail counts the open ones.
    const [rail] = screen.getAllByRole('navigation', { name: 'Main' });
    expect(within(rail!).getByRole('link', { name: /Environments/ })).toHaveTextContent('3');

    await user.click(rows[1]!);
    await waitFor(() => expect(router.state.location.pathname).toBe(`/${ORG}/environments/e7k2`));
  });

  it('says what an environment is, and how an agent gets one, when there are none', async () => {
    org().environments = [];
    renderApp(`/${ORG}/environments`);
    expect(await screen.findByText('No environments yet', undefined, T)).toBeInTheDocument();
    expect(screen.getByText(/a cloud computer an agent rents for one chat/)).toBeInTheDocument();
    expect(screen.getByText(/environment_open\(title=/)).toBeInTheDocument();
  });

  it('says calmly when this computer does not serve environments', async () => {
    server.use(http.get('/api/orgs/:org/environments', () => HttpResponse.json({ error: { code: 'not_found', message: 'Not found.' } }, { status: 404 })));
    renderApp(`/${ORG}/environments`);
    expect(await screen.findByText('Shown on your computer', undefined, T)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows the open ones at the top of Home, and nothing when none is open', async () => {
    renderApp(`/${ORG}`);
    const list = await screen.findByRole('list', { name: 'Open environments' }, T);
    expect(within(list).getAllByRole('listitem')).toHaveLength(3);
    expect(within(list).getByRole('link', { name: /Black Friday offer page/ })).toHaveAttribute('href', `/${ORG}/environments/e7k2`);
  });

  it('says on an empty Home that the agents are working in environments', async () => {
    // a node used only for environments has no workspace of accounts: Home does not say nothing runs
    org().workspaces = [];
    renderApp(`/${ORG}`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Your agents are working in 3 environments.' }, T)).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Open environments' })).toBeInTheDocument();
    expect(screen.queryByText('Nothing is running yet.')).not.toBeInTheDocument();
  });

  it('leaves Home without the section when nothing is open', async () => {
    org().environments = org().environments!.filter((item) => item.detail.state === 'closed' || item.detail.state === 'failed');
    renderApp(`/${ORG}`);
    await screen.findByRole('heading', { level: 1 }, T);
    await waitFor(() => expect(screen.queryByText('Running now')).toBeInTheDocument(), T);
    expect(screen.queryByRole('list', { name: 'Open environments' })).not.toBeInTheDocument();
  });
});

describe('One environment', () => {
  it('shows the live browser, the terminal with each job linked to its run, ports, git and the last calls', async () => {
    renderApp(`/${ORG}/environments/e7k2`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Black Friday offer page' }, T)).toBeInTheDocument();
    expect(screen.getByText('Claude Code in T3 Code')).toBeInTheDocument();
    // The browser lane's live view, with take over, as on an account.
    expect(await screen.findByRole('region', { name: 'Control of E7k2 browser' }, T)).toBeInTheDocument();

    const commands = screen.getByRole('table', { name: 'Commands' });
    expect(within(commands).getByRole('link', { name: 'pnpm dev' })).toHaveAttribute('href', `/${ORG}/runs/job-8c1f0e2a3b`);
    expect(within(commands).queryByRole('link', { name: 'git switch -c feat/black-friday-offer' })).not.toBeInTheDocument();

    expect(screen.getByRole('link', { name: 'Open port 3000, next-server' })).toHaveAttribute('target', '_blank');
    expect(screen.getByText('On the machine only')).toBeInTheDocument();
    expect(screen.getByText('a41c9e2')).toBeInTheDocument();
    // Each pushed branch opens its pull request on GitHub, in a new tab.
    const review = screen.getAllByRole('link', { name: 'Open a pull request for feat/black-friday-offer' });
    expect(review).toHaveLength(2);
    expect(review[0]).toHaveAttribute('href', 'https://github.com/harbor-supply/shop/compare/feat/black-friday-offer?expand=1');
    expect(review[0]).toHaveAttribute('target', '_blank');
    expect(screen.getByRole('list', { name: 'Calls, newest first' }).children).toHaveLength(10);
  });

  it('opens a job from the terminal on its run page', async () => {
    const { user, router } = renderApp(`/${ORG}/environments/e7k2`);
    await user.click(await screen.findByRole('link', { name: 'pnpm install' }, T));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/${ORG}/runs/job-7b0e9d1c2a`));
    expect(await screen.findByText(/Done in 21.4s/, undefined, T)).toBeInTheDocument();
  });

  it('closes a clean one after asking', async () => {
    const { user } = renderApp(`/${ORG}/environments/e7k2`);
    await user.click(await screen.findByRole('button', { name: 'Close' }, T));
    const dialog = await screen.findByRole('dialog', { name: 'Close this environment?' });
    await user.click(within(dialog).getByRole('button', { name: 'Close it' }));
    await waitFor(() => expect(org().environments!.find((item) => item.detail.id === 'e7k2')!.detail.state).toBe('closing'));
    expect(await screen.findByText('Closing', undefined, T)).toBeInTheDocument();
  });

  it('shows the unsaved work when the node refuses, and discards it only after asking again', async () => {
    const { user } = renderApp(`/${ORG}/environments/w8tp`);
    await user.click(await screen.findByRole('button', { name: 'Close' }, T));
    await user.click(within(await screen.findByRole('dialog', { name: 'Close this environment?' })).getByRole('button', { name: 'Close it' }));
    const unsaved = await screen.findByRole('dialog', { name: 'This environment has unsaved work' });
    expect(within(unsaved).getByText('src/invoices/export-button.tsx')).toBeInTheDocument();
    expect(within(unsaved).getByText('9a3c7f0 Invoices: one row per line item, with the VAT rate')).toBeInTheDocument();
    expect(org().environments!.find((item) => item.detail.id === 'w8tp')!.detail.state).toBe('idle');

    await user.click(within(unsaved).getByRole('button', { name: 'Discard and close' }));
    const again = await screen.findByRole('dialog', { name: 'Discard the work and close?' });
    expect(again).toHaveTextContent('3 changed files and 2 commits not pushed are deleted with the machine');
    await user.click(within(again).getByRole('button', { name: 'Discard and close' }));
    await waitFor(() => expect(org().environments!.find((item) => item.detail.id === 'w8tp')!.detail.state).toBe('closing'));
  });

  it('extends one by the time picked', async () => {
    const { user } = renderApp(`/${ORG}/environments/w8tp`);
    const before = Date.parse(org().environments!.find((item) => item.detail.id === 'w8tp')!.detail.expires_at);
    await user.click(await screen.findByRole('button', { name: 'Extend' }, T));
    await user.click(await screen.findByRole('menuitem', { name: 'Extend by 1 hour' }));
    await waitFor(() => expect(Date.parse(org().environments!.find((item) => item.detail.id === 'w8tp')!.detail.expires_at)).toBe(before + 3_600_000));
  });

  it('is a read-only summary once closed, with why it closed', async () => {
    renderApp(`/${ORG}/environments/v6up`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Upgrade Vite to 6' }, T)).toBeInTheDocument();
    expect(screen.getByText('It was closed after an hour with no call from the agent.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Close' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Extend' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Browser' })).not.toBeInTheDocument();
    expect(screen.getByText('Ran for')).toBeInTheDocument();
  });

  it('offers no Close or Extend to a viewer', async () => {
    org().role = 'viewer';
    renderApp(`/${ORG}/environments/e7k2`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Black Friday offer page' }, T)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Close' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Extend' })).not.toBeInTheDocument();
  });

  it('says plainly when the id is not there', async () => {
    renderApp(`/${ORG}/environments/zzzz`);
    expect(await screen.findByText('This environment is not here', undefined, T)).toBeInTheDocument();
  });
});

describe('an environment in the command bar', () => {
  it('is found by its title, under Environments, and opens its page', async () => {
    const { user, router } = renderApp(`/${ORG}`);
    act(() => setPaletteOpen(true));
    await user.type(await screen.findByRole('combobox'), 'black friday');
    const option = await screen.findByRole('option', { name: /Black Friday offer page/ }, T);
    expect(screen.queryByText('Disposable VMs')).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /env\/e7k2/ })).not.toBeInTheDocument();
    await user.click(option);
    await waitFor(() => expect(router.state.location.pathname).toBe(`/${ORG}/environments/e7k2`));
  });
});

describe('the proofs of an environment', () => {
  it('shows each kept screenshot with what it shows, when and which page, and opens it large', async () => {
    const { user } = renderApp(`/${ORG}/environments/e7k2`);
    const gallery = await screen.findByRole('list', { name: 'Proofs, newest first' }, T);
    const items = within(gallery).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(within(items[0]!).getByText('The offer page on a phone')).toBeInTheDocument();
    expect(within(items[0]!).getByText(/localhost:3000\/offer/)).toBeInTheDocument();
    expect(within(items[0]!).getByText(/^\d+m ago$/)).toBeInTheDocument();
    await user.click(within(items[1]!).getByRole('button', { name: 'Open the proof: The offer banner on the home page' }));
    const dialog = await screen.findByRole('dialog', { name: 'The offer banner on the home page' });
    expect(within(dialog).getByRole('img', { name: 'The offer banner on the home page' })).toHaveAttribute(
      'src',
      `/api/orgs/${ORG}/environments/e7k2/proofs/pf_offer_desktop.png`,
    );
  });

  it('keeps them after the environment closed', async () => {
    renderApp(`/${ORG}/environments/p2lc`);
    const gallery = await screen.findByRole('list', { name: 'Proofs, newest first' }, T);
    expect(within(gallery).getByText('The new pricing page copy')).toBeInTheDocument();
  });

  it('says in plain words when there are none yet', async () => {
    renderApp(`/${ORG}/environments/q3mn`);
    expect(await screen.findByText(/No proofs yet\. When the agent keeps a screenshot/, undefined, T)).toBeInTheDocument();
  });
});
