import { act, screen, waitFor, within } from '@testing-library/react';
import { fleet } from '@/api/client';
import { ApiError } from '@/api/http';
import { applyLiveEvent } from '@/api/live';
import type { FreezeRecord } from '@/api/types';
import { setPaletteOpen } from '@/components/shell/palette-store';
import { computerName } from '@/lib/computers';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

vi.mock('@/lib/webauthn', () => ({
  passkeysAvailable: () => true,
  getPasskey: vi.fn(() => Promise.resolve({ id: 'credential' })),
  createPasskey: vi.fn(() => Promise.resolve({ id: 'credential' })),
}));

const ORG = 'northwind-group';
const org = () => db.orgs.get(ORG)!;
const ACCOUNT = '/northwind-group/lanes/aster/instagram';
const COMPUTER = '/northwind-group/nodes/home-mini?tab=lanes';

/** A freeze already in force, as the node would hold it. */
function frozen(scope: FreezeRecord['scope'], target: string, extra: Partial<FreezeRecord> = {}): FreezeRecord {
  return { id: `fz_${scope}`, scope, target, reason: 'a login we did not expect', by: 'owner', at: new Date(Date.now() - 5 * 60_000).toISOString(), via: 'panel', durable: true, applied_at: new Date().toISOString(), revoked: [], ...extra };
}

