import type { Lane } from '@/api/types';
import { accountCondition, problemSentence } from './words';

const held = (byId: string | null): Lane =>
  ({
    id: 'aster/social',
    workspace: 'aster',
    name: 'social',
    kind: 'browser',
    state: 'active',
    signal: 'ok',
    control: { state: 'human', by: 'owner', by_id: byId, since: null, takeback_requested: false, takeback_reason: null, takeback_by: null, last_human_input: null },
    account: null,
    health: null,
    site: { key: 'instagram', label: 'Instagram', domain: 'instagram.com' },
  }) as unknown as Lane;

describe('a lane someone holds, in the signed-in person\'s words', () => {
  it('says "You have control" when the signed-in person holds it', () => {
    const me = { id: 'usr_owner', name: 'Demo Owner' };
    expect(accountCondition(held('usr_owner'), me).sentence).toBe('You have control');
  });

  it('is not a problem for the owner who holds it, so no sentence says "owner has control" for it', () => {
    const me = { id: 'usr_owner', name: 'Demo Owner' };
    expect(problemSentence(held('usr_owner'), me)).toBeNull();
    expect(accountCondition(held('usr_owner'), me).sentence).not.toMatch(/^owner/i);
  });

  it('names another person who holds it by their name', () => {
    const me = { id: 'usr_owner', name: 'Demo Owner' };
    expect(accountCondition(held('usr_ana'), me).sentence).not.toBe('You have control');
  });
});
