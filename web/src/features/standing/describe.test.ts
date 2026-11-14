import type { Approval, Flow, LaneAskFirst, StandingApproval } from '@/api/types';
import { canAllowAgain, covers, limitGap, STANDING_EFFECTS, standingEffects, standingNote, standsAsTarget } from './describe';

const item = (over: Partial<StandingApproval> = {}): StandingApproval => ({
  id: 'sa_1',
  workspace: 'aster',
  effect: 'commit',
  lanes: ['aster/instagram'],
  count: 20,
  used: 3,
  remaining: 17,
  per_day: null,
  until: '2026-11-20T00:00:00Z',
  targets: [],
  contents: 0,
  covers_publishing: true,
  note: null,
  state: 'active',
  created_at: '2026-11-01T00:00:00Z',
  revoked_at: null,
  enforcement: { state: 'applied', applied_by: ['home-mini'], not_applied_by: [], message: '' } as StandingApproval['enforcement'],
  ...over,
});

const ask = (lane: string, asks: boolean): LaneAskFirst => ({
  lane,
  ask_first: asks,
  asks_first: asks,
  ask_first_set_by: null,
  ask_first_set_at: null,
  ask_first_supported: true,
  ask_first_online: true,
});

const flow = (id: string, asks: boolean): Flow => ({ id, lane: 'aster/instagram', version: 4, params: {}, effect: 'commit', last_run: null, asks_first: asks });

describe('limitGap', () => {
  it('says where a commit runs on its own, because nothing there uses the limit', () => {
    expect(limitGap(item(), [ask('aster/instagram', false)], [])).toEqual({ routines: [], lanes: ['aster/instagram'] });
  });

  it('is quiet where the account asks first, so the limit holds', () => {
    expect(limitGap(item(), [ask('aster/instagram', true)], [])).toBeNull();
  });

  it('names only the accounts that run on their own, in the workspace it covers', () => {
    const all = item({ lanes: null });
    const asks = [ask('aster/instagram', false), ask('aster/gmail', true), ask('harbor/xero', false)];
    expect(limitGap(all, asks, [])).toEqual({ routines: [], lanes: ['aster/instagram'] });
  });

  it('follows a routine it is bound to, not the account around it', () => {
    const bound = item({ targets: ['flow:aster.instagram.post@4'] });
    expect(limitGap(bound, [ask('aster/instagram', false)], [flow('aster.instagram.post', true)])).toBeNull();
    expect(limitGap(bound, [ask('aster/instagram', true)], [flow('aster.instagram.post', false)])).toEqual({ routines: ['aster.instagram.post'], lanes: [] });
  });

  it('falls back to the accounts when the routine it names is not reported', () => {
    expect(limitGap(item({ targets: ['flow:aster.gone@1'] }), [ask('aster/instagram', false)], [])).toEqual({ routines: [], lanes: ['aster/instagram'] });
  });

  it('never flags a computer that always asks', () => {
    expect(limitGap(item(), [{ ...ask('aster/instagram', false), asks_first: true, ask_first_supported: false }], [])).toBeNull();
  });

  it('only speaks of commits, and of what is still active', () => {
    const off = [ask('aster/instagram', false)];
    expect(limitGap(item({ effect: 'secret' }), off, [])).toBeNull();
    expect(limitGap(item({ effect: 'exec' }), off, [])).toBeNull();
    expect(limitGap(item({ state: 'used_up' }), off, [])).toBeNull();
    expect(limitGap(item({ state: 'revoked' }), off, [])).toBeNull();
  });

  it('claims nothing until it knows the switches', () => {
    expect(limitGap(item(), undefined, [])).toBeNull();
    expect(limitGap(item(), [], undefined)).toBeNull();
  });
});

describe('covers', () => {
  it('reads lane patterns inside the workspace', () => {
    expect(covers({ workspace: 'aster', lanes: ['aster/*'] }, 'aster/gmail')).toBe(true);
    expect(covers({ workspace: 'aster', lanes: ['aster/gmail'] }, 'aster/gmailx')).toBe(false);
    expect(covers({ workspace: 'aster', lanes: null }, 'harbor/xero')).toBe(false);
  });
});

describe('standsAsTarget and standingNote', () => {
  /** A recorded routine: its version is the content hash its approvals bind to. */
  const recorded = (over: Partial<Flow> = {}): Flow => ({ id: 'biz-a.social.post', lane: 'biz-a/social', version: 'sha256:e55702a2bc21157f', params: {}, effect: 'commit', last_run: null, ...over });
  /** A builder routine: its version is a count of the saves, which no approval carries. */
  const builder = (over: Partial<Flow> = {}): Flow => ({ id: 'biz-a.routine.bakery', lane: 'biz-a/social', version: 2, params: {}, effect: 'commit', last_run: null, ...over });
  const withInputs = { caption: { type: 'string', description: 'post text' } } as unknown as Flow['params'];

  it('offers a recorded routine that runs with no inputs', () => {
    expect(standsAsTarget(recorded())).toBe(true);
  });

  it('never offers a builder routine, since its posting step approves the recorded routine it runs', () => {
    expect(standsAsTarget(builder())).toBe(false);
    expect(standsAsTarget(builder({ effect: 'exec' }))).toBe(false);
  });

  it('does not offer a publishing routine with inputs, which a target alone never covers', () => {
    expect(standsAsTarget(recorded({ params: withInputs }))).toBe(false);
  });

  it('offers a recorded routine with inputs for a non-publishing effect, which a target covers', () => {
    expect(standsAsTarget(recorded({ effect: 'exec', params: withInputs }))).toBe(true);
  });

  it('says what to do instead, and only when a routine was left out', () => {
    expect(standingNote([recorded()])).toBeNull();
    expect(standingNote([recorded(), builder()])).toBe('Routines built in the builder ask each time. Allow one of their approvals again from Needs you.');
    expect(standingNote([recorded({ params: withInputs })])).toBe('A routine with inputs asks each time. Allow one of its approvals again from Needs you.');
  });

  it('names every reason a routine is left out, not only the builder one', () => {
    expect(standingNote([builder(), recorded({ params: withInputs })])).toBe(
      'Routines built in the builder and routines with inputs ask each time. Allow one of their approvals again from Needs you.',
    );
  });
});

describe('what a standing approval can cover', () => {
  it('is a commit only on a computer on its own, the one thing the node takes', () => {
    expect(standingEffects(true)).toEqual(['commit']);
    expect(standingEffects(false)).toEqual(STANDING_EFFECTS);
  });

  it('offers "Allow this again" only for a commit on a computer on its own', () => {
    const approval = (effect: string) =>
      ({ id: 'ap_1', kind: 'flow', effect, lane: 'aster/instagram', content_digest: 'sha256:x', flow_target: 'flow:aster.post@1', tool: null, state: 'approved' }) as unknown as Approval;
    expect(canAllowAgain(approval('commit'), true)).toBe(true);
    for (const effect of ['exec', 'secret', 'brain', 'service', 'install_overlay', 'pay', 'irreversible', 'install_base']) {
      expect(canAllowAgain(approval(effect), true)).toBe(false);
    }
  });
});
