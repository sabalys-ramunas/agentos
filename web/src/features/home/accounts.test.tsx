import { screen, within } from '@testing-library/react';
import { renderApp } from '@/test/render';

describe('Accounts', () => {
  it('lists every account by workspace, with its state in words and the one thing it needs', async () => {
    renderApp('/northwind-group/accounts');
    expect(await screen.findByRole('heading', { level: 1, name: 'Accounts' })).toBeInTheDocument();
    const aster = within(await screen.findByRole('region', { name: 'Aster Coffee' }));
    const reddit = aster.getByRole('article', { name: 'Reddit' });
    expect(within(reddit).getByText('Stuck')).toBeInTheDocument();
    expect(within(reddit).getByRole('link', { name: 'See what happened' })).toHaveAttribute('href', '/northwind-group/accounts/aster/reddit');
    const solstice = within(screen.getByRole('region', { name: 'Solstice Studio' }));
    expect(within(solstice.getByRole('article', { name: 'Facebook' })).getByRole('link', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.queryByText(/\blanes?\b/i)).not.toBeInTheDocument();
  });

  it('narrows to what needs a hand, and to one workspace from its address', async () => {
    const { user } = renderApp('/northwind-group/accounts?workspace=harbor');
    expect(await screen.findByRole('region', { name: 'Harbor Supply' })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Aster Coffee' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: /Need a hand/ }));
    for (const card of within(screen.getByRole('region', { name: 'Harbor Supply' })).getAllByRole('article')) {
      expect(card).toHaveTextContent(/Waiting for you|Stuck/);
    }
  });
});
