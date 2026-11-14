import { screen, waitFor, within } from '@testing-library/react';
import { orgs } from '@/api/client';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import type { Role } from '@/api/types';

const ORG = 'northwind-group';
const PAGE = `/${ORG}/settings/members`;
const asRole = (role: Role) => {
  db.orgs.get(ORG)!.role = role;
};

/** Where a member is signed in is an admin's to see. An admin can end one device; nobody else sees any. */
describe('a member\'s signed-in devices', () => {
  it('lets an admin see each device a member is signed in on, and end one after a confirm', async () => {
    asRole('admin');
    const { user } = renderApp(PAGE);
    const someone = db.orgs.get(ORG)!.members.find((item) => item.user.id !== 'owner' && item.role !== 'owner')!;
    await user.click(await screen.findByRole('button', { name: `Actions for ${someone.user.name}` }));
    await user.click(await screen.findByRole('menuitem', { name: 'Signed-in devices' }));
    const dialog = await screen.findByRole('dialog', { name: `${someone.user.name}'s devices` });
    expect(await within(dialog).findByText('iPhone, Safari')).toBeInTheDocument();
    expect(within(dialog).getByText(/Laptop, Chrome/)).toBeInTheDocument();
    await user.click(within(dialog).getAllByRole('button', { name: 'End' })[0]!);
    const confirm = await screen.findByRole('dialog', { name: 'End this device?' });
    await user.click(within(confirm).getByRole('button', { name: 'End device' }));
    await waitFor(() => expect(within(dialog).queryByText('iPhone, Safari')).toBeNull());
    expect(within(dialog).getByText('Laptop, Chrome')).toBeInTheDocument();
  });

  it('is not offered to a member who is not an admin, and the node refuses it', async () => {
    asRole('approver');
    renderApp(PAGE);
    expect(await screen.findByRole('heading', { name: 'Members' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Actions for/ })).toBeNull();
    const someone = db.orgs.get(ORG)!.members.find((item) => item.user.id !== 'owner')!;
    await expect(orgs.member(ORG, someone.id)).rejects.toMatchObject({ status: 403 });
    await expect(orgs.endMemberDevice(ORG, someone.id, 'anything')).rejects.toMatchObject({ status: 403 });
  });

  it('is not offered for yourself, and a device that is not there cannot be ended', async () => {
    asRole('admin');
    renderApp(PAGE);
    const me = await screen.findByText(/\(you\)/);
    const row = me.closest('tr') as HTMLElement;
    expect(within(row).queryByRole('button', { name: /^Actions for/ })).toBeNull();
    await expect(orgs.endMemberDevice(ORG, 'owner', 'no-such-device')).rejects.toMatchObject({ status: 404 });
  });
});
