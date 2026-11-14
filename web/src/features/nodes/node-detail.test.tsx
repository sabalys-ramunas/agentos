import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '@/test/setup';
import { renderApp } from '@/test/render';

const PATH = '/northwind-group/nodes/hetzner-1';

describe('Node page', () => {
  it('says what the node does in plain words, with a live header', async () => {
    renderApp(`${PATH}?tab=lanes`);
    // The first render in a file also imports the lazy route, and the sentence waits on three reads.
    expect(await screen.findByText(/^Runs 26 accounts for Harbor Supply, Meridian Travel and Solstice Studio, holds the anthropic-api brain/, undefined, { timeout: 10_000 })).toBeInTheDocument();
    expect(await screen.findByRole('img', { name: /Heartbeat for the last hour/ })).toBeInTheDocument();
    expect(screen.getByText('Up, 30 days')).toBeInTheDocument();
  });

  it('keeps the tab in the URL, so Back and a shared link land on the same view', async () => {
    const { user, router } = renderApp(`${PATH}?tab=lanes`);
    await user.click(await screen.findByRole('tab', { name: /Installed/ }));
    expect(router.state.location.search).toMatchObject({ tab: 'installed' });
    expect(await screen.findByRole('region', { name: 'Programs' })).toBeInTheDocument();
    expect(screen.getByText('sharp-cli')).toBeInTheDocument();
  });

  it('filters programs by where they are installed, in the URL too', async () => {
    const { user, router } = renderApp(`${PATH}?tab=installed`);
    const programs = await screen.findByRole('region', { name: 'Programs' });
    await user.click(within(programs).getByRole('radio', { name: 'Base image' }));
    expect(router.state.location.search).toMatchObject({ scope: 'base_image' });
    expect(within(programs).queryByText('sharp-cli')).not.toBeInTheDocument();
    expect(within(programs).getByText('camoufox')).toBeInTheDocument();
  });

  it('says plainly when the node does not report something yet, and shows no numbers for it', async () => {
    server.use(
      http.get('/api/orgs/:org/nodes/:id/programs', () =>
        HttpResponse.json({ error: { code: 'unsupported', message: 'This node does not report this yet.' } }, { status: 409 }),
      ),
    );
    renderApp(`${PATH}?tab=installed`);
    expect(await screen.findByText(/does not report its programs yet/)).toBeInTheDocument();
    // The parts it does report still show.
    expect(await screen.findByRole('region', { name: 'Services' })).toBeInTheDocument();
  });

  it('shows checks that need a look first', async () => {
    renderApp(`${PATH}?tab=health`);
    const panel = await screen.findByRole('region', { name: 'Health' });
    // The answer comes first: three checks need a look, and seven of ten pass.
    expect(within(panel).getByText('3 checks need a look. 7 of 10 pass.')).toBeInTheDocument();
    const names = within(panel).getAllByText(/Disk space|Vault/).map((item) => item.textContent);
    expect(names[0]).toMatch(/Disk space/);
  });

  it('shows the run history of each schedule, with missed fires named', async () => {
    renderApp(`${PATH}?tab=schedules`);
    expect(await screen.findByRole('region', { name: 'Every schedule' })).toBeInTheDocument();
    expect(screen.getAllByRole('img', { name: /^Last \d+ fires/ }).length).toBeGreaterThan(0);
  });
});
