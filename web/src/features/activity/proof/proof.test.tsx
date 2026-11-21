import { screen, waitFor, within } from '@testing-library/react';
import { proof } from '@/api/client';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

const ORG = 'northwind-group';

/** Proof is the owner's to share and to check; a member sees the day and nothing that changes it. */
describe('Proof', () => {
  it('says whether the log is intact, and shows each step with its record', async () => {
    renderApp(`/${ORG}/activity/proof`);
    expect(await screen.findByRole('heading', { name: 'Proof' })).toBeInTheDocument();
    expect(await screen.findByText('The log is intact.')).toBeInTheDocument();
    const table = await screen.findByRole('table');
    expect(within(table).getAllByText('Signed').length).toBeGreaterThan(0);
  });

  it('shares a day as a link that is shown once, and lists it until it is stopped', async () => {
    const { user } = renderApp(`/${ORG}/activity/proof`);
    await user.click(await screen.findByRole('button', { name: 'Share this day' }));
    const dialog = await screen.findByRole('dialog', { name: 'Share proof' });
    await user.click(within(dialog).getByRole('button', { name: 'Create link' }));
    const made = await screen.findByRole('dialog', { name: 'Your link' });
    expect(within(made).getByText(/#token=shr_mock_/)).toBeInTheDocument();
    expect(within(made).getByText(/Shown once, so copy it now/)).toBeInTheDocument();
    await user.click(within(made).getByRole('button', { name: 'Done' }));

    expect((await screen.findAllByText('Active')).length).toBeGreaterThan(0);

    await user.click(screen.getByRole('button', { name: 'Stop link' }));
    const confirm = await screen.findByRole('dialog', { name: 'Stop this link?' });
    await user.click(within(confirm).getByRole('button', { name: 'Stop link' }));
    await waitFor(() => expect(screen.getAllByText('Stopped').length).toBeGreaterThan(0));
    expect(screen.queryByRole('button', { name: 'Stop link' })).toBeNull();
  });

  it('hides the log check, sharing and the list of links from anyone but the owner', async () => {
    db.orgs.get(ORG)!.role = 'admin';
    renderApp(`/${ORG}/activity/proof`);
    expect(await screen.findByRole('heading', { name: 'Proof' })).toBeInTheDocument();
    await screen.findByRole('table');
    expect(screen.queryByText('The log is intact')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Share this day' })).toBeNull();
    expect(screen.queryByText('Read-only links')).toBeNull();
    expect(screen.queryByRole('button', { name: /^Share this step/ })).toBeNull();
  });

  it('refuses a share the mock does not know the account of, and says nothing was shared', async () => {
    await expect(proof.createShare(ORG, { lane: 'nope/none', day: '2026-11-01' })).rejects.toMatchObject({ status: 404 });
    await expect(proof.createShare(ORG, { ttl: '30d', trace: 'x' })).rejects.toMatchObject({ status: 400 });
    expect(db.orgs.get(ORG)!.proofShares ?? []).toHaveLength(0);
  });
});
