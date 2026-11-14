/**
 * What the engagement screens say, in one place. Every sentence is built from what the node sent:
 * no count is made up, and a field the node does not send says nothing rather than zero.
 */
import type { EngageCapUse, EngageDay, EngageHold, EngageKind, EngageWindow } from '@/api/types';
import { clock } from '@/lib/format';
import { laneName } from '@/lib/labels';

const NAMES: Record<EngageKind, [one: string, many: string]> = {
  post: ['post', 'posts'],
  comment: ['comment', 'comments'],
  like: ['like', 'likes'],
  follow: ['follow', 'follows'],
  join: ['join', 'joins'],
  story_view: ['story view', 'story views'],
  profile_edit: ['profile edit', 'profile edits'],
};

/** "1 like", "3 likes". The count is the node's, so nothing here rounds it. */
export function countOf(count: number, kind: EngageKind): string {
  const [one, many] = NAMES[kind];
  return `${count} ${count === 1 ? one : many}`;
}

export function kindName(kind: EngageKind): string {
  return NAMES[kind][1].charAt(0).toUpperCase() + NAMES[kind][1].slice(1);
}

/** The signals a site shows, as plain sentences. Anything else says it is paused. */
const SIGNAL_WORDS: Record<string, string> = {
  rate_limit: 'The site asked the account to slow down',
  captcha: 'The site showed a robot check',
  checkpoint: 'The site asked a security question',
  logged_out: 'The site signed the account out',
  blocked: 'The site blocked the account',
  wrong_account: 'The account is signed in as someone else',
};

/** One pause, in a sentence a person reads, with when it ends. */
export function pauseTitle(hold: EngageHold): string {
  if (hold.scope === 'drafts') return 'The text provider could not be reached';
  if (hold.kind) return `${kindName(hold.kind)} are paused`;
  const signal = hold.signal ? SIGNAL_WORDS[hold.signal] : undefined;
  if (signal) return signal;
  if (hold.reason === 'no_valid_rule') return 'These actions are not checked yet';
  return 'The account is paused';
}

/** "Until 14:54" when the node says when it ends, else "Until you start it again". */
export function pauseUntil(hold: EngageHold): string {
  if (!hold.until) return 'Until you start it again';
  return `Until ${clock(hold.until)}`;
}

/** The day's line on the top of a page: what ran, or that nothing did because sessions are off. */
export function daySentence(days: EngageDay[], sessionsOn: boolean | null): string {
  if (days.length === 0) return 'No account is engaging yet.';
  const accounts = days.length === 1 ? '1 account' : `${days.length} accounts`;
  if (sessionsOn === false) {
    const running = days.filter((d) => d.program?.approved).length;
    const waiting = days.length - running;
    const parts = [
      running ? `${running === 1 ? '1 account runs' : `${running} accounts run`} a plan` : '',
      waiting ? `${waiting === 1 ? '1 plan waits' : `${waiting} plans wait`} for you` : '',
    ].filter(Boolean);
    return `${parts.join(', and ') || `${accounts} planned`}. Sessions are off, so nothing has been done.`;
  }
  const verified = days.reduce((sum, d) => sum + d.totals.verified, 0);
  // What waits for the owner now, as Home counts it, whatever day it was written.
  const waiting = days.reduce((sum, d) => sum + d.awaiting_now.count, 0);
  const went = verified === 1 ? '1 action went out' : `${verified} actions went out`;
  const tail = waiting ? `, and ${waiting === 1 ? '1 waits' : `${waiting} wait`} for you` : '';
  return `${went} across ${accounts}${tail}.`;
}

/** "Yesterday", "Today", or the date, for the day a page is showing. */
export function dayLabel(day: string, today: string): string {
  if (day === today) return 'Today';
  const yesterday = new Date(Date.parse(`${today}T12:00:00Z`) - 86_400_000).toISOString().slice(0, 10);
  if (day === yesterday) return 'Yesterday';
  return new Date(`${day}T12:00:00Z`).toLocaleDateString('en', { weekday: 'short', month: 'short', day: 'numeric' });
}

/** Seven days ending yesterday, newest first: what the day picker offers. */
export function pickableDays(today: string): string[] {
  const base = Date.parse(`${today}T12:00:00Z`);
  return Array.from({ length: 7 }, (_, i) => new Date(base - i * 86_400_000).toISOString().slice(0, 10));
}

/** Today on this computer, as YYYY-MM-DD: the day a person means by "today", whatever the hour in UTC. */
export function localDay(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** One limit as a line: "3 of 10 likes today", or "Ramp, up to 80 likes" when the owner has set none. */
export function limitLine(use: EngageCapUse): string {
  const limit = use.cap ?? use.ceiling;
  const used = `${use.used} of ${limit} ${NAMES[use.kind][1]}`;
  return use.cap === null ? `${used}, ramp up to ${use.ceiling}` : used;
}

/** The limit an account is closest to: the one a person would look at first. */
export function tightestLimit(caps: EngageCapUse[]): EngageCapUse | null {
  let best: EngageCapUse | null = null;
  let bestShare = -1;
  for (const use of caps) {
    const limit = use.cap ?? use.ceiling;
    const share = limit > 0 ? use.used / limit : 0;
    if (share > bestShare) {
      best = use;
      bestShare = share;
    }
  }
  return best;
}

/** "Likes 3 of 10", for a row that shows one limit. */
export function tightLimitText(use: EngageCapUse): string {
  return `${kindName(use.kind)} ${use.used} of ${use.cap ?? use.ceiling}`;
}

/** Whether a limit is nearly used up: the same 80 percent line as every meter. */
export function nearLimit(use: EngageCapUse): boolean {
  const limit = use.cap ?? use.ceiling;
  return limit > 0 && use.used / limit >= 0.8;
}

/** A visit as a sentence: when it was planned, and how long it was meant to last. */
export function visitWhen(window: EngageWindow): string {
  return `${clock(window.planned_start)}, about ${window.minutes} minutes`;
}

/** "Skipped because of the day's limit" and the like, from the reason the node gives. */
const SKIP_WORDS: Record<string, string> = {
  cap: 'the daily limit',
  hour_cap: 'the hour’s limit',
  held: 'a pause',
  no_content: 'no words to post',
  no_target: 'nothing to act on',
  bad_target: 'a target it cannot use',
  profile_current: 'the profile already says it',
  session_ended: 'the visit ended early',
  missed: 'the visit was missed',
};

export function skipWords(reasons: Record<string, number>): string {
  return Object.entries(reasons)
    .map(([reason, n]) => `${n} for ${SKIP_WORDS[reason] ?? reason.replace(/_/g, ' ')}`)
    .join(', ');
}

/** An account as a person names it, "Biz A · Social", never its id "biz-a/social" (the business's name when it is known). */
export function accountTitleOf(id: string, workspaces?: { id: string; name: string }[]): string {
  const [ws = '', name = ''] = id.split('/');
  const account = laneName(name);
  const business = workspaces?.find((item) => item.id === ws)?.name;
  return business ? `${business} · ${account}` : account;
}
