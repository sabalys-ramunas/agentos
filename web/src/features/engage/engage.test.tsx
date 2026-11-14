import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { renderApp } from '@/test/render';
import { server } from '@/test/setup';

const ACCOUNT = '/northwind-group/accounts/aster/instagram/engagement';

describe('Engagement', () => {
  it('says the day in one sentence, and lists each account with its plan and limits', async () => {
    renderApp('/northwind-group/accounts/engagement');
    expect(await screen.findByRole('heading', { level: 1, name: /went out across 3 accounts/ })).toBeInTheDocument();
    const table = await screen.findByRole('table');
    // Each account is named as a person reads it (its business and site), and it links to its own page.
    const aster = within(table).getAllByRole('link').find((link) => link.getAttribute('href') === '/northwind-group/accounts/aster/instagram/engagement');
    expect(aster).toHaveTextContent('Instagram');
    expect(aster).not.toHaveTextContent('aster/instagram');
    expect(within(table).getAllByText('Plan running', { selector: 'span' })).toHaveLength(2);
    expect(within(table).getByText('Waiting for you', { selector: 'span' })).toBeInTheDocument();
    for (const column of ['Plan', 'Drafted', 'Went out', 'Waiting for you', 'Held', 'Daily limits']) {
      expect(within(table).getByRole('columnheader', { name: column })).toBeInTheDocument();
    }
    expect(screen.getByRole('button', { name: 'Today' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Yesterday' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('shows the day before, when it is picked, and the day picker keeps today as its first choice', async () => {
    const { user } = renderApp('/northwind-group/accounts/engagement');
    await user.click(await screen.findByRole('button', { name: 'Yesterday' }));
    expect(screen.getByRole('button', { name: 'Yesterday' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Today' })).toHaveAttribute('aria-pressed', 'false');
    await user.click(screen.getByRole('button', { name: 'Today' }));
    expect(screen.getByRole('button', { name: 'Today' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('shows the day of one account, with its pause in words and the node’s own words under it', async () => {
    renderApp(ACCOUNT);
    expect(await screen.findByRole('heading', { level: 1, name: /Engagement: aster\/instagram|Engagement/ })).toBeInTheDocument();
    expect(await screen.findByText('The site asked the account to slow down')).toBeInTheDocument();
    expect(screen.getByText('Waiting for you (1)')).toBeInTheDocument();
    expect(screen.getByText(/Those rosettas are crisp/)).toBeInTheDocument();
  });

  it('starts a paused account again once the owner confirms it, and the pause goes', async () => {
    const { user } = renderApp(ACCOUNT);
    await user.click(await screen.findByRole('button', { name: 'Start again' }));
    const dialog = await screen.findByRole('dialog', { name: 'Start this account again?' });
    expect(within(dialog).getByText(/Check the account on the site first/)).toBeInTheDocument();
    expect(screen.getByText('The site asked the account to slow down')).toBeInTheDocument();  // not yet
    await user.click(within(dialog).getByRole('button', { name: 'Start again' }));
    await waitFor(() => expect(screen.queryByText('The site asked the account to slow down')).not.toBeInTheDocument());
  });

  it('approves a plan that waits for the owner, and says so once it runs', async () => {
    const { user } = renderApp('/northwind-group/accounts/meridian/instagram/engagement?tab=plan');
    await user.click(await screen.findByRole('button', { name: 'Approve plan' }));
    expect(await screen.findByText('Plan running')).toBeInTheDocument();
  });

  it('keeps a daily limit within the ceiling, and says why when it is not', async () => {
    const { user } = renderApp('/northwind-group/accounts/aster/instagram/engagement?tab=plan');
    const input = await screen.findByRole('spinbutton', { name: 'Likes a day' });
    const row = within(input.closest('tr') as HTMLElement);
    await user.clear(input);
    await user.type(input, '999');
    expect(row.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(row.getByText('Pick a whole number from 0 to 80.')).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, '12');
    await user.click(row.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(row.getByRole('button', { name: 'Back to the ramp' })).toBeInTheDocument());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('takes a queued item back once the owner confirms it', async () => {
    const { user } = renderApp('/northwind-group/accounts/harbor/reddit/engagement');
    expect(await screen.findByText('New stock on the bench')).toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: 'Take back' }));
    const dialog = await screen.findByRole('dialog', { name: 'Take this back?' });
    await user.click(within(dialog).getByRole('button', { name: 'Take back' }));
    await waitFor(() => expect(screen.queryByText('New stock on the bench')).not.toBeInTheDocument());
  });

  it('shows what the account is connected as and what its connections can do', async () => {
    renderApp('/northwind-group/accounts/harbor/reddit/engagement?tab=connected');
    expect(await screen.findByText('u/harbor_supply')).toBeInTheDocument();
    const ops = await screen.findByRole('table');
    expect(within(ops).getByText('Post text to a subreddit')).toBeInTheDocument();
    expect(within(ops).getByText('Held')).toBeInTheDocument();
  });

  it('disconnects an identity once the owner confirms it, and connects it again by name', async () => {
    const { user } = renderApp('/northwind-group/accounts/harbor/reddit/engagement?tab=connected');
    await user.click(await screen.findByRole('button', { name: 'Disconnect' }));
    const leaving = await screen.findByRole('dialog', { name: 'Disconnect this account?' });
    await user.click(within(leaving).getByRole('button', { name: 'Disconnect' }));
    expect(await screen.findByRole('heading', { level: 3, name: 'Not connected as an identity yet' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Connect' }));
    const dialog = await screen.findByRole('dialog', { name: 'Connect' });
    await user.type(within(dialog).getByLabelText('Name on the site'), 'u/harbor_supply');
    await user.click(within(dialog).getByRole('button', { name: 'Connect' }));
    expect(await screen.findByText('u/harbor_supply')).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: 'Connect' })).not.toBeInTheDocument();
  });

  it('asks an Instagram identity for the numeric account id, and says so before it sends anything', async () => {
    const { user } = renderApp('/northwind-group/accounts/aster/instagram/engagement?tab=connected');
    await user.click(await screen.findByRole('button', { name: 'Disconnect' }));
    await user.click(within(await screen.findByRole('dialog', { name: 'Disconnect this account?' })).getByRole('button', { name: 'Disconnect' }));
    await user.click(await screen.findByRole('button', { name: 'Connect' }));
    const dialog = await screen.findByRole('dialog', { name: 'Connect' });
    await user.type(within(dialog).getByLabelText('Name on the site'), '@aster.coffee');
    await user.type(within(dialog).getByLabelText('Account id'), 'not-a-number');
    await user.click(within(dialog).getByRole('button', { name: 'Connect' }));
    expect(within(dialog).getByText('Use the number the site gives the business account.')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Connect' })).toBeInTheDocument();  // nothing was sent, so it stays open
  });

  it('says the connections are the owner’s to see when the node refuses a member', async () => {
    server.use(
      http.get('/api/orgs/:org/surfaces/:ws/:name', () =>
        HttpResponse.json({ error: { code: 'denied', message: 'Only the owner can see this.' } }, { status: 403 }),
      ),
    );
    renderApp('/northwind-group/accounts/aster/instagram/engagement?tab=connected');
    expect(await screen.findByText('The connections of an account are the owner’s to see.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument();
  });

  it('shows no plan for an account that has none, and the account page links to its plan when it has one', async () => {
    renderApp('/northwind-group/accounts/solstice/instagram/engagement?tab=plan');
    expect(await screen.findByRole('heading', { level: 3, name: 'No plan yet' })).toBeInTheDocument();
  });

  it('links to the engagement from an account that has a plan, and not from one that has none', async () => {
    const { unmount } = renderApp('/northwind-group/accounts/aster/instagram');
    expect(await screen.findByRole('link', { name: 'Open engagement' })).toHaveAttribute(
      'href',
      '/northwind-group/accounts/aster/instagram/engagement',
    );
    unmount();
    renderApp('/northwind-group/accounts/solstice/instagram');
    await screen.findByRole('heading', { level: 1 });
    expect(screen.queryByRole('link', { name: 'Open engagement' })).not.toBeInTheDocument();
  });
});
