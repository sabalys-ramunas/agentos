import { screen, within } from '@testing-library/react';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { decidedByMe, deciderKey, deciderName } from './outcome';

describe("whose decision it is", () => {
  it('is the signed-in person only, by their id or their name', () => {
    const owner = { id: 'owner', name: 'Demo Owner' };
    expect(decidedByMe('owner', owner)).toBe(true);
    expect(decidedByMe('Demo Owner', owner)).toBe(true);
    expect(decidedByMe('Mara Lindqvist', owner)).toBe(false);
    expect(decidedByMe('m_k3j2', owner)).toBe(false);
    expect(decidedByMe(null, owner)).toBe(false);
    expect(decidedByMe('m_k3j2', { id: 'm_k3j2', name: 'Ana' })).toBe(true);
    expect(decidedByMe('owner', { id: 'm_k3j2', name: 'Ana' })).toBe(false);
  });

  it('names a teammate, and never shows an id a person cannot read', () => {
    expect(deciderName('Mara Lindqvist')).toBe('Mara Lindqvist');
    expect(deciderName('owner')).toBe('The owner');
    expect(deciderName('m_k3j2')).toBe('Someone on your team');
    expect(deciderName('agent:claude-code')).toBe('Someone on your team');
    expect(deciderName(null)).toBe('Someone on your team');
  });

  it('reads the owner\'s own yes as the owner\'s, whatever the node wrote in front of the id', () => {
    // The node writes "human:owner" for a yes from a routine or its CLI: the kind is not a name
    const owner = { id: 'owner', name: 'Demo Owner' };
    expect(decidedByMe('human:owner', owner)).toBe(true);
    expect(deciderName('human:owner')).toBe('The owner');
    expect(decidedByMe('human:m_k3j2', { id: 'm_k3j2', name: 'Ana' })).toBe(true);
    expect(decidedByMe('human:m_k3j2', owner)).toBe(false);
    expect(deciderName('human:m_k3j2', new Map([['m_k3j2', 'Ana Kowalski']]))).toBe('Ana Kowalski');
    expect(deciderName('human:m_unknown')).toBe('Someone on your team');
  });

  it('matches a member\'s own yes by their id, since the node writes it under their handle', () => {
    const ana = { id: 'm_k3j2', name: 'Ana Kowalski' };
    expect(decidedByMe(deciderKey({ decided_by: 'ana', decided_by_id: 'm_k3j2' }), ana)).toBe(true);
    expect(decidedByMe(deciderKey({ decided_by: 'ana', decided_by_id: 'm_other' }), ana)).toBe(false);
    expect(decidedByMe(deciderKey({ decided_by: 'human:owner' }), { id: 'owner', name: 'Demo Owner' })).toBe(true);
  });

  it('takes the node\'s own name for a member who decided, before any lookup', () => {
    // A member decides under their handle, which the members list may not show a viewer
    expect(deciderName('ada', undefined, 'Ada Lind')).toBe('Ada Lind');
    expect(deciderName('ada', new Map([['ada', 'Someone Else']]), 'Ada Lind')).toBe('Ada Lind');
  });

  it('shows a teammate\'s decision as "Decided today" with their name, never as "You decided"', async () => {
    const data = db.orgs.get('northwind-group')!;
    const item = data.history.find((row) => !row.outcome)!;
    Object.assign(item, { decided_by: 'Mara Lindqvist', decided_at: new Date().toISOString(), state: 'approved' });
    renderApp('/northwind-group/needs-you');
    const region = await screen.findByRole('region', { name: 'Decided today' });
    expect(within(region).getAllByText(/Mara Lindqvist/).length).toBeGreaterThan(0);
    expect(screen.queryByRole('region', { name: 'You decided today' })).not.toBeInTheDocument();
  });
});
