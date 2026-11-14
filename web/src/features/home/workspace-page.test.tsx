import { screen, within } from '@testing-library/react';
import { fleet } from '@/api/client';
import { ApiError } from '@/api/http';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import type { Role } from '@/api/types';

const ORG = 'northwind-group';
const asRole = (role: Role) => {
  db.orgs.get(ORG)!.role = role;
};

/** A business's accounts are for anyone who sees it. What is left to log in is the owner's alone. */
describe('one business', () => {
  it('lists its accounts, and for the owner what is still to log in with the place to do it', async () => {
    renderApp(`/${ORG}/workspaces/aster`);
    expect(await screen.findByRole('heading', { name: 'Aster Coffee' })).toBeInTheDocument();
    const accounts = screen.getByRole('heading', { name: 'Accounts' }).closest('section') as HTMLElement;
    expect(within(accounts).getAllByRole('link').length).toBeGreaterThan(0);
    const queue = await screen.findByRole('heading', { name: 'Still to log in' });
    const section = queue.closest('section') as HTMLElement;
    expect(await within(section).findAllByRole('link', { name: 'Open the account' })).not.toHaveLength(0);
  });

  it('keeps the queue from anyone who is not the owner, and the node refuses it the same', async () => {
    asRole('admin');
    renderApp(`/${ORG}/workspaces/aster`);
    expect(await screen.findByRole('heading', { name: 'Accounts' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Still to log in' })).toBeNull();
    await expect(fleet.workspaceQueue(ORG, 'aster')).rejects.toMatchObject({ status: 403 });
    await expect(fleet.workspace(ORG, 'aster')).resolves.toMatchObject({ id: 'aster' });
  });

  it('says plainly when there is no such business, and offers the list', async () => {
    renderApp(`/${ORG}/workspaces/no-such-business`);
    expect(await screen.findByRole('heading', { name: 'There is no business with that name here' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Back to Accounts' })).toBeInTheDocument();
  });

  it('reads a business that is not there as not found', async () => {
    await expect(fleet.workspace(ORG, 'no-such-business')).rejects.toBeInstanceOf(ApiError);
  });
});
