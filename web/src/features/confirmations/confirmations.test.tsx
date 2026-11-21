import { screen, waitFor, within } from '@testing-library/react';
import { db } from '@/mocks/handlers';
import { renderApp, renderToasts } from '@/test/render';
import { asComputer } from '@/test/computer';

describe('what needs a yes', () => {
  it('draws the day in words before any control', async () => {
    renderApp('/northwind-group/settings/yes');
    expect(await screen.findByText(/things got done\. \d+ went on their own, and 11 stopped for a person\./)).toBeInTheDocument();
  });

  it('never offers money less than a tap in openajar, and says why', async () => {
    renderApp('/northwind-group/settings/yes');
    const money = await screen.findByRole('radiogroup', { name: 'How strong a yes: Money' });
    const stops = within(money).getAllByRole('radio').map((stop) => stop.getAttribute('aria-label'));
    expect(stops).toEqual(['A tap in openajar', "Prove it's you"]);
    expect(screen.getByText(/Money sent can't be taken back from here, so only how strong the yes is can change/)).toBeInTheDocument();
  });

  it('shows no level name to a person', async () => {
    renderApp('/northwind-group/settings/yes');
    await screen.findByRole('radiogroup', { name: 'How strong a yes: Money' });
    expect(document.body.textContent).not.toMatch(/signed_in|device_key|\bchat level\b/);
  });

  it('reshapes the day when posts start to ask', async () => {
    renderToasts();
    const { user } = renderApp('/northwind-group/settings/yes');
    const posts = await screen.findByRole('radiogroup', { name: 'How strong a yes: Posts, emails and replies' });
    await user.click(within(posts).getByRole('radio', { name: 'Just a tap' }));
    // A stronger yes says so, with no Undo: putting it back would let more go on its own.
    expect(await screen.findByText('Posts, emails and replies: just a tap')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull();
    await waitFor(() => expect(screen.queryByText(/and 11 stopped for a person/)).not.toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: 'Back to the system default' }));
    expect(await screen.findByText(/and 11 stopped for a person/)).toBeInTheDocument();
  });

  it('offers Undo when a change lets more go on its own, and Undo asks more again', async () => {
    renderToasts();
    const { user } = renderApp('/northwind-group/settings/yes');
    const posts = await screen.findByRole('radiogroup', { name: 'How strong a yes: Posts, emails and replies' });
    await user.click(within(posts).getByRole('radio', { name: 'Just a tap' }));
    await screen.findByText('Posts, emails and replies: just a tap');
    await user.click(within(posts).getByRole('radio', { name: 'On its own' }));
    const said = await screen.findByText('Posts, emails and replies: on its own');
    await user.click(within(said.closest('[data-change-toast]') as HTMLElement).getByRole('button', { name: 'Undo' }));
    expect(await screen.findByText('Posts, emails and replies: just a tap again')).toBeInTheDocument();
    await waitFor(() => expect(within(posts).getByRole('radio', { name: 'Just a tap' })).toBeChecked());
  });

  it('lifts the floor for a business, so it can only ask for more', async () => {
    renderApp('/northwind-group/settings/yes');
    const money = await screen.findByRole('radiogroup', { name: 'How strong a yes: Money, Meridian Travel' });
    expect(within(money).getAllByRole('radio').map((stop) => stop.getAttribute('aria-label'))).toEqual(["Prove it's you"]);
  });

  it('lets chat answers be turned off', async () => {
    const { user } = renderApp('/northwind-group/settings/yes');
    await user.click(await screen.findByRole('switch', { name: 'Let people answer in chat' }));
    expect(await screen.findByText(/A yes in a chat counts for nothing now/)).toBeInTheDocument();
  });
});

describe('where you can say yes', () => {
  it('shows what would do for a payment, and what is coming has no button', async () => {
    renderApp('/northwind-group/settings/say-yes');
    expect(await screen.findByText(/A payment takes proving it's you\. You have 2 places/)).toBeInTheDocument();
    const later = screen.getByRole('region', { name: 'Coming later' });
    expect(within(later).getByText('iPhone')).toBeInTheDocument();
    expect(within(later).queryByRole('button')).not.toBeInTheDocument();
  });

  it('answers for another job', async () => {
    const { user } = renderApp('/northwind-group/settings/say-yes');
    await user.click(await screen.findByRole('radio', { name: /For example, a post/ }));
    expect(await screen.findByText(/A post takes just a tap\. You have 4 places/)).toBeInTheDocument();
  });
});

describe('a yes that takes more than a tap', () => {
  const org = () => db.orgs.get('northwind-group')!;

  it('says so before the press, and refuses a yes with no proof', async () => {
    renderApp('/northwind-group/needs-you');
    expect(await screen.findByText("Prove it's you to say yes")).toBeInTheDocument();
    const payment = org().approvals.find((item) => item.effect === 'pay')!;
    const refused = await fetch(`/api/orgs/northwind-group/approvals/${payment.id}/approve`, { method: 'POST', body: JSON.stringify({ content_hash: payment.content_hash }) });
    expect(refused.status).toBe(403);
    expect(await refused.json()).toMatchObject({ error: { code: 'proof_too_weak' }, requires: 'present' });
    expect(org().approvals.some((item) => item.id === payment.id)).toBe(true);
  });

  it('decides only after the proof, and a yes is on the record as proved', async () => {
    asComputer();
    const { user } = renderApp('/northwind-group/needs-you');
    const payment = org().approvals.find((item) => item.effect === 'pay')!;
    await user.click(await screen.findByRole('button', { name: 'Pay $40' }));
    const dialog = within(await screen.findByRole('dialog', { name: "Prove it's you" }));
    await waitFor(() => expect(dialog.getByRole('button', { name: 'Use your passkey' })).toBeEnabled());
    expect(org().approvals.some((item) => item.id === payment.id)).toBe(true);
    await user.click(dialog.getByRole('button', { name: 'Use your passkey' }));
    await waitFor(() => expect(org().history.find((item) => item.id === payment.id)).toMatchObject({ decided_method: 'passkey', decided_level: 'present' }), { timeout: 3_000 });
  }, 10_000);
});
