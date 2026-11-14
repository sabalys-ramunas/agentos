import { screen } from '@testing-library/react';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

const PATH = '/northwind-group/needs-you';
const org = () => db.orgs.get('northwind-group')!;
const openInstagram = async (user: ReturnType<typeof renderApp>['user']) => {
  await user.click(await screen.findByRole('button', { name: /^Instagram post, Aster Coffee/ }));
};

describe('the tap line on a decision', () => {
  it('tells a person who can answer how to answer', async () => {
    const { user } = renderApp(PATH);
    await openInstagram(user);
    expect(await screen.findByText(/Just a tap/)).toBeInTheDocument();
  }, 15_000);

  it('is not offered to someone who cannot answer: no "just a tap", no chat line', async () => {
    org().role = 'viewer';
    const { user } = renderApp(PATH);
    await openInstagram(user);
    expect(await screen.findByText('You can see this, but your role cannot answer it.')).toBeInTheDocument();
    expect(screen.queryByText(/Just a tap/)).not.toBeInTheDocument();
    expect(screen.queryByText(/answer this in your chat/)).not.toBeInTheDocument();
  }, 15_000);
});
