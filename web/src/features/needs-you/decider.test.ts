import { deciderName } from './outcome';

describe('who decided, as a person reads it', () => {
  it('names a member from the members list when the viewer can see it', () => {
    const people = new Map([['m_k3j2', 'Ana Kowalski']]);
    expect(deciderName('m_k3j2', people)).toBe('Ana Kowalski');
  });

  it('says someone on the team for an id the viewer cannot see, and the owner as the owner', () => {
    expect(deciderName('m_unknown', new Map())).toBe('Someone on your team');
    expect(deciderName('usr_unknown')).toBe('Someone on your team');
    expect(deciderName('owner', new Map([['owner', 'Demo Owner']]))).toBe('The owner');
  });
});
