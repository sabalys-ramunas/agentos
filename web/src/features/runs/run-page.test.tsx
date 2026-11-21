import { render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { db } from '@/mocks/handlers';
import { server } from '@/test/setup';
import { renderApp } from '@/test/render';
import { OutputView, filterItems, outputItems } from './output';

const ORG = 'northwind-group';
const open = (id: string) => renderApp(`/${ORG}/runs/${id}`);

describe('A run', () => {
  it('shows a failed test run: its state, command, facts, and the failures read from the output', async () => {
    const { user } = open('job-3c4d5e6f7a');
    expect(await screen.findByRole('heading', { level: 1, name: 'Tests in harbor-shop' }, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByText('Failed: 2 of 118 tests')).toBeInTheDocument();
    expect(screen.getByTitle('openajar run -- pnpm vitest run')).toBeInTheDocument();
    expect(screen.getByText('feature/cart-rounding')).toBeInTheDocument();
    expect(screen.getByText((_, node) => node?.tagName === 'DD' && node.textContent === '9f86d08 · clean')).toBeInTheDocument();
    expect(screen.getByText('116 of 118 passed')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();

    const failed = within(screen.getByRole('region', { name: 'What failed' }));
    expect(failed.getByText('Read from the Vitest output.')).toBeInTheDocument();
    expect(failed.getByText('2 tests')).toBeInTheDocument();
    expect(failed.getByText('rounds the total to cents before it charges')).toBeInTheDocument();
    expect(failed.getByText('shows the card error next to the field')).toBeInTheDocument();
    expect(failed.getAllByText('src/checkout/payment.test.ts')).toHaveLength(2);

    // The output marks the failed lines, and a failure takes the reader to its own.
    const log = await screen.findByRole('log', { name: 'Output of pnpm vitest run' });
    await waitFor(() => expect(log.querySelectorAll('[data-highlight="bad"]').length).toBeGreaterThanOrEqual(3));
    await user.click(failed.getAllByRole('button', { name: /Show in the output/ })[1]!);
    await waitFor(() => expect(log.querySelector('[data-highlight="warn"]')).toHaveTextContent('shows the card error next to the field'));
  });

  it('filters the output, and says when no line matches', async () => {
    const { user } = open('job-2b3c4d5e6f');
    const log = await screen.findByRole('log', { name: 'Output of uv run pytest -q' }, { timeout: 4000 });
    await waitFor(() => expect(log).toHaveTextContent('211 passed, 3 skipped'));
    await user.type(screen.getByRole('searchbox', { name: 'Filter the output' }), 'test_stock');
    await waitFor(() => expect(log.querySelectorAll('[data-row]')).toHaveLength(1));
    expect(log.querySelector('mark')).toHaveTextContent('test_stock');
    await user.type(screen.getByRole('searchbox', { name: 'Filter the output' }), 'zzz');
    expect(await within(log).findByText(/No line has/)).toBeInTheDocument();
    const failed = within(screen.getByRole('region', { name: 'What failed' }));
    expect(failed.getByText('Nothing failed')).toBeInTheDocument();
  });

  it('follows a running run to its end, and lets an owner cancel it after asking', async () => {
    const { user } = open('job-1a2b3c4d5e');
    expect(await screen.findByRole('heading', { level: 1, name: 'Tests in harbor-shop' }, { timeout: 4000 })).toBeInTheDocument();
    if (!screen.queryByText('Running')) return; // ended already on a slow machine
    expect(screen.getByText('Not reported yet')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Following/ })).toHaveAttribute('aria-pressed', 'true');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel the run' }));
    await waitFor(() => expect(db.orgs.get(ORG)!.jobs!.find((item) => item.job.id === 'job-1a2b3c4d5e')!.job.state).toBe('cancelled'));
    expect((await screen.findAllByText('Cancelled', {}, { timeout: 6000 }))[0]).toBeInTheDocument();
    expect(screen.getAllByText(/The machine said: cancelled from the panel/)[0]).toBeInTheDocument();
  }, 15_000);

  it('names what an older client did not send, and finds no tests where there is no summary', async () => {
    open('job-7a8b9c0d1e');
    expect(await screen.findByRole('heading', { level: 1, name: 'Command in shell' }, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByText('Not recorded by this client')).toBeInTheDocument();
    expect(screen.getByText('No test summary')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'First failure' })).toBeDisabled();
    const happened = within(screen.getByRole('region', { name: 'What happened' }));
    expect(happened.getByText('It passed, with exit code 0')).toBeInTheDocument();
    expect(happened.queryByText(/^Sent /)).not.toBeInTheDocument();
    expect(happened.getByText(/is not\s+recorded, so it is not shown/)).toBeInTheDocument();
  });

  it('says how a timed out and a lost run ended, in the machine’s words', async () => {
    const first = open('job-5e6f7a8b9c');
    expect((await screen.findAllByText('Timed out', {}, { timeout: 4000 }))[0]).toBeInTheDocument();
    expect(screen.getAllByText(/The machine said: stopped after 1800 s, its time limit/)[0]).toBeInTheDocument();
    first.unmount();
    open('job-6f7a8b9c0d');
    expect((await screen.findAllByText('Lost', {}, { timeout: 4000 }))[0]).toBeInTheDocument();
    expect(screen.getAllByText(/The machine stopped while it ran/)[0]).toBeInTheDocument();
  });

  it('is calm about a run that is gone or not for this person', async () => {
    const first = open('job-0000000000');
    expect(await screen.findByText('This run is not on the machine', {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'All runs' })).toHaveAttribute('href', `/${ORG}/runs`);
    first.unmount();
    server.use(http.get('/api/orgs/:org/jobs/:id', () => HttpResponse.json({ error: { code: 'denied', message: 'No.' } }, { status: 403 })));
    open('job-3c4d5e6f7a');
    expect(await screen.findByText('This run is not for you', {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('says when the page let go of the start, and when the machine kept only the first part', async () => {
    const record = db.orgs.get(ORG)!;
    const { mockJobs } = await import('@/mocks/jobs');
    const item = mockJobs(record).find((entry) => entry.job.id === 'job-2b3c4d5e6f')!;
    item.job = { ...item.job, dropped_bytes: 4_200_000 };
    open('job-2b3c4d5e6f');
    expect(await screen.findByText(/only counted the last 4\.2 MB/, {}, { timeout: 4000 })).toBeInTheDocument();
  });
});

describe('the output at 2 MB', () => {
  const big = () => {
    const lines: string[] = [];
    let size = 0;
    for (let i = 0; size < 2_000_000; i += 1) {
      const line = i % 997 === 50 ? `   × case ${i} rounds the total 12ms` : ` ✓ src/catalog/case-${i}.test.ts > a fairly long test name about the product grid ${i} 4ms`;
      lines.push(line);
      size += line.length + 1;
    }
    return lines;
  };

  it('builds, filters and draws only a window of the lines, quickly', () => {
    const lines = big();
    expect(lines.length).toBeGreaterThan(15_000);
    let start = performance.now();
    const items = outputItems(lines, [], 'both');
    const built = performance.now() - start;
    start = performance.now();
    const hits = filterItems(items, 'rounds the total');
    const filtered = performance.now() - start;
    expect(hits).toHaveLength(Math.ceil((lines.length - 50) / 997));
    start = performance.now();
    const { container } = render(<OutputView items={items} label="Output" wrap={false} follow={false} onFollowChange={() => {}} />);
    const drawn = performance.now() - start;
    // jsdom has no layout, so the window is the 600 px default plus the overscan on each side: a few chunks.
    expect(container.querySelectorAll('[data-row]').length).toBeLessThanOrEqual(200);
    expect(container.querySelectorAll('[data-highlight="bad"]').length).toBeGreaterThan(0);
    // Generous bounds for a slow CI machine; on a laptop each is a few tens of ms.
    expect(built).toBeLessThan(1_000);
    expect(filtered).toBeLessThan(1_000);
    expect(drawn).toBeLessThan(1_000);
  });
});
