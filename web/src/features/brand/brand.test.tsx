import { screen, waitFor, within } from '@testing-library/react';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

const data = () => db.orgs.get('northwind-group')!;

describe('Brand', () => {
  it('leads with the waiting proposal and lists the profiles', async () => {
    renderApp('/northwind-group/brand');
    expect(await screen.findByText(/proposes version 4 of Harbor Supply shop/, undefined, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Review version 4/ })).toHaveAttribute('href', '/northwind-group/brand/harbor%2Fshop');
    expect(screen.getByRole('link', { name: /Aster Coffee/ })).toBeInTheDocument();
  });

  it('is a calm page for anyone who is not the owner', async () => {
    data().role = 'viewer';
    renderApp('/northwind-group/brand');
    expect(await screen.findByText("Brand is the owner's to see", undefined, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('opens a profile on Components, with the proposal first, and the owner approves it', async () => {
    const { user } = renderApp('/northwind-group/brand/harbor%2Fshop');
    const proposal = await screen.findByRole('heading', { name: /proposes version 4/ }, { timeout: 10_000 });
    const card = proposal.closest('[data-kind]') as HTMLElement;
    const tokens = within(card).getByRole('table', { name: 'Tokens the proposal changes' });
    expect(within(tokens).getByText('#0E7C66')).toBeInTheDocument();
    expect(within(tokens).getByText('#1F5FAD')).toBeInTheDocument();
    expect(within(card).getByText('One accent colour per screen')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Components' }, { timeout: 10_000 })).toBeInTheDocument();
    expect(await screen.findByText(/Drawn from tokens: color.accent, color.text-on-accent/, undefined, { timeout: 10_000 })).toBeInTheDocument();

    await user.click(within(card).getByRole('button', { name: /Approve version 4/ }));
    expect(await screen.findByText('Version 4 is live', undefined, { timeout: 10_000 })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('heading', { name: /proposes version 4/ })).not.toBeInTheDocument(), { timeout: 10_000 });
    expect(screen.getByText(/Version 4, approved on/)).toBeInTheDocument();
  }, 45_000); // waits for several screens and an approval, each up to 10 s; the defaults fail on a busy machine
});
