import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { STUDIO_MISSING } from '@/mocks/studio';
import { server } from '@/test/setup';
import { renderApp } from '@/test/render';

const CHECKS = '/northwind-group/checks';
const BLOCKED = `${CHECKS}/run_shopblock01`;

describe('Checks', () => {
  it('lists every check with its result, findings by severity and the design system share', async () => {
    renderApp(CHECKS);
    const table = await screen.findByRole('table', { name: 'Checks, newest first' }, { timeout: 10_000 });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows.length).toBeGreaterThanOrEqual(3);
    const blocked = rows.find((row) => within(row).queryByText('2 blockers'))!;
    expect(within(blocked).getByText('3 of 4')).toBeInTheDocument();
    expect(within(blocked).getByText('1 failed')).toBeInTheDocument();
    expect(within(blocked).getByRole('meter', { name: /Values on a design token/ })).toHaveAttribute('aria-valuenow', '78');
    expect(screen.getByText(/^\d+ checks of \d+ pages\./)).toBeInTheDocument();
  });

  it('filters by page, in the address', async () => {
    const { user, router } = renderApp(CHECKS);
    const pages = await screen.findByRole('group', { name: 'Page' }, { timeout: 10_000 });
    await user.click(within(pages).getByRole('button', { name: /\/cart/ }));
    expect(router.state.location.search).toMatchObject({ page: 'https://shop.harborsupply.example/cart' });
    const table = screen.getByRole('table', { name: 'Checks, newest first' });
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(2));
  }, 20_000);

  it('says calmly when studio is not on this computer, with the command that adds it', async () => {
    server.use(http.get('/api/orgs/:org/studio/runs', () => HttpResponse.json(STUDIO_MISSING)));
    renderApp(CHECKS);
    expect(await screen.findByText('Studio is not on this computer', undefined, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText((_, element) => element?.tagName === 'CODE' && element.textContent === 'uv sync --extra studio')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('An open check', () => {
  it('opens the finding the address names, with what was measured and a copy for the agent', async () => {
    renderApp(`${BLOCKED}?finding=3`);
    const picked = await screen.findByRole('button', { name: /^Finding 3: Text is cut off/, pressed: true }, { timeout: 10_000 });
    expect(picked.closest('li')).toBeInTheDocument();
    // The open finding is its own card beside the picture.
    const item = screen.getByRole('region', { name: /^Finding 3: Text is cut off/ });
    expect(within(item).getByText('hidden 0.42')).toBeInTheDocument();
    expect(within(item).getByText('Let the title wrap to two lines on phones.')).toBeInTheDocument();
    expect(within(item).getByRole('button', { name: 'Copy for the agent' })).toBeInTheDocument();
    // Coverage says what did not run, in words.
    expect(screen.getByText(/1280 dark did not render: The page did not finish loading in 30 s\./)).toBeInTheDocument();
  });

  it('steps through findings with J and K, and moves to the screen the finding is on', async () => {
    const { user, router } = renderApp(`${BLOCKED}?finding=1`);
    await screen.findByRole('button', { name: /^Finding 1:/, pressed: true }, { timeout: 10_000 });
    await user.keyboard('j');
    await waitFor(() => expect(router.state.location.search).toMatchObject({ finding: 2, screen: 'default@360-light' }));
    await user.keyboard('k');
    await waitFor(() => expect(router.state.location.search).toMatchObject({ finding: 1, screen: 'default@360-dark' }));
    await user.keyboard('3');
    await waitFor(() => expect(router.state.location.search).toMatchObject({ screen: 'default@1280-light' }));
  });

  it('reads well when the pictures are gone', async () => {
    renderApp(`${CHECKS}/run_shopcart001`);
    expect(await screen.findByText(/The pictures of this check are no longer kept/, undefined, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Finding 1: Too small to tap/ })).toBeInTheDocument();
  });

  it('shows a check outside your workspaces as not yours, not as an error', async () => {
    server.use(
      http.get('/api/orgs/:org/studio/runs/:id', () =>
        HttpResponse.json({ error: { code: 'denied', message: 'This check is not in a workspace you can see.' } }, { status: 403 }),
      ),
    );
    renderApp(BLOCKED);
    expect(await screen.findByText('This check is not in a workspace you can see', undefined, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('says causes first for what the change did, against the page baseline', async () => {
    renderApp(`${CHECKS}/run_shopclean01?screen=default@360-light`);
    expect(await screen.findByText('2 of 4 screens changed: 3 causes.', undefined, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText(/The checkout button moved down 22 px/)).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'What changed' })).toBeInTheDocument();
  });
});
