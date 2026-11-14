import { screen, waitFor } from '@testing-library/react';
import type { Member } from '@/api/types';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

const ORG = 'northwind-group';
const PAGE = `/${ORG}/settings/access`;

/** Tomas is an approver in Aster Coffee and a viewer in Harbor Supply: his roles differ by business. */
function mixed(): Member {
  const member = db.orgs.get(ORG)!.members.find((item) => item.user.name === 'Tomas Reyes')!;
  Object.assign(member, {
    role: 'approver',
    workspaces: ['aster', 'harbor'],
    roles_by_workspace: [
      { workspace: 'aster', role: 'approver' },
      { workspace: 'harbor', role: 'viewer' },
    ],
  });
  return member;
}

describe('a person whose role differs by business', () => {
  it('says so, and gives each business its own role', async () => {
    const member = mixed();
    renderApp(PAGE);
    expect((await screen.findAllByText('Differs by workspace')).length).toBeGreaterThan(0);
    expect(screen.queryByRole('combobox', { name: `Role for ${member.user.name}` })).not.toBeInTheDocument();
    const harbor = await screen.findByRole('combobox', { name: `Role in Harbor Supply for ${member.user.name}` });
    expect(harbor).toHaveTextContent('Viewer');
    expect(screen.getByRole('combobox', { name: `Role in Aster Coffee for ${member.user.name}` })).toHaveTextContent('Approver');
  });

  it('changes one business, and keeps the role it has in the others', async () => {
    const member = mixed();
    const { user } = renderApp(PAGE);
    await user.click(await screen.findByRole('combobox', { name: `Role in Harbor Supply for ${member.user.name}` }));
    await user.click(await screen.findByRole('option', { name: 'Approver' }));
    await waitFor(() => expect(member.roles_by_workspace).toEqual([
      { workspace: 'aster', role: 'approver' },
      { workspace: 'harbor', role: 'approver' },
    ]));
    expect(member.role).toBe('approver');
  });
});

describe('an auditor', () => {
  it('keeps the auditor label on the members list', async () => {
    const member = db.orgs.get(ORG)!.members.find((item) => item.user.name === 'Ines Okafor')!;
    Object.assign(member, { role: 'auditor', workspaces: ['meridian'], roles_by_workspace: [{ workspace: 'meridian', role: 'auditor' }] });
    renderApp('/northwind-group/settings/members');
    // The list shows the role on a phone and on a desk, so there are two of them.
    const [label] = await screen.findAllByRole('combobox', { name: `Role for ${member.user.name}` });
    expect(label).toHaveTextContent('Auditor');
  });
});
