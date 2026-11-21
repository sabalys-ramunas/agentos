import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { db } from '@/mocks/handlers';
import { server } from '@/test/setup';
import { renderApp } from '@/test/render';
import { filterJobs, sortJobs } from './runs-page';
import { summaryOf } from './words';
import type { Job } from '@/api/types';

const ORG = 'northwind-group';

async function table() {
  return within(await screen.findByRole('table', { name: 'Runs' }, { timeout: 4000 }));
}

const rowOf = (t: ReturnType<typeof within>, name: RegExp) => t.getByRole('row', { name });

describe('Runs', () => {
  it('lists every run newest first with its kind, command, state, branch, who and where, and the test totals', async () => {
    renderApp(`/${ORG}/runs`);
    const t = await table();
    const rows = t.getAllByRole('row').slice(1);
    expect(rows).toHaveLength(7);
    expect(rows[0]).toHaveAccessibleName(/^Tests in harbor-shop, (Running|Passed)$/);
    const failed = rowOf(t, /^Tests in harbor-shop, Failed$/);
    expect(within(failed).getByText('pnpm vitest run')).toBeInTheDocument();
    expect(within(failed).getByText(/^feature\/cart-rounding/)).toBeInTheDocument();
    expect(within(failed).getByText(/· 9f86d08/)).toBeInTheDocument();
    expect(within(failed).getByText('116')).toBeInTheDocument();
    expect(within(failed).getByText('2')).toHaveClass('text-bad');
    expect(within(failed).getByTitle(/lane harbor\/run-shop$/)).toBeInTheDocument();
    // An older client sent no branch, and the command printed no test summary: said, not guessed.
    const old = rowOf(t, /^Command in shell, Passed$/);
    expect(within(old).getByText('No branch recorded')).toBeInTheDocument();
    expect(within(old).getAllByLabelText('None')).toHaveLength(2);
    expect(within(rowOf(t, /^Build in harbor-shop/)).getByText('Cancelled')).toBeInTheDocument();
    expect(within(rowOf(t, /^Tests in meridian-site/)).getByText('Timed out')).toBeInTheDocument();
    expect(within(rowOf(t, /^Tests in harbor-api, Lost/)).getByText('Lost')).toBeInTheDocument();
  });

  it('says how the last day went over the table, and where routine and agent runs went', async () => {
    renderApp(`/${ORG}/runs`);
    await table();
    expect(screen.getByText(/In the last 24 hours 4 finished: 1 passed, 1 failed and 2 stopped early\. Their tests: 327 passed and 2 failed\./)).toBeInTheDocument();
    expect(within(screen.getByRole('main')).getByRole('link', { name: 'History' })).toHaveAttribute('href', `/${ORG}/history`);
  });

  it('filters by state, kind and words, and offers to clear when nothing matches', async () => {
    const { user } = renderApp(`/${ORG}/runs`);
    let t = await table();
    await user.click(screen.getByRole('radio', { name: /^Failed\s?\d+$/ }));
    t = await table();
    await waitFor(() => expect(t.getAllByRole('row')).toHaveLength(2));
    expect(rowOf(t, /Failed$/)).toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: /^Stopped early\s?\d+$/ }));
    await waitFor(() => expect(t.getAllByRole('row')).toHaveLength(4));
    await user.click(screen.getByRole('combobox', { name: 'Kind of work' }));
    await user.click(await screen.findByRole('option', { name: /^Build/ }));
    await waitFor(() => expect(t.getAllByRole('row')).toHaveLength(2));
    expect(rowOf(t, /^Build in harbor-shop, Cancelled$/)).toBeInTheDocument();
    await user.type(screen.getByRole('searchbox', { name: 'Search runs' }), 'nothing-like-this');
    expect(await screen.findByText('No run matches')).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: 'Clear filters' })[0]!);
    await waitFor(() => expect(t.getAllByRole('row')).toHaveLength(8));
  });

  it('cancels the running ones from a selection, after asking', async () => {
    const { user } = renderApp(`/${ORG}/runs`);
    const t = await table();
    const running = t.queryByRole('row', { name: /, Running$/ });
    if (!running) return; // the mock's running run may already have finished on a slow machine
    await user.click(within(running).getByRole('checkbox'));
    await user.click(within(rowOf(t, /^Tests in harbor-api, Passed$/)).getByRole('checkbox'));
    const bar = screen.getByRole('toolbar', { name: 'Selection' });
    expect(bar).toHaveTextContent('2 runs selected');
    await user.click(within(bar).getByRole('button', { name: /Cancel 1 running/ }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Cancel the run' }));
    await waitFor(() => expect(db.orgs.get(ORG)!.jobs!.find((item) => item.job.id === 'job-1a2b3c4d5e')!.job.state).toBe('cancelled'));
    await waitFor(() => expect(screen.queryByRole('toolbar', { name: 'Selection' })).not.toBeInTheDocument());
  });

  it('offers no cancel to a viewer', async () => {
    db.orgs.get(ORG)!.role = 'viewer';
    const { user } = renderApp(`/${ORG}/runs`);
    const t = await table();
    await user.click(within(t.getAllByRole('row')[1]!).getByRole('checkbox'));
    const bar = screen.getByRole('toolbar', { name: 'Selection' });
    expect(within(bar).queryByRole('button', { name: /Cancel/ })).not.toBeInTheDocument();
    expect(within(bar).getByRole('button', { name: /Copy ids/ })).toBeInTheDocument();
  });

  it('says calmly when there are no runs, when the computer runs none, and when the role sees none', async () => {
    server.use(http.get('/api/orgs/:org/jobs', () => HttpResponse.json({ available: true, jobs: [] })));
    const first = renderApp(`/${ORG}/runs`);
    expect(await screen.findByText('No runs yet')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Copy the command' })).toBeInTheDocument();
    first.unmount();

    server.use(http.get('/api/orgs/:org/jobs', () => HttpResponse.json({ available: false, reason: 'This computer has no terminal lanes switched on.', jobs: [] })));
    const second = renderApp(`/${ORG}/runs`);
    expect(await screen.findByText('This computer has no terminal lanes switched on.')).toBeInTheDocument();
    second.unmount();

    server.use(http.get('/api/orgs/:org/jobs', () => HttpResponse.json({ error: { code: 'denied', message: 'No.' } }, { status: 403 })));
    renderApp(`/${ORG}/runs`);
    expect(await screen.findByText('Runs are not shown to your role')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('counts the running runs on the rail', async () => {
    renderApp(`/${ORG}/runs`);
    await table();
    const running = db.orgs.get(ORG)!.jobs!.filter((item) => item.job.state === 'running').length;
    if (running) expect((await screen.findAllByLabelText(`${running} running`))[0]).toBeInTheDocument();
  });
});

const job = (over: Partial<Job>): Job => ({
  id: 'job-0000000000',
  lane: 'a/run',
  workspace: 'a',
  lane_name: 'run',
  node: 'n1',
  command: 'pnpm test',
  cwd: null,
  state: 'passed',
  exit_code: 0,
  signal: null,
  oom_killed: false,
  started_at: '2026-11-13T10:00:00Z',
  finished_at: '2026-11-13T10:01:00Z',
  duration_s: 60,
  timeout_s: 3600,
  started_by: 'claude-code',
  stdout_bytes: 0,
  stderr_bytes: 0,
  dropped_bytes: 0,
  sync: null,
  note: null,
  ...over,
});

describe('filterJobs and sortJobs', () => {
  const now = Date.parse('2026-11-13T12:00:00Z');
  const jobs = [
    job({ id: 'a', state: 'running', duration_s: null, finished_at: null, started_at: '2026-11-13T11:59:00Z' }),
    job({ id: 'b', state: 'failed', command: 'pnpm build', duration_s: 5, tests: undefined }),
    job({ id: 'c', state: 'lost', started_at: '2026-11-01T10:00:00Z', node: 'n2', source: { branch: 'fix/x', commit: 'abc', dirty: false, told_by: 'client' } }),
    job({ id: 'd', state: 'passed', duration_s: 300, tests: { runner: 'vitest', passed: 10, failed: 0, skipped: 0, total: 10, source: 'output' } }),
  ];
  it('groups cancelled, timed out and lost as stopped early', () => {
    expect(filterJobs(jobs, { state: 'stopped' }, now).map((item) => item.id)).toEqual(['c']);
  });
  it('filters by kind, machine, range and words in the branch', () => {
    expect(filterJobs(jobs, { kind: 'build' }, now).map((item) => item.id)).toEqual(['b']);
    expect(filterJobs(jobs, { machine: 'n2' }, now).map((item) => item.id)).toEqual(['c']);
    expect(filterJobs(jobs, { range: '24h' }, now).map((item) => item.id)).toEqual(['a', 'b', 'd']);
    expect(filterJobs(jobs, { q: 'FIX/X' }, now).map((item) => item.id)).toEqual(['c']);
  });
  it('sorts by state with running first, and by duration', () => {
    expect(sortJobs(jobs, 'state', 'asc', now).map((item) => item.id)).toEqual(['a', 'b', 'c', 'd']);
    expect(sortJobs(jobs, 'duration', 'desc', now).map((item) => item.id)).toEqual(['d', 'a', 'c', 'b']);
  });
  it('sums the last day in words', () => {
    expect(summaryOf([], now)).toBe('Nothing has run yet.');
    expect(summaryOf(jobs, now)).toBe('1 run going now. In the last 24 hours 3 finished: 1 passed, 1 failed and 1 stopped early. Their tests: 10 passed and 0 failed.');
  });
});

describe('the command bar', () => {
  it('finds a run by its command and opens it', async () => {
    const { user, router } = renderApp(`/${ORG}`);
    await screen.findAllByRole('navigation', { name: 'Main' });
    await user.click(screen.getAllByRole('button', { name: 'Search or run a command' })[0]!);
    const dialog = await screen.findByRole('dialog', { name: 'Search or run a command' });
    await user.type(within(dialog).getByRole('combobox'), 'pytest tests/test_invoices');
    await user.click(await within(dialog).findByRole('option', { name: /^Tests in harbor-api: Lost/ }, { timeout: 4000 }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/${ORG}/runs/job-6f7a8b9c0d`));
    // The address changes before the run page has loaded. A load still running when the file ends
    // lands after jsdom is gone ("window is not defined" on CI), so the test waits for it.
    await waitFor(() => expect(router.state.status).toBe('idle'));
    expect(router.state.isLoading).toBe(false);
  });
});
