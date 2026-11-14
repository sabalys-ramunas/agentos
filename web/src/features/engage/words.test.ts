import { describe, expect, it } from 'vitest';
import type { EngageCapUse, EngageDay, EngageHold } from '@/api/types';
import { accountTitleOf, countOf, daySentence, dayLabel, limitLine, localDay, nearLimit, pauseTitle, pickableDays, skipWords, tightLimitText, tightestLimit } from './words';

const hold = (over: Partial<EngageHold> = {}): EngageHold => ({
  id: 1, scope: 'surface', kind: null, signal: null, reason: 'signal', detail: null, since: '2026-11-07T06:00:00Z',
  until: null, cleared_at: null, created_that_day: true, active_now: true, ...over,
});

const day = (over: Partial<EngageDay> = {}): EngageDay =>
  ({
    surface: 'aster/instagram', site: 'instagram', day: '2026-11-06', plan_saved: true, sessions_on: true,
    totals: { planned: 10, attempted: 8, verified: 7, failed: 0, awaiting_approval: 1, skipped: 2 },
    awaiting_now: { count: 1, days: {}, drafts: [] },
    ...over,
  }) as EngageDay;

describe('engagement words', () => {
  it('counts actions in the singular and the plural', () => {
    expect(countOf(1, 'like')).toBe('1 like');
    expect(countOf(3, 'like')).toBe('3 likes');
    expect(countOf(2, 'story_view')).toBe('2 story views');
  });

  it('says what paused an account, and never invents a reason', () => {
    expect(pauseTitle(hold({ signal: 'rate_limit' }))).toBe('The site asked the account to slow down');
    expect(pauseTitle(hold({ signal: 'captcha' }))).toBe('The site showed a robot check');
    expect(pauseTitle(hold({ kind: 'comment' }))).toBe('Comments are paused');
    expect(pauseTitle(hold({ scope: 'drafts' }))).toBe('The text provider could not be reached');
    expect(pauseTitle(hold({ reason: 'no_valid_rule' }))).toBe('These actions are not checked yet');
    expect(pauseTitle(hold({ signal: 'something_new' }))).toBe('The account is paused');
  });

  it('says a day in one sentence from what the node counted', () => {
    expect(daySentence([day(), day({ surface: 'b/x' })], true)).toBe(
      '14 actions went out across 2 accounts, and 2 wait for you.',
    );
    expect(daySentence([day({ totals: { ...day().totals, verified: 1 }, awaiting_now: { count: 0, days: {}, drafts: [] } })], true)).toBe(
      '1 action went out across 1 account.',
    );
  });

  it('says plainly when sessions are off, and which plans still wait for the owner', () => {
    const running = day({ program: { id: 'p', version: 1, approved: true } } as Partial<EngageDay>);
    const draft = day({ program: { id: 'q', version: 1, approved: false } } as Partial<EngageDay>);
    expect(daySentence([running], false)).toBe('1 account runs a plan. Sessions are off, so nothing has been done.');
    expect(daySentence([running, draft], false)).toBe(
      '1 account runs a plan, and 1 plan waits for you. Sessions are off, so nothing has been done.',
    );
    expect(daySentence([], true)).toBe('No account is engaging yet.');
  });

  it('shows the daily limit an account is closest to, not a sum of unlike actions', () => {
    const caps: EngageCapUse[] = [
      { kind: 'like', used: 2, cap: null, ceiling: 80, hourly: 15 },
      { kind: 'comment', used: 2, cap: 3, ceiling: 10, hourly: 3 },
    ];
    expect(tightestLimit(caps)?.kind).toBe('comment');
    expect(tightLimitText(tightestLimit(caps)!)).toBe('Comments 2 of 3');
    expect(tightestLimit([])).toBeNull();
  });

  it('names the day from today, and offers today and the six days before it', () => {
    expect(dayLabel('2026-11-07', '2026-11-07')).toBe('Today');
    expect(dayLabel('2026-11-06', '2026-11-07')).toBe('Yesterday');
    expect(pickableDays('2026-11-07')).toEqual([
      '2026-11-07', '2026-11-06', '2026-11-05', '2026-11-04', '2026-11-03', '2026-11-02', '2026-11-01',
    ]);
  });

  it('takes today from the computer’s own date, not from UTC', () => {
    // 01:30 on 8 November in Vilnius is still 7 November in UTC: the person's day is the 8th.
    expect(localDay(new Date(2026, 10, 8, 1, 30))).toBe('2026-11-08');
    expect(localDay(new Date(2026, 0, 3, 23, 59))).toBe('2026-01-03');
  });

  it('shows a limit as used of the owner’s cap, or of the ramp when there is none', () => {
    const set: EngageCapUse = { kind: 'like', used: 3, cap: 10, ceiling: 80, hourly: 15 };
    expect(limitLine(set)).toBe('3 of 10 likes');
    const ramp: EngageCapUse = { ...set, cap: null, used: 0 };
    expect(limitLine(ramp)).toBe('0 of 80 likes, ramp up to 80');
    expect(nearLimit({ ...set, used: 8 })).toBe(true);
    expect(nearLimit(set)).toBe(false);
  });

  it('says why each kind of action was skipped, from the node’s own reasons', () => {
    expect(skipWords({ cap: 2, no_content: 1 })).toBe('2 for the daily limit, 1 for no words to post');
    expect(skipWords({ unknown_reason: 3 })).toBe('3 for unknown reason');
  });
});

describe('an account by its name', () => {
  it('says the business and the account, never the id', () => {
    expect(accountTitleOf('biz-a/social', [{ id: 'biz-a', name: 'Biz A' }])).toBe('Biz A · Social');
    expect(accountTitleOf('biz-b/forum')).toBe('Forum');
    expect(accountTitleOf('biz-a/ig-bakery')).toBe('Ig bakery');
  });
});