describe('freezing an account', () => {
  it('says what a freeze does, takes an optional reason, and then shows the account as frozen', async () => {
    const { user } = renderApp(ACCOUNT);
    await user.click(await screen.findByRole('button', { name: 'Freeze' }));
    const dialog = await screen.findByRole('dialog', { name: "Freeze Aster Coffee's Instagram?" });
    expect(within(dialog).getByText('Every agent on this account stops at once.')).toBeInTheDocument();
    expect(within(dialog).getByText(/No agent can act on this account until you lift the freeze/)).toBeInTheDocument();
    expect(within(dialog).getByText(/The browser is kept as it is, so you can take over and look/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText('Why (optional)'), 'a login we did not expect');
    await user.click(within(dialog).getByRole('button', { name: 'Freeze now' }));

    await waitFor(() => expect(org().freeze).toHaveLength(1));
    expect(org().freeze[0]).toMatchObject({ scope: 'lane', target: 'aster/instagram', reason: 'a login we did not expect' });
    // a grant for the whole workspace is kept for its other accounts; the node refuses it on this one
    expect(org().grants.find((grant) => grant.subject === 'claude-code-aster')?.revoked).toBe(false);
    expect(await screen.findByText('Agents are stopped on this account.')).toBeInTheDocument();
    // Said once, in the header, where a person looks (the page does not repeat a state in a details card).
    expect(screen.getAllByText('Frozen')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'Freeze' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Unfreeze' })).toBeInTheDocument();
  });

  it('gives a reason of its own when none is typed, since the node needs one', async () => {
    const { user } = renderApp(ACCOUNT);
    await user.click(await screen.findByRole('button', { name: 'Freeze' }));
    await user.click(await screen.findByRole('button', { name: 'Freeze now' }));
    await waitFor(() => expect(org().freeze).toHaveLength(1));
    expect(org().freeze[0]?.reason).toBe('no reason given');
  });

  it('shows the owner a freeze made elsewhere, from the live stream, without a reload', async () => {
    const { client } = renderApp(ACCOUNT);
    await screen.findByRole('button', { name: 'Freeze' });
    org().freeze.push(frozen('lane', 'aster/instagram'));
    act(() => applyLiveEvent(client, ORG, { type: 'freeze', payload: { state: 'frozen', scope: 'lane', target: 'aster/instagram' } }));
    expect(await screen.findByText('Agents are stopped on this account.')).toBeInTheDocument();
  });
});

describe('lifting a freeze', () => {
  it('is the owner\'s, after a check, and says nothing comes back by itself', async () => {
    org().freeze.push(frozen('lane', 'aster/instagram'));
    const { user } = renderApp(ACCOUNT);
    await user.click(await screen.findByRole('button', { name: 'Unfreeze' }));
    const dialog = await screen.findByRole('dialog', { name: "Unfreeze Aster Coffee's Instagram?" });
    expect(within(dialog).getByText(/Nothing comes back by itself/)).toBeInTheDocument();
    expect(within(dialog).getByText(/grants stay revoked and paused schedules stay paused/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Unfreeze' }));

    // Asked first: the owner confirms it is them, and only then is the freeze lifted.
    expect(await screen.findByText('Confirm it is you')).toBeInTheDocument();
    expect(org().freeze).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: 'Use your passkey' }));
    await waitFor(() => expect(org().freeze).toHaveLength(0));
    expect(await screen.findByRole('button', { name: 'Freeze' })).toBeInTheDocument();
  });

  it('is not offered to an admin, who can see that the account is frozen', async () => {
    org().role = 'admin';
    org().freeze.push(frozen('lane', 'aster/instagram'));
    renderApp(ACCOUNT);
    expect(await screen.findByText('Agents are stopped on this account.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Unfreeze' })).not.toBeInTheDocument();
  });

  it('names the wider freeze that still covers an account, and lifts that one for the owner', async () => {
    org().freeze.push(frozen('node', 'home-mini', { reason: 'all of it' }));
    const { user } = renderApp(ACCOUNT);
    expect(await screen.findByText('Agents are stopped on this machine.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Freeze' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Unfreeze' }));
    expect(await screen.findByRole('dialog', { name: `Unfreeze ${computerName('home-mini')}?` })).toBeInTheDocument();
  });
});

describe('the computer', () => {
  it('is frozen as a whole from its page, and says so there', async () => {
    const { user } = renderApp(COMPUTER);
    await user.click(await screen.findByRole('button', { name: 'Freeze' }));
    const dialog = await screen.findByRole('dialog', { name: /^Freeze .+\?$/ });
    expect(within(dialog).getByText('Every agent on this machine stops at once.')).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Freeze now' }));
    await waitFor(() => expect(org().freeze.some((record) => record.scope === 'node')).toBe(true));
    expect(await screen.findByText('Agents are stopped on this machine.')).toBeInTheDocument();
    // The page says it once; the status pill in the top bar says it too, which is right.
    expect(within(screen.getByRole('main')).getByText('Frozen')).toBeInTheDocument();
  });

  it('is not offered to an admin, even though an admin may freeze one account', async () => {
    org().role = 'admin';
    renderApp(COMPUTER);
    await screen.findByRole('heading', { level: 1 });
    expect(await screen.findByRole('tab', { name: /Accounts/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Freeze' })).not.toBeInTheDocument();
  });
});

describe('the command palette', () => {
  it('offers the owner Freeze and Unfreeze for this machine', async () => {
    const { user } = renderApp('/northwind-group');
    act(() => setPaletteOpen(true));
    await user.click(await screen.findByRole('option', { name: /^Freeze / }));
    const dialog = await screen.findByRole('dialog', { name: /^Freeze .+\?$/ });
    await user.click(within(dialog).getByRole('button', { name: 'Freeze now' }));
    await waitFor(() => expect(org().freeze.some((record) => record.scope === 'node')).toBe(true));

    act(() => setPaletteOpen(true));
    expect(await screen.findByRole('option', { name: /^Unfreeze / })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /^Freeze / })).not.toBeInTheDocument();
  });

  it('does not offer a freeze to an admin', async () => {
    org().role = 'admin';
    renderApp('/northwind-group');
    act(() => setPaletteOpen(true));
    await screen.findByRole('option', { name: /Review what needs you/ });
    expect(screen.queryByRole('option', { name: /^Freeze / })).not.toBeInTheDocument();
  });
});

describe('a refusal', () => {
  it('is what the node says to anyone who may not freeze or lift one', async () => {
    org().role = 'admin';
    await expect(fleet.freezeNow(ORG, { scope: 'node', reason: 'not an admin thing' })).rejects.toMatchObject({ status: 403, code: 'denied' });
    org().freeze.push(frozen('lane', 'aster/instagram'));
    const grantsLive = org().grants.filter((grant) => !grant.revoked).length;
    const lift = fleet.unfreeze(ORG, { scope: 'lane', target: 'aster/instagram' });
    await expect(lift).rejects.toBeInstanceOf(ApiError);
    await expect(lift).rejects.toMatchObject({ status: 403 });
    // Nothing moved: the freeze holds, and no grant was touched.
    expect(org().freeze).toHaveLength(1);
    expect(org().grants.filter((grant) => !grant.revoked).length).toBe(grantsLive);
  });

  it('lets an admin freeze one account, which the node also allows', async () => {
    org().role = 'admin';
    const report = await fleet.freezeNow(ORG, { scope: 'lane', target: 'aster/instagram', reason: 'checking' });
    expect(report).toMatchObject({ scope: 'lane', target: 'aster/instagram', already_frozen: false });
  });
});
