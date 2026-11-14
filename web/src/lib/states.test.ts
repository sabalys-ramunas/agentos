import type { Lane } from '@/api/types';
import { approvalCondition, laneCondition, nodeCondition, questionCondition, rollUp, STATE_WORDS } from './states';
import { idleControl } from './lanes';

const base: Lane = {
  id: 'aster/instagram',
  workspace: 'aster',
  name: 'instagram',
  kind: 'browser',
  engine: 'camoufox',
  preset: 'account',
  state: 'warm',
  signal: 'ok',
  proxy: { label: 'exit-aster' },
  exit_ip: null,
  last_used: null,
  node: 'home-mini',
  quarantine_reason: null,
  logged_in_hint: 'ok',
  control: idleControl,
  hold: null,
  hold_until: null,
  signal_detail: null,
  account: null,
};
const lane = (patch: Partial<Lane>): Lane => ({ ...base, ...patch });

describe('the four states', () => {
  it('has a word for each, and only these', () => {
    expect(Object.values(STATE_WORDS)).toEqual(['All good', 'Waiting for you', 'Stuck', 'Sleeping', 'Working now']);
  });

  it.each<[string, Partial<Lane>, string, string]>([
    ['a ready lane', {}, 'good', 'Ready'],
    ['a resting lane', { state: 'cold' }, 'sleeping', 'Resting until the next job'],
    ['an agent working', { state: 'active' }, 'working', 'An agent is working on it now'],
    ['the wrong account', { account: { site: 'instagram', user_id: null, source: null, status: 'wrong_account' } }, 'stuck', 'Signed in as someone else'],
    ['quarantined, with a fine signal', { state: 'quarantined', signal: 'ok' }, 'stuck', 'Switched off for safety'],
    ['blocked by the site', { signal: 'blocked' }, 'stuck', 'The site is blocking it'],
    ['its own check failed', { check: { level: 'fail', safe: false } }, 'stuck', 'Not safe to use right now'],
    ['its own check failed, though the signal is fine', { signal: 'ok', check: { level: 'fail', safe: false }, state: 'warm' }, 'stuck', 'Not safe to use right now'],
    ['a warning from its check is not a stop', { check: { level: 'warn', safe: true } }, 'good', 'Ready'],
    ['signed out', { signal: 'logged_out' }, 'waiting', 'Needs you to sign in'],
    ['a captcha', { signal: 'captcha' }, 'waiting', 'Asked a robot check'],
    ['a checkpoint', { signal: 'checkpoint' }, 'waiting', 'The site wants to confirm it is you'],
    ['slowed down', { signal: 'rate_limited' }, 'sleeping', 'The site asked it to slow down. It carries on by itself'],
    ['missing from its node', { missing_since: '2026-11-04T10:00:00Z' }, 'stuck', 'Its computer stopped reporting it'],
    ['agents paused', { control: { ...idleControl, state: 'paused' } }, 'waiting', 'Agents are paused until someone lets them carry on'],
  ])('says %s', (_name, patch, state, sentence) => {
    expect(laneCondition(lane(patch))).toEqual({ state, sentence });
  });

  it('puts what stops an account ahead of what is going on', () => {
    // Quarantined beats an agent acting; a down computer beats everything.
    expect(laneCondition(lane({ state: 'quarantined' }), { acting: true }).state).toBe('stuck');
    expect(laneCondition(lane({}), { node: 'down' }).sentence).toBe('Its computer is offline');
  });

  it('names who asks for control back, and who holds it', () => {
    const asking = lane({ control: { ...idleControl, takeback_requested: true, takeback_by: 'claude-code' } });
    expect(laneCondition(asking).sentence).toBe('Claude Code is asking for control back');
    const held = lane({ control: { ...idleControl, state: 'human', by: 'Dana', by_id: 'usr_D' } });
    expect(laneCondition(held).sentence).toBe('Dana has control');
    expect(laneCondition(held, { me: { id: 'usr_D', name: 'Dana' } }).sentence).toBe('You have control');
  });

  it('reads a frozen account as Frozen, the word a frozen computer gets', () => {
    expect(laneCondition(lane({ frozen: true }))).toEqual({ state: 'stuck', label: 'Frozen', sentence: 'Nothing runs on it until the freeze is lifted' });
    expect(laneCondition(lane({ frozen: true }), { acting: true }).label).toBe('Frozen');
  });

  it('maps computers', () => {
    expect(nodeCondition({ state: 'up' })).toEqual({ state: 'good', sentence: 'Online' });
    expect(nodeCondition({ state: 'down' }).state).toBe('stuck');
    expect(nodeCondition({ state: 'degraded' }).state).toBe('waiting');
  });

  it('maps approvals, and warns when the account cannot act', () => {
    expect(approvalCondition({ state: 'pending', effect: 'commit' })).toEqual({ state: 'waiting', sentence: 'Wants to go ahead, and waits for your yes' });
    expect(approvalCondition({ state: 'pending', effect: 'commit' }, lane({ state: 'quarantined' })).sentence).toBe(
      'Waits for you, but the account is not ready: switched off for safety',
    );
    expect(approvalCondition({ state: 'expired', effect: 'pay' }).state).toBe('sleeping');
  });

  it('maps questions', () => {
    expect(questionCondition({ state: 'open', asked_by: { kind: 'agent', id: 'codex' } })).toEqual({ state: 'waiting', sentence: 'Codex asked you something' });
  });

  it('rolls a group up: stuck beats waiting beats good, sleeping only when all sleep', () => {
    expect(rollUp(['good', 'stuck', 'waiting'])).toBe('stuck');
    expect(rollUp(['good', 'waiting'])).toBe('waiting');
    expect(rollUp(['good', 'good'], 2)).toBe('waiting');
    expect(rollUp(['sleeping', 'working'])).toBe('good');
    expect(rollUp(['sleeping', 'sleeping'])).toBe('sleeping');
  });
});
