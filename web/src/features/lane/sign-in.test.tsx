import { screen, waitFor, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { db, resetDb } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

const ORG = 'northwind-group';
const source = readFileSync(join(__dirname, 'sign-in.tsx'), 'utf8');

/** The first account the login queue shows: an unready browser, the ones that need attention first. */
function firstQueued(): string {
  const rows = db.orgs.get(ORG)!.lanes.filter((lane) => lane.kind === 'browser' && lane.readiness !== 'ready');
  const rank = (lane: { readiness?: string }) => (lane.readiness === 'needs_attention' ? 0 : 1);
  const first = [...rows].sort((a, b) => rank(a) - rank(b))[0]!;
  return `${first.workspace}/${first.name}`;
}

function savedFor(lane: string) {
  db.orgs.get(ORG)!.vault.push({
    id: 'v_shop', name: 'shop-login', kind: 'password', domain: 'instagram.com', lanes: [lane], created_at: '2026-10-01T00:00:00Z', last_used: null,
  });
}

function bodyOf(fetched: { mock: { calls: unknown[][] } }, url: RegExp): unknown {
  const write = fetched.mock.calls.filter((item) => url.test(String(item[0])) && (item[1] as RequestInit | undefined)?.method === 'POST').at(-1);
  return write ? JSON.parse(String((write[1] as RequestInit).body ?? 'null')) : undefined;
}

describe('signing an account in from the login queue', () => {
  beforeEach(() => resetDb());
  afterEach(() => vi.restoreAllMocks());

  it('opens the computer\'s form for an account with no saved login, and sends no password', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue({ location: { href: '' }, close: vi.fn() } as unknown as Window);
    const fetched = vi.spyOn(globalThis, 'fetch');
    const { user } = renderApp(`/${ORG}/logins`);
    const region = await screen.findByRole('region', { name: 'Sign in' });
    expect(within(region).getByText(/Not signed in yet/)).toBeInTheDocument();
    await user.click(within(region).getByRole('button', { name: 'Sign in' }));
    await waitFor(() => expect(open).toHaveBeenCalled());
    expect(bodyOf(fetched, new RegExp(`/lanes/.*/login/form$`))).toEqual({});
    expect(await within(region).findByRole('link', { name: /Open the page on this computer/ })).toBeInTheDocument();
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it('signs in from the vault with no form, when the password is saved for this account', async () => {
    const lane = firstQueued();
    savedFor(lane);
    const fetched = vi.spyOn(globalThis, 'fetch');
    const { user } = renderApp(`/${ORG}/logins`);
    const region = await screen.findByRole('region', { name: 'Sign in' });
    expect(await within(region).findByText(/Saved in the vault. agentos signs in from it and asks nobody/)).toBeInTheDocument();
    await user.click(within(region).getByRole('button', { name: 'Sign in from the vault' }));
    expect(await within(region).findByText('Signed in. The login is saved in the vault.')).toBeInTheDocument();
    expect(bodyOf(fetched, /\/login\/form$/)).toBeUndefined();
    expect(bodyOf(fetched, /\/login\/vault$/)).toEqual({});
  });

  it('asks to unlock the vault, rather than a form, when the vault is locked', async () => {
    savedFor(firstQueued());
    db.orgs.get(ORG)!.vaultState = 'sealed';
    renderApp(`/${ORG}/logins`);
    const region = await screen.findByRole('region', { name: 'Sign in' });
    expect(await within(region).findByRole('link', { name: 'Unlock it in Settings' })).toBeInTheDocument();
    expect(within(region).queryByRole('button', { name: 'Sign in from the vault' })).not.toBeInTheDocument();
    expect(within(region).queryByRole('button', { name: 'Sign in' })).not.toBeInTheDocument();
  });

  it('shows the state to someone who cannot operate the lane, with no control', async () => {
    db.orgs.get(ORG)!.role = 'viewer';
    renderApp(`/${ORG}/logins`);
    const region = await screen.findByRole('region', { name: 'Sign in' });
    expect(within(region).getByText(/Not signed in yet/)).toBeInTheDocument();
    expect(within(region).queryByRole('button')).not.toBeInTheDocument();
  });

  it('offers a saved login to an admin, and tells a member who may not use it that an owner or admin signs in from it', async () => {
    savedFor(firstQueued());
    db.orgs.get(ORG)!.role = 'member';
    const first = renderApp(`/${ORG}/logins`);
    const region = await screen.findByRole('region', { name: 'Sign in' });
    expect(await within(region).findByText('Saved in the vault. An owner or admin signs in from it, with no form.')).toBeInTheDocument();
    expect(within(region).queryByRole('button', { name: 'Sign in from the vault' })).not.toBeInTheDocument();
    expect(within(region).queryByRole('button', { name: 'Sign in' })).not.toBeInTheDocument();
    first.unmount();
    db.orgs.get(ORG)!.role = 'admin';
    renderApp(`/${ORG}/logins`);
    const again = await screen.findByRole('region', { name: 'Sign in' });
    expect(await within(again).findByRole('button', { name: 'Sign in from the vault' })).toBeInTheDocument();
  });

  it('has no password field, and types no password in the app', () => {
    expect(source).not.toMatch(/type=["']password["']|<input(?![^>]*type="checkbox")|<Input/);
    expect(source).not.toMatch(/password\??:\s*string/);
  });
});
