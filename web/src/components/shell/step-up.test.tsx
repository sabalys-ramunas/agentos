import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { fleet } from '@/api/client';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { server } from '@/test/setup';

vi.mock('@/lib/webauthn', () => ({
  passkeysAvailable: () => true,
  getPasskey: vi.fn(() => Promise.resolve({ id: 'credential' })),
  createPasskey: vi.fn(() => Promise.resolve({ id: 'credential' })),
}));

const org = () => db.orgs.get('northwind-group')!;

async function askToRemoveNode() {
  const view = renderApp('/northwind-group/nodes/home-mini?tab=lanes');
  const { user } = view;
  await user.click(await screen.findByRole('button', { name: 'Remove machine' }));
  await user.type(await screen.findByLabelText(/to confirm/), 'home-mini');
  const dialogs = screen.getAllByRole('button', { name: 'Remove machine' });
  await user.click(dialogs[dialogs.length - 1]!);
  return view;
}

/** A machine on its own offers no removal, so the same request is sent from behind the screen. */
async function askOnAComputer() {
  const view = renderApp('/northwind-group/nodes/home-mini?tab=lanes');
  await screen.findByRole('tab', { name: /Health/ });
  void fleet.removeNode('northwind-group', 'home-mini').catch(() => undefined);
  return view;
}

describe('step-up', () => {
  it('asks on 403 step_up_required and retries the request', async () => {
    const { user } = await askToRemoveNode();
    expect(await screen.findByText('Confirm it is you')).toBeInTheDocument();
    expect(org().nodes.some((node) => node.id === 'home-mini')).toBe(true);
    await user.click(screen.getByRole('button', { name: 'Use your passkey' }));
    await waitFor(() => expect(org().nodes.some((node) => node.id === 'home-mini')).toBe(false));
  });

  it('does nothing when the dialog is closed', async () => {
    const { user } = await askToRemoveNode();
    await screen.findByText('Confirm it is you');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByText('Confirm it is you')).not.toBeInTheDocument());
    expect(org().nodes.some((node) => node.id === 'home-mini')).toBe(true);
  });

  it('asks again after the screen that asked went away with the dialog open', async () => {
    const first = await askToRemoveNode();
    await screen.findByText('Confirm it is you');
    first.unmount();
    await askToRemoveNode();
    expect(await screen.findByText('Confirm it is you')).toBeInTheDocument();
  });

  it('offers no email link on a computer, only a passkey', async () => {
    server.use(http.get('/api/auth/providers', () => HttpResponse.json({ magic_link: false, google: false, passkey: true, code: true })));
    const { user } = await askOnAComputer();
    expect(await screen.findByText('Confirm it is you')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Email me a link' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Use your passkey' }));
    await waitFor(() => expect(org().nodes.some((node) => node.id === 'home-mini')).toBe(false));
  });

  it('says how to add a passkey when a computer has none to check with', async () => {
    server.use(http.get('/api/auth/providers', () => HttpResponse.json({ magic_link: false, google: false, passkey: true, code: true })));
    const had = db.user.has_passkey;
    db.user.has_passkey = false;
    try {
      await askOnAComputer();
      expect(await screen.findByText(/Confirming this takes a passkey/)).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Email me a link' })).not.toBeInTheDocument();
      expect(screen.getByRole('link', { name: 'Add a passkey' })).toHaveAttribute('href', '/northwind-group/settings/passkeys');
    } finally {
      db.user.has_passkey = had;
    }
  });

  it('does not ask again inside the step-up window', async () => {
    db.stepUpUntil = new Date(Date.now() + 60_000).toISOString();
    await askToRemoveNode();
    await waitFor(() => expect(org().nodes.some((node) => node.id === 'home-mini')).toBe(false));
    expect(screen.queryByText('Confirm it is you')).not.toBeInTheDocument();
  });
});
