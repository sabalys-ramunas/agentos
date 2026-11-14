import type { FreezeRecord, FreezeStatus } from '@/api/types';
import { computerFreeze, coveringFreeze, frozenBy, frozenSentence, NO_REASON, reasonOf } from './freeze';

const record = (scope: FreezeRecord['scope'], target: string, extra: Partial<FreezeRecord> = {}): FreezeRecord => ({
  id: `fz_${scope}_${target}`,
  scope,
  target,
  reason: 'a login we did not expect',
  by: 'owner',
  at: new Date(Date.now() - 5 * 60_000).toISOString(),
  ...extra,
});

const status = (frozen: FreezeRecord[]): FreezeStatus => ({
  available: true,
  frozen,
  lanes: [],
  node_frozen: frozen.some((item) => item.scope === 'node'),
});

describe('what covers an account', () => {
  it('is nothing when nothing is frozen, and nothing before the status is read', () => {
    expect(coveringFreeze(undefined, 'aster/instagram')).toBeNull();
    expect(coveringFreeze(status([]), 'aster/instagram')).toBeNull();
  });

  it('is its own freeze, or its workspace, or the whole computer, widest first', () => {
    const lane = record('lane', 'aster/instagram');
    const workspace = record('workspace', 'aster');
    const node = record('node', 'dev');
    expect(coveringFreeze(status([lane]), 'aster/instagram')).toBe(lane);
    expect(coveringFreeze(status([lane, workspace]), 'aster/instagram')).toBe(workspace);
    expect(coveringFreeze(status([lane, workspace, node]), 'aster/instagram')).toBe(node);
  });

  it('does not reach an account in another workspace or another lane', () => {
    expect(coveringFreeze(status([record('lane', 'aster/blog'), record('workspace', 'harbor')]), 'aster/instagram')).toBeNull();
  });

  it('reads the computer freeze on its own', () => {
    expect(computerFreeze(status([record('lane', 'aster/instagram')]))).toBeNull();
    expect(computerFreeze(status([record('node', 'dev')]))?.scope).toBe('node');
  });
});

describe('what a freeze says', () => {
  it('names who froze it in words, never by an id', () => {
    expect(frozenBy({ by: 'owner' })).toBe('the owner');
    expect(frozenBy({ by: 'owner (token)' })).toBe('the owner');
    expect(frozenBy({ by: 'member:m_3f9a' })).toBe('a member');
    expect(frozenBy({ by: 'agent:claude-code' })).toBe('an agent');
    expect(frozenBy({ by: 'push' })).toBe('a notification link');
  });

  it('says when it started in one calm sentence', () => {
    const now = Date.now();
    expect(frozenSentence({ at: new Date(now - 5 * 60_000).toISOString(), by: 'owner' }, now)).toBe('Frozen 5m ago by the owner.');
    expect(frozenSentence({ at: new Date(now).toISOString(), by: 'owner' }, now)).toBe('Frozen just now by the owner.');
  });

  it('shows the node\'s stand-in when no reason was given, and the reason otherwise', () => {
    expect(reasonOf({ reason: '  ' })).toBe(NO_REASON);
    expect(reasonOf({ reason: 'odd login' })).toBe('odd login');
  });
});
