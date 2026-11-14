import { screen, waitFor, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { db, resetDb } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { asComputer } from '@/test/computer';

const ORG = 'northwind-group';
const here = __dirname;
const screenSource = readFileSync(join(here, 'vault.tsx'), 'utf8');

/** The request bodies the app sent, as JSON. A test reads them to see what left the browser. */
function sentBodies(spy: { mock: { calls: unknown[][] } }, url: RegExp): string[] {
  return spy.mock.calls
    .filter((call) => url.test(String(call[0])))
    .map((call) => String((call[1] as RequestInit | undefined)?.body ?? ''));
}

function openTab() {
  const tab = { location: { href: '' }, close: vi.fn() };
  return vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
}

describe('the vault on a computer', () => {
  beforeEach(() => resetDb());
  afterEach(() => vi.restoreAllMocks());

  it('shows names, and says when the vault is locked', async () => {
    db.orgs.get(ORG)!.vaultState = 'sealed';
    renderApp(`/${ORG}/settings/vault`);
    expect(await screen.findByText(/Locked\. Saved logins cannot be used/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Unlock' })).toBeInTheDocument();
    expect(await screen.findByRole('table')).toBeInTheDocument();
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it('locks the vault from the page', async () => {
    const { user } = renderApp(`/${ORG}/settings/vault`);
    await user.click(await screen.findByRole('button', { name: 'Lock' }));
    expect(await screen.findByRole('button', { name: 'Unlock' })).toBeInTheDocument();
    expect(db.orgs.get(ORG)!.vaultState).toBe('sealed');
  });

  it('unlocks on the computer: the passphrase is never in the app\'s request', async () => {
    db.orgs.get(ORG)!.vaultState = 'sealed';
    const open = openTab();
    const fetched = vi.spyOn(globalThis, 'fetch');
    const { user } = renderApp(`/${ORG}/settings/vault`);
    await user.click(await screen.findByRole('button', { name: 'Unlock' }));
    await waitFor(() => expect(open).toHaveBeenCalled());
    const start = sentBodies(fetched, new RegExp(`/api/orgs/${ORG}/forms$`)).at(0) ?? '';
    expect(JSON.parse(start)).toEqual({ kind: 'vault_unlock' });
    expect(start).not.toMatch(/passphrase|password|secret/);
    await waitFor(() => expect(db.orgs.get(ORG)!.vaultState).toBe('unsealed'), { timeout: 8_000 });
    expect(await screen.findByText(/^Unlocked\./)).toBeInTheDocument();
  }, 15_000);

  it('adds a saved login through the computer, and sends no secret', async () => {
    const open = openTab();
    const fetched = vi.spyOn(globalThis, 'fetch');
    const { user } = renderApp(`/${ORG}/settings/vault`);
    await user.click(await screen.findByRole('button', { name: 'Add a saved login' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('Name'), 'Shop Instagram');
    await user.type(within(dialog).getByLabelText(/^Site/), 'instagram.com');
    await user.type(within(dialog).getByLabelText(/^Account/), 'northwind/shop');
    await user.click(within(dialog).getByRole('button', { name: 'Continue on this computer' }));
    await waitFor(() => expect(open).toHaveBeenCalled());
    const start = sentBodies(fetched, new RegExp(`/api/orgs/${ORG}/forms$`)).at(0) ?? '';
    const body = JSON.parse(start);
    expect(body).toEqual({
      kind: 'vault_entry',
      fields: { name: 'Shop Instagram', kind: 'password', domain: 'instagram.com', lanes: 'northwind/shop' },
    });
    expect(Object.keys(body.fields).sort()).toEqual(['domain', 'kind', 'lanes', 'name']);
    expect(Object.keys(body)).toEqual(['kind', 'fields']);
    await waitFor(() => expect(db.orgs.get(ORG)!.vault.some((entry) => entry.name === 'Shop Instagram')).toBe(true), { timeout: 8_000 });
  }, 15_000);

  it('keeps Add disabled until the account is an id the vault can bind', async () => {
    const { user } = renderApp(`/${ORG}/settings/vault`);
    await user.click(await screen.findByRole('button', { name: 'Add a saved login' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('Name'), 'Shop');
    await user.type(within(dialog).getByLabelText(/^Site/), 'shop.example.com');
    await user.type(within(dialog).getByLabelText(/^Account/), 'no slash here');
    expect(within(dialog).getByRole('button', { name: 'Continue on this computer' })).toBeDisabled();
  });

  it('removes a saved login only after its name is typed', async () => {
    asComputer();
    const { user } = renderApp(`/${ORG}/settings/vault`);
    const table = await screen.findByRole('table');
    const name = db.orgs.get(ORG)!.vault[0]?.name ?? '';
    await user.click(within(table).getByRole('button', { name: `Remove ${name}` }));
    const dialog = await screen.findByRole('dialog');
    const confirm = within(dialog).getByRole('button', { name: 'Remove the saved login' });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByRole('textbox'), name);
    await user.click(confirm);
    await waitFor(() => expect(db.orgs.get(ORG)!.vault.some((entry) => entry.name === name)).toBe(false));
  });

  it('shows names only to a viewer, with no control to change the vault', async () => {
    db.orgs.get(ORG)!.role = 'viewer';
    renderApp(`/${ORG}/settings/vault`);
    const table = await screen.findByRole('table');
    expect(within(table).getByText('aster-instagram')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Lock' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add a saved login' })).not.toBeInTheDocument();
    expect(within(table).queryByRole('button')).not.toBeInTheDocument();
  });

  it('lets an admin remove a name, and offers no lock, unlock or add, which the node answers only to the owner', async () => {
    asComputer();
    db.orgs.get(ORG)!.role = 'admin';
    const fetched = vi.spyOn(globalThis, 'fetch');
    renderApp(`/${ORG}/settings/vault`);
    const table = await screen.findByRole('table');
    const name = db.orgs.get(ORG)!.vault[0]!.name;
    expect(within(table).getByRole('button', { name: `Remove ${name}` })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Lock' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Unlock' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add a saved login' })).not.toBeInTheDocument();
    // The vault's state is the owner's to read, so an admin's screen never asks the node for it.
    expect(fetched.mock.calls.some((call) => /\/vault\/status$/.test(String(call[0])))).toBe(false);
  });

  it('asks the node for the vault state only for the owner', async () => {
    const fetched = vi.spyOn(globalThis, 'fetch');
    renderApp(`/${ORG}/settings/vault`);
    await screen.findByRole('table');
    await waitFor(() => expect(fetched.mock.calls.some((call) => /\/vault\/status$/.test(String(call[0])))).toBe(true));
  });

  it('never has a secret field, and the screen reads no secret', () => {
    expect(screenSource).not.toMatch(/type=["']password["']|<input|<Input[^>]*type=["']password/);
    expect(screenSource).not.toMatch(/\b(passphrase|password|secret)\??:\s*string/);
  });
});
