import { screen, waitFor, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { db, resetDb } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

const ORG = 'northwind-group';
const source = readFileSync(join(__dirname, 'workspaces.tsx'), 'utf8') + readFileSync(join(__dirname, 'on-computer.tsx'), 'utf8');

function openTab() {
  const tab = { location: { href: '' }, close: vi.fn() };
  return vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
}

function bodyOf(fetched: { mock: { calls: unknown[][] } }, url: RegExp): unknown {
  const call = fetched.mock.calls.find((item) => url.test(String(item[0])));
  return call ? JSON.parse(String((call[1] as RequestInit | undefined)?.body ?? 'null')) : undefined;
}

describe('moving a workspace in and out, on the computer', () => {
  beforeEach(() => resetDb());
  afterEach(() => vi.restoreAllMocks());

  it('exports a workspace through a page on the computer, with no passphrase in the request', async () => {
    const open = openTab();
    const fetched = vi.spyOn(globalThis, 'fetch');
    const { user } = renderApp(`/${ORG}/settings/workspaces`);
    const workspace = db.orgs.get(ORG)!.workspaces[0]!;
    await user.click(await screen.findByRole('button', { name: `Actions for ${workspace.name}` }));
    await user.click(await screen.findByRole('menuitem', { name: 'Export' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/passphrase you type on this computer/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Continue on this computer' }));
    await waitFor(() => expect(open).toHaveBeenCalled());
    expect(bodyOf(fetched, new RegExp(`/api/orgs/${ORG}/forms$`))).toEqual({ kind: 'workspace_export', fields: { workspace: workspace.id } });
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it('imports a workspace through a page on the computer', async () => {
    const open = openTab();
    const fetched = vi.spyOn(globalThis, 'fetch');
    const { user } = renderApp(`/${ORG}/settings/workspaces`);
    await user.click(await screen.findByRole('button', { name: 'Import a workspace' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Continue on this computer' }));
    await waitFor(() => expect(open).toHaveBeenCalled());
    expect(bodyOf(fetched, new RegExp(`/api/orgs/${ORG}/forms$`))).toEqual({ kind: 'workspace_import' });
  });

  it('takes a new name in the browser, and sends it to the page on the computer as a plain field', async () => {
    const open = openTab();
    const fetched = vi.spyOn(globalThis, 'fetch');
    const { user } = renderApp(`/${ORG}/settings/workspaces`);
    await user.click(await screen.findByRole('button', { name: 'Import a workspace' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByRole('textbox', { name: 'New name (optional)' }), 'harbor-2');
    await user.click(within(dialog).getByRole('button', { name: 'Continue on this computer' }));
    await waitFor(() => expect(open).toHaveBeenCalled());
    expect(bodyOf(fetched, new RegExp(`/api/orgs/${ORG}/forms$`))).toEqual({ kind: 'workspace_import', fields: { name: 'harbor-2' } });
  });

  it('refuses to move a workspace for an admin, who cannot reach the computer\'s passphrase page', async () => {
    db.orgs.get(ORG)!.role = 'admin';
    const { user } = renderApp(`/${ORG}/settings/workspaces`);
    const workspace = db.orgs.get(ORG)!.workspaces[0]!;
    await user.click(await screen.findByRole('button', { name: `Actions for ${workspace.name}` }));
    expect(await screen.findByRole('menuitem', { name: 'Delete' })).toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: 'Export' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Import a workspace' })).not.toBeInTheDocument();
  });

  it('has no field a passphrase could be typed into, on either page', () => {
    expect(source).not.toMatch(/type=["']password["']|<Input[^>]*type=["']password|<input(?![^>]*type="(?:file|checkbox|hidden)")/);
  });
});
