import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { db } from '@/mocks/handlers';
import { holdRunningJobs } from '@/mocks/jobs';
import { server } from '@/test/setup';
import { renderApp } from '@/test/render';
import type { Job } from '@/api/types';

function job(over: Partial<Job> & Pick<Job, 'id'>): Job {
  return {
    lane: 'harbor/run-shop', workspace: 'harbor', lane_name: 'run-shop', node: 'hetzner-1', command: 'pnpm vitest run', cwd: null,
    state: 'running', exit_code: null, signal: null, oom_killed: false, started_at: new Date().toISOString(), finished_at: null,
    duration_s: null, timeout_s: 3600, started_by: null, stdout_bytes: 0, stderr_bytes: 0, dropped_bytes: 0, sync: null, note: null,
    ...over,
  };
}

describe('Home', () => {
  it('answers with one sentence first, and what needs you is the same number as the rail', async () => {
    renderApp('/northwind-group');
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/(running|Nothing is running)/), { timeout: 8000 });
    const needs = await screen.findByRole('link', { name: /^\d+ things need you$/ }, { timeout: 8000 });
    expect(needs).toHaveAttribute('href', '/northwind-group/needs-you');
    const total = /^(\d+)/.exec(needs.textContent ?? '')?.[1];
    // The count the shell builder found apart (Home 26, the rail 27): one count now, everywhere.
    const rail = screen.getAllByRole('navigation', { name: 'Main' })[0]!;
    await waitFor(() => expect(within(rail).getByRole('link', { name: /Needs you/ })).toHaveTextContent(total!));
    const panel = within(screen.getByRole('region', { name: /^Needs you/ }));
    await waitFor(() => expect(panel.getByRole('link', { name: `See all ${total}` })).toHaveAttribute('href', '/northwind-group/needs-you'));
  });

  it('shows what runs now as cards, each with its kind and one action', async () => {
    // The seeded run stays running for this test: the mock would otherwise end it after a few reads,
    // and the rail and Home both read the runs.
    holdRunningJobs(db.orgs.get('northwind-group')!);
    renderApp('/northwind-group');
    const slider = within(await screen.findByRole('region', { name: 'Running now' }, { timeout: 8000 }));
    expect(await slider.findByRole('link', { name: 'Watch the output' }, { timeout: 8000 })).toHaveAttribute('href', '/northwind-group/runs/job-1a2b3c4d5e');
    expect(slider.getAllByText('Tests').length).toBeGreaterThan(0);
    expect(slider.getAllByText('pnpm vitest run').length).toBeGreaterThan(0);
    expect(slider.getByText('run-shop on feature/cart-rounding')).toBeInTheDocument();
  });

  it('lists the heaviest things that wait, each with what a yes does and a Look that opens it', async () => {
    renderApp('/northwind-group');
    const panel = within(await screen.findByRole('region', { name: /^Needs you/ }, { timeout: 8000 }));
    await waitFor(() => expect(panel.getAllByRole('listitem')).toHaveLength(3), { timeout: 8000 });
    // Money first, as on Needs you: a payment, a refund or a VM to rent.
    expect(panel.getAllByRole('listitem')[0]).toHaveTextContent(/Pay|Refund|Rent a VM/);
    expect(panel.getAllByText('If yes').length).toBeGreaterThan(0);
    expect(panel.getAllByRole('link', { name: /^Look: / })[0]).toHaveAttribute('href', expect.stringMatching(/^\/northwind-group\/needs-you\?approval=/));
  });

  it('says a failed run plainly, with what it printed, and never that someone is fixing it', async () => {
    const now = Date.now();
    const failed = job({ id: 'job-failed01', state: 'failed', exit_code: 1, started_at: new Date(now - 5 * 60_000).toISOString(), finished_at: new Date(now - 4 * 60_000).toISOString(), tests: { runner: 'vitest', passed: 116, failed: 2, skipped: 0, total: 118, source: 'output' } });
    server.use(
      http.get('/api/orgs/:org/jobs', () => HttpResponse.json({ available: true, jobs: [failed] })),
      http.get('/api/orgs/:org/jobs/:id', () =>
        HttpResponse.json({ ...failed, output: { stdout: ' × rounds the total to cents before it charges 12ms\n', stderr: 'AssertionError: expected 41.999999 to be 42\n', stdout_offset: 0, stderr_offset: 0, next_stdout_offset: 50, next_stderr_offset: 44, more: false } }),
      ),
    );
    renderApp('/northwind-group');
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('A test run failed, and it has not run again since.'), { timeout: 8000 });
    expect(screen.getByRole('heading', { level: 1 })).not.toHaveTextContent(/fixing/);
    const problems = within(screen.getByRole('region', { name: /^Problems/ }));
    expect(problems.getByText('2 tests failed in run-shop')).toBeInTheDocument();
    expect(await problems.findByText('AssertionError: expected 41.999999 to be 42')).toBeInTheDocument();
    expect(problems.getByText('It has not run again since.')).toBeInTheDocument();
    expect(problems.getByRole('link', { name: 'See the failures' })).toHaveAttribute('href', '/northwind-group/runs/job-failed01');
    // A stuck account is a problem too, with its business.
    expect(problems.getByText(/^Aster Coffee's Reddit/)).toBeInTheDocument();
  });

  it('keeps 20 running jobs in one slider, with filter chips by kind and a pager', async () => {
    const now = Date.now();
    const many = Array.from({ length: 20 }, (_, index) =>
      job({ id: `job-many${String(index).padStart(2, '0')}`, command: index % 3 ? 'pnpm vitest run' : 'pnpm build', started_at: new Date(now - (index + 1) * 60_000).toISOString() }),
    );
    server.use(
      http.get('/api/orgs/:org/jobs', () => HttpResponse.json({ available: true, jobs: many })),
      http.get('/api/orgs/:org/jobs/:id', () => HttpResponse.json({ ...many[0], output: { stdout: '', stderr: '', stdout_offset: 0, stderr_offset: 0, next_stdout_offset: 0, next_stderr_offset: 0, more: false } })),
    );
    renderApp('/northwind-group');
    const running = within(await screen.findByRole('region', { name: 'Running now' }, { timeout: 8000 }));
    const chips = within(await running.findByRole('group', { name: 'Show' }, { timeout: 8000 }));
    expect(chips.getByRole('button', { name: /^Tests/ })).toBeInTheDocument();
    expect(chips.getByRole('button', { name: /^Builds/ })).toBeInTheDocument();
    expect(running.getByRole('button', { name: 'Later' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/^\d+ jobs are running/);
  });

  it('ends with three links that carry real numbers', async () => {
    renderApp('/northwind-group');
    const foot = within(await screen.findByRole('navigation', { name: 'Earlier today' }));
    const links = foot.getAllByRole('link');
    expect(links.map((link) => link.getAttribute('href'))).toEqual(['/northwind-group/runs', '/northwind-group/checks', '/northwind-group/machines']);
    await waitFor(() => expect(links[2]).toHaveTextContent(/\d+ of \d+ online/), { timeout: 8000 });
  });

  it('has the ten places, the four everyday ones first', async () => {
    renderApp('/northwind-group');
    const rails = await screen.findAllByRole('navigation', { name: 'Main' });
    const rail = within(rails[0]!);
    const words = (link: HTMLElement) => link.querySelector('[data-rail-label]')?.textContent;
    // The developer places show once the node says the organisation has runs and Site APIs.
    await rail.findByRole('link', { name: /Site APIs/ });
    await rail.findByRole('link', { name: /Runs and checks/ });
    expect(rail.getAllByRole('link').map(words)).toEqual([
      'Home',
      'Needs you',
      'Computers',
      'What happened',
      'Accounts',
      'Jobs that repeat',
      'Brand',
      'Runs and checks',
      'Site APIs',
      'Settings',
    ]);
    expect(within(rail.getByRole('group', { name: 'Your business' })).getAllByRole('link').map(words)).toEqual(['Accounts', 'Jobs that repeat', 'Brand']);
  });

  it('puts four places on the phone tab bar and the rest under More', async () => {
    const { user } = renderApp('/northwind-group');
    const bars = await screen.findAllByRole('navigation', { name: 'Main' });
    const tabs = within(bars[1]!);
    expect(tabs.getAllByRole('link').map((item) => item.textContent?.replace(/, \d+ (waiting|open)$/, ''))).toEqual(['Home', 'Needs you', 'Computers', 'What happened']);
    await waitFor(() => expect(tabs.getByRole('button', { name: /^More/ }).getAttribute('aria-label')).toMatch(/Runs and checks, Site APIs/));
    await user.click(tabs.getByRole('button', { name: /^More/ }));
    const more = within(within(await screen.findByRole('dialog', { name: 'More' })).getByRole('navigation', { name: 'More' }));
    expect(more.getAllByRole('link').map((item) => item.textContent)).toEqual(['Accounts', 'Jobs that repeat', 'Brand', 'Runs and checks', 'Site APIs', 'Settings']);
  });

  it('does not offer setup to an approver: their Home is Needs you', async () => {
    const { router } = renderApp('/juniper-labs');
    await waitFor(() => expect(router.state.location.pathname).toBe('/juniper-labs/needs-you'));
    expect(screen.queryByRole('button', { name: 'New workspace' })).not.toBeInTheDocument();
    expect(screen.queryByText('Getting started')).not.toBeInTheDocument();
  });

  it('offers setup to an admin', async () => {
    db.orgs.get('juniper-labs')!.role = 'admin';
    renderApp('/juniper-labs');
    expect(await screen.findByRole('button', { name: 'New workspace' })).toBeInTheDocument();
  });

  it('says so when the org does not exist', async () => {
    renderApp('/no-such-org');
    expect(await screen.findByText('You no longer have access to this org')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Go to Northwind Group' })).toBeInTheDocument();
  });
});
