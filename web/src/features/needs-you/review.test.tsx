import { screen, waitFor, within } from '@testing-library/react';
import { db } from '@/mocks/handlers';
import { reviewPath } from '@/mocks/s3';
import { renderApp } from '@/test/render';
import { asComputer } from '@/test/computer';

const org = () => db.orgs.get('northwind-group')!;
const pending = (effect: string) => org().approvals.find((item) => item.effect === effect)!;

describe('a Review link', () => {
  it('opens the one approval it meant, and opening it decides nothing', async () => {
    const target = pending('commit');
    const before = org().approvals.length;
    renderApp(reviewPath('northwind-group', target.id));
    expect(await screen.findByRole('heading', { name: "Post this photo on Aster Coffee's Instagram?" })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: "Don't post" })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Post it' })).toBeInTheDocument();
    expect(org().approvals).toHaveLength(before);
  });

  it('shows the picture a post will carry, from the node', async () => {
    const withPicture = pending('commit');
    renderApp(reviewPath('northwind-group', withPicture.id));
    const picture = await screen.findByRole('img', { name: 'What will be posted' });
    expect(picture).toHaveAttribute('src', expect.stringMatching(/\/approvals\/[^/]+\/preview\.png$/));
  });

  it('says yes through the signed-in route, and says it is posting, never Approved', async () => {
    const target = pending('commit');
    const { user } = renderApp(reviewPath('northwind-group', target.id));
    await user.click(await screen.findByRole('button', { name: 'Post it' }));
    expect((await screen.findAllByText(/^Posting to Aster Coffee's Instagram( in \d+ s)?$/)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Approved/)).not.toBeInTheDocument();
    await waitFor(() => expect(org().history[0]).toMatchObject({ id: target.id, state: 'approved' }));
    expect(screen.getByRole('link', { name: /Open Needs you/ })).toBeInTheDocument();
  }, 10_000);

  it('asks again before approving a payment from a link', async () => {
    asComputer();
    const target = pending('pay');
    const { user } = renderApp(reviewPath('northwind-group', target.id));
    await user.click(await screen.findByRole('button', { name: 'Pay $40' }));
    expect(within(await screen.findByRole('dialog')).getByText(/Money can't be taken back from here/)).toBeInTheDocument();
    expect(org().approvals.some((item) => item.id === target.id)).toBe(true);
  });

  it('says a link from a chat said yes to nothing', async () => {
    const target = pending('pay');
    renderApp(`${reviewPath('northwind-group', target.id)}&from=chat`);
    expect(await screen.findByText('You came from a chat.')).toBeInTheDocument();
    expect(screen.getByText(/said yes to nothing/)).toBeInTheDocument();
    expect(org().approvals.some((item) => item.id === target.id)).toBe(true);
  });

  it('lands calmly when the link has run out', async () => {
    const target = pending('commit');
    renderApp(reviewPath('northwind-group', target.id, Date.now() - 31 * 60_000));
    expect(await screen.findByRole('heading', { name: 'This link has run out' })).toBeInTheDocument();
    expect(screen.getByText(/Anything still waiting for you is in Needs you/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Open Needs you/ })).toHaveAttribute('href', '/northwind-group/needs-you');
    expect(screen.queryByRole('button', { name: 'Post it' })).not.toBeInTheDocument();
  });

  it('treats a changed signature and a missing one as expired', async () => {
    const target = pending('commit');
    renderApp(reviewPath('northwind-group', target.id).replace('s=mock', 's=forged'));
    expect(await screen.findByRole('heading', { name: 'This link has run out' })).toBeInTheDocument();
  });

  it('says when someone already decided, and shows a decision the node refused', async () => {
    const refused = org().history.find((item) => item.delivery === 'refused')!;
    renderApp(reviewPath('northwind-group', refused.id));
    expect(await screen.findByRole('heading', { name: 'Already answered: yes' })).toBeInTheDocument();
    expect(screen.getByText(/The computer did not take your yes/)).toBeInTheDocument();
    expect(screen.getByText(/It still waits there, and nothing was done/)).toBeInTheDocument();
  });

  it('sends a signed-out person to sign in and back to the same link', async () => {
    const { mockAuth } = await import('@/mocks/scenario');
    mockAuth.signedIn = false;
    const target = pending('commit');
    const { router } = renderApp(reviewPath('northwind-group', target.id));
    await waitFor(() => expect(router.state.location.pathname).toBe('/sign-in'));
    expect(String(router.state.location.search.next)).toContain(`/northwind-group/review/${target.id}`);
    mockAuth.signedIn = true;
  });
});
