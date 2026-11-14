import { screen, waitFor, within } from '@testing-library/react';
import { act } from 'react';
import { applyLiveEvent } from '@/api/live';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { ARM_MS, DONE_MS } from './decision-flow';
import { asComputer } from '@/test/computer';

const org = () => db.orgs.get('northwind-group')!;
const FIRST = "Post this photo on Aster Coffee's Instagram?";

async function open() {
  const view = renderApp('/northwind-group/approve');
  await screen.findByRole('heading', { level: 2, name: FIRST });
  return view;
}

describe('one by one (the phone view)', () => {
  it('shows one decision at a time, as a plain question with the post', async () => {
    await open();
    expect(await screen.findByText('23 to decide')).toBeInTheDocument();
    expect(screen.getByRole('article', { name: 'Instagram post preview' })).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { level: 2 })).toHaveLength(1);
  });

  it("puts Don't post on the left and Post it on the right", async () => {
    await open();
    const buttons = screen.getAllByRole('button', { name: /^(Post it|Don't post)$/ }).map((item) => item.textContent);
    expect(buttons).toEqual(["Don't post", 'Post it']);
  });

  it('does not swap the card when a new approval arrives', async () => {
    const { client, user } = await open();
    const reading = org().approvals[0]!;
    const arriving = { ...reading, id: 'ap_NEW', effect: 'pay' as const, summary: 'Pay $900 to a new vendor' };
    act(() => applyLiveEvent(client, 'northwind-group', { type: 'approval', payload: arriving }));
    expect(await screen.findByText('24 to decide')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /\$900/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Post it' }));
    await waitFor(() => expect(org().approvals.some((item) => item.id === reading.id)).toBe(false));
  });

  it('takes a double tap as one decision, and the next card waits before it can be decided', async () => {
    const { user } = await open();
    const before = org().approvals.length;
    await user.dblClick(screen.getByRole('button', { name: 'Post it' }));
    // Where the buttons were: the done state, with nothing to tap.
    expect(screen.getByText("Posting to Aster Coffee's Instagram", { selector: '[data-done]' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Post it' })).not.toBeInTheDocument();
    await screen.findByRole('heading', { level: 2, name: "Post this on Aster Coffee's LinkedIn?" }, { timeout: DONE_MS + 500 });
    const next = screen.getByRole('button', { name: 'Post it' });
    expect(next).toBeDisabled();
    await user.click(next);
    await waitFor(() => expect(next).toBeEnabled(), { timeout: ARM_MS + 500 });
    expect(org().approvals).toHaveLength(before - 1);
  });

  it('asks again, and waits before arming, for a payment', async () => {
    asComputer();
    const payment = org().approvals.find((item) => item.effect === 'pay')!;
    org().approvals = [payment];
    const { user } = renderApp('/northwind-group/approve');
    await screen.findByRole('heading', { level: 2, name: /^Pay \$40/ });
    await user.click(screen.getByRole('button', { name: 'Pay $40' }));
    const dialog = within(await screen.findByRole('dialog'));
    expect(dialog.getByText(/Money can't be taken back from here/)).toBeInTheDocument();
    const confirm = dialog.getByRole('button', { name: 'Use your passkey' });
    expect(confirm).toBeDisabled();
    expect(org().approvals).toHaveLength(1);
    await waitFor(() => expect(confirm).toBeEnabled(), { timeout: 1_500 });
    await user.click(confirm);
    await waitFor(() => expect(org().approvals).toHaveLength(0));
  });

  it('decides a light one in one tap, and says it is posting with Undo', async () => {
    const before = org().approvals.length;
    const { user } = await open();
    await user.click(screen.getByRole('button', { name: 'Post it' }));
    await waitFor(() => expect(org().approvals).toHaveLength(before - 1));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(await screen.findByText(/^Posting to Aster Coffee's Instagram in \d s$/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument();
  });

  it('shows questions once the approvals are done', async () => {
    org().approvals = [];
    const { user } = renderApp('/northwind-group/approve');
    await user.click(await screen.findByRole('button', { name: 'The kraft bag on the counter' }));
    await waitFor(() => expect(org().questions).toHaveLength(2));
  });

  it('says All clear when the queue is clear', async () => {
    renderApp('/juniper-labs/approve');
    expect(await screen.findByRole('heading', { name: 'All clear' })).toBeInTheDocument();
  });
});
