import { act, screen, waitFor, within } from '@testing-library/react';
import { fleet } from '@/api/client';
import { applyLiveEvent } from '@/api/live';
import type { CodeRequest } from '@/api/types';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

const PATH = '/northwind-group/needs-you';
const ORG = 'northwind-group';
const org = () => db.orgs.get(ORG)!;
/** A code the owner types, distinct enough to find anywhere it might leak. */
const CODE = 'Q7X9K2';

const cardButton = () => screen.findByRole('button', { name: /^Login code for Instagram/ });
const panel = () => within(screen.getByRole('region', { name: 'Selected item' }));

async function ready() {
  const view = renderApp(PATH);
  await screen.findByRole('heading', { name: 'Needs you', level: 1 });
  await screen.findByRole('region', { name: 'Login codes' });
  return view;
}

async function openCard(user: ReturnType<typeof renderApp>['user']) {
  await user.click(await cardButton());
  await screen.findByRole('heading', { level: 2, name: 'Type the code Instagram sent you' });
}

describe('login codes', () => {
  it('sit in Needs you with the same weight as a question, and say whose login it is', async () => {
    const { user } = await ready();
    const region = within(screen.getByRole('region', { name: 'Login codes' }));
    expect(region.getByText('A site asked for a code. Only you can type it.')).toBeInTheDocument();
    expect(await cardButton()).toHaveTextContent('Aster Coffee');
    await openCard(user);
    expect(panel().getByText(/is signing in to Instagram/)).toBeInTheDocument();
    expect(panel().getByText(/For Aster Coffee\. The login waits until/)).toBeInTheDocument();
  });

  it('asks for the code in a plain field, never a password one, and never sends it anywhere but the node', async () => {
    const { user } = await ready();
    await openCard(user);
    const field = screen.getByLabelText('Code');
    expect(field).toHaveAttribute('type', 'text');
    expect(field).toHaveAttribute('autocomplete', 'one-time-code');
    expect(screen.getByText(/the code is never stored or shown to the agent/)).toBeInTheDocument();
  });

  it('is typed once: the card leaves, the node holds it as answered, and no cache keeps the code', async () => {
    const { user, client } = await ready();
    await openCard(user);
    await user.type(screen.getByLabelText('Code'), CODE);
    await user.click(screen.getByRole('button', { name: 'Send code' }));

    await waitFor(() => expect(org().codes.filter((card) => card.state === 'open')).toHaveLength(0));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Login codes' })).not.toBeInTheDocument());
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain(CODE);
    expect(JSON.stringify(client.getMutationCache().getAll().map((mutation) => mutation.state))).not.toContain(CODE);
  });

  it('says a bad code in plain words, and keeps the card so it can be typed again', async () => {
    const { user } = await ready();
    await openCard(user);
    await user.type(screen.getByLabelText('Code'), '12');
    await user.click(screen.getByRole('button', { name: 'Send code' }));
    expect(await screen.findByText('A code is 4 to 12 letters or digits.')).toBeInTheDocument();
    expect(screen.getByLabelText('Code')).toHaveValue('');
    expect(org().codes.filter((card) => card.state === 'open')).toHaveLength(1);
  });

  it('asks for the code before sending, and "No code came" stops the login instead', async () => {
    const { user } = await ready();
    await openCard(user);
    await user.click(screen.getByRole('button', { name: 'Send code' }));
    expect(await screen.findByText('Type the code the site sent you.')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'No code came' }));
    await waitFor(() => expect(org().codes.filter((card) => card.state === 'open')).toHaveLength(0));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Login codes' })).not.toBeInTheDocument());
  });

  it('arrives and leaves on the live stream, and counts in the rail the same way', async () => {
    const { client } = await ready();
    const card: CodeRequest = {
      id: 'cr_live_1',
      state: 'open',
      lane: 'harbor/reddit',
      site: 'reddit.com',
      requested_by: { kind: 'agent', id: 'claude-code' },
      created_at: new Date().toISOString(),
      expires_at: new Date(Date.now() + 8 * 60_000).toISOString(),
    };
    act(() => applyLiveEvent(client, ORG, { type: 'code', payload: card }));
    expect(await screen.findByRole('button', { name: /^Login code for Reddit/ })).toBeInTheDocument();

    act(() => applyLiveEvent(client, ORG, { type: 'code', payload: { ...card, state: 'used' } }));
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Login code for Reddit/ })).not.toBeInTheDocument());
  });

  it('is never shown to a member or an admin, and a refusal says so', async () => {
    org().role = 'admin';
    renderApp(PATH);
    await screen.findByRole('heading', { name: 'Needs you', level: 1 });
    await screen.findByRole('region', { name: 'Posts to check' });
    expect(screen.queryByRole('region', { name: 'Login codes' })).not.toBeInTheDocument();
    await expect(fleet.codes(ORG)).rejects.toMatchObject({ status: 403 });
    await expect(fleet.enterCode(ORG, org().codes[0]?.id ?? 'cr_x', CODE)).rejects.toMatchObject({ status: 403 });
    await expect(fleet.declineCode(ORG, org().codes[0]?.id ?? 'cr_x')).rejects.toMatchObject({ status: 403 });
    expect(org().codes).toHaveLength(1);
  });
});
