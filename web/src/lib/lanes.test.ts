import type { Lane } from '@/api/types';
import { laneStatus, idleControl } from './lanes';
import { laneCondition } from './states';

const base: Lane = {
  id: 'biz-a/social',
  workspace: 'biz-a',
  name: 'social',
  kind: 'browser',
  engine: 'camoufox',
  preset: 'account',
  state: 'warm',
  signal: 'ok',
  proxy: null,
  exit_ip: null,
  last_used: null,
  node: 'dev',
  quarantine_reason: null,
  logged_in_hint: 'ok',
  control: idleControl,
  hold: null,
  hold_until: null,
  signal_detail: null,
  account: null,
};
const lane = (patch: Partial<Lane>): Lane => ({ ...base, ...patch });

describe('one lane, one word, on every screen', () => {
  it.each<[string, Partial<Lane>, string]>([
    ['a frozen account reads Frozen, not Ready', { frozen: true }, 'Frozen'],
    ['a failing check reads Stuck, not Ready', { check: { level: 'fail', safe: false } }, 'Stuck'],
    ['a resting account reads Sleeping, the word Accounts uses', { state: 'cold' }, 'Sleeping'],
    ['a waiting account reads Waiting for you', { signal: 'captcha' }, 'Waiting for you'],
    ['an account an agent asked a login for reads Waiting for you', { login_request: { lane: 'biz-a/social', why: 'sign in', by: 'claude-code', at: '2026-11-07T10:00:00Z' } } as Partial<Lane>, 'Waiting for you'],
    ['a fine account reads All good', {}, 'All good'],
  ])('%s', (_name, patch, word) => {
    expect(laneStatus(lane(patch)).label).toBe(word);
  });

  it('is the same word as the rule Accounts reads, for every lane it is given', () => {
    for (const patch of [{ frozen: true }, { check: { level: 'fail', safe: false } }, { state: 'cold' }, { state: 'active' }, { signal: 'blocked' }] as Partial<Lane>[]) {
      const condition = laneCondition(lane(patch));
      expect(laneStatus(lane(patch)).label).toBe(condition.label ?? { good: 'All good', waiting: 'Waiting for you', stuck: 'Stuck', sleeping: 'Sleeping', working: 'Working now' }[condition.state]);
    }
  });

  it('gives a stuck lane the red tone, and a working one the live pulse', () => {
    expect(laneStatus(lane({ frozen: true }))).toEqual({ label: 'Frozen', tone: 'bad', pulse: false });
    expect(laneStatus(lane({ state: 'active' }))).toEqual({ label: 'Working now', tone: 'info', pulse: true });
  });

  it('says a computer that is down, to the lane on it', () => {
    expect(laneStatus(lane({}), { node: 'down' }).label).toBe('Stuck');
  });
});
