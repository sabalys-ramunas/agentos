import { principalName } from './principal';

describe('naming an agent that acts on the owner\'s token', () => {
  it('is named as an agent, never as the owner', () => {
    expect(principalName({ kind: 'agent', id: 'owner' })).toBe('An agent');
    expect(principalName({ kind: 'agent', id: 'owner', thread: null })).not.toMatch(/owner/i);
  });

  it('reads in a sentence as "an agent", so "asks you" follows it', () => {
    expect(principalName({ kind: 'agent', id: 'owner' }, { inSentence: true })).toBe('an agent');
  });

  it('keeps the names of the agents we know', () => {
    expect(principalName({ kind: 'agent', id: 'claude-code' })).toBe('Claude Code');
  });
});

describe('the signed-in person in a sentence', () => {
  it('is "you" in the middle of a sentence and "You" on its own', () => {
    const me = { id: 'usr_owner', name: 'Demo Owner' };
    expect(principalName({ kind: 'human', id: 'usr_owner' }, { me, inSentence: true })).toBe('you');
    expect(principalName({ kind: 'human', id: 'usr_owner' }, { me })).toBe('You');
  });
});
