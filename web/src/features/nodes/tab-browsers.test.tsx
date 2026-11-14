import { screen, waitFor, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { db, resetDb } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

const ORG = 'northwind-group';
const PATH = `/${ORG}/nodes/hetzner-1?tab=browsers`;
const source = readFileSync(join(__dirname, 'tab-browsers.tsx'), 'utf8');

/** The body of the last write to a URL. Reads are ignored: a list refetches after each change. */
function bodyOf(fetched: { mock: { calls: unknown[][] } }, url: RegExp): Record<string, unknown> | undefined {
  const write = fetched.mock.calls.filter((item) => url.test(String(item[0])) && (item[1] as RequestInit | undefined)?.method === 'POST').at(-1);
  return write ? (JSON.parse(String((write[1] as RequestInit).body ?? 'null')) as Record<string, unknown>) : undefined;
}

describe('browsers and connections on a machine', () => {
  beforeEach(() => resetDb());
  afterEach(() => vi.restoreAllMocks());

  it('lists browsers with their accounts, and keeps an address behind Technical details', async () => {
    renderApp(PATH);
    expect(await screen.findByText('aster-desk')).toBeInTheDocument();
    expect(screen.getByText(/Connection de1 · 1 account/)).toBeInTheDocument();
    expect(screen.getByText('aster/instagram')).toBeInTheDocument();
    expect(screen.getByText('Set up')).toBeInTheDocument();
    expect(screen.getByText('Not used yet')).toBeInTheDocument();
    expect(screen.queryByText('198.51.100.21:8080')).not.toBeInTheDocument();
  });

  it('adds a connection with no password straight away, and sends no password', async () => {
    const fetched = vi.spyOn(globalThis, 'fetch');
    const { user } = renderApp(PATH);
    await user.click(await screen.findByRole('button', { name: 'Add a connection' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('Name'), 'lt2');
    await user.type(within(dialog).getByLabelText('Address'), '203.0.113.7');
    await user.click(within(dialog).getByRole('button', { name: 'Add the connection' }));
    await waitFor(() => expect(db.orgs.get(ORG)!.proxies.some((proxy) => proxy.label === 'lt2')).toBe(true));
    const body = bodyOf(fetched, new RegExp(`/api/orgs/${ORG}/proxies$`))!;
    expect(body).toMatchObject({ label: 'lt2', host: '203.0.113.7', port: 8080 });
    expect(Object.keys(body)).not.toContain('password');
  }, 15_000);

  it('sends a connection that needs a password to the machine, with no password in the app', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue({ location: { href: '' }, close: vi.fn() } as unknown as Window);
    const fetched = vi.spyOn(globalThis, 'fetch');
    const { user } = renderApp(PATH);
    await user.click(await screen.findByRole('button', { name: 'Add a connection' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('Name'), 'de9');
    await user.type(within(dialog).getByLabelText('Address'), '203.0.113.9');
    await user.click(within(dialog).getByRole('button', { name: 'It needs a password' }));
    await user.click(await screen.findByRole('button', { name: 'Continue on this machine' }));
    await waitFor(() => expect(open).toHaveBeenCalled());
    const body = bodyOf(fetched, new RegExp(`/api/orgs/${ORG}/forms$`))!;
    expect(body).toEqual({ kind: 'proxy', fields: { label: 'de9', host: '203.0.113.9', port: '8080', username: '' } });
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it('keeps "It needs a password" off until the connection has a name and an address, so no form opens that would refuse', async () => {
    const open = vi.spyOn(window, 'open');
    const { user } = renderApp(PATH);
    await user.click(await screen.findByRole('button', { name: 'Add a connection' }));
    const dialog = await screen.findByRole('dialog');
    const computer = within(dialog).getByRole('button', { name: 'It needs a password' });
    expect(computer).toBeDisabled();
    await user.type(within(dialog).getByLabelText('Name'), 'de9');
    expect(computer).toBeDisabled();
    await user.type(within(dialog).getByLabelText('Address'), '203.0.113.9');
    expect(computer).toBeEnabled();
    expect(open).not.toHaveBeenCalled();
  });

  it('adds a browser that uses a connection, and checks its name first', async () => {
    const fetched = vi.spyOn(globalThis, 'fetch');
    const { user } = renderApp(PATH);
    const addBrowser = await screen.findByRole('button', { name: 'Add a browser' });
    await waitFor(() => expect(addBrowser).toBeEnabled());
    await user.click(addBrowser);
    const dialog = await screen.findByRole('dialog');
    const add = within(dialog).getByRole('button', { name: 'Add the browser' });
    await user.type(within(dialog).getByLabelText(/^Name/), 'Not Valid');
    expect(add).toBeDisabled();
    await user.clear(within(dialog).getByLabelText(/^Name/));
    await user.type(within(dialog).getByLabelText(/^Name/), 'shop-two');
    expect(add).toBeDisabled();
    expect(bodyOf(fetched, /browser-devices$/)).toBeUndefined();
  }, 15_000);

  it('shows what moving an account would change before anything moves', async () => {
    const fetched = vi.spyOn(globalThis, 'fetch');
    const { user } = renderApp(PATH);
    await user.click(await screen.findByRole('button', { name: 'Move aster/instagram' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('aster/instagram moves to aster-desk.')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Move the account' })).toBeDisabled();
    expect(bodyOf(fetched, /\/device$/)).toBeUndefined();
  }, 15_000);

  it('shows browsers to an admin, who cannot change them', async () => {
    db.orgs.get(ORG)!.role = 'admin';
    renderApp(PATH);
    expect(await screen.findByText('aster-desk')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add a browser' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add a connection' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Move aster/instagram' })).not.toBeInTheDocument();
  });

  it('has no field a password could be typed into', () => {
    expect(source).not.toMatch(/type=["']password["']|<input(?![^>]*type="(?:checkbox)")/);
  });
});
