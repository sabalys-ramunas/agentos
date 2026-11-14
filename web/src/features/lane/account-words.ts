/**
 * The words an account's page says about its pace, limits, queue, taint, check, keepalive and sites.
 * Pure, so each sentence is tested once and the screens only place it. Plain English for a person;
 * the node's field names and money stay out of these unless a screen asks for Technical details.
 */
import type {
  ControlAction,
  KeepaliveVisit,
  LaneCheckBrief,
  LaneKeepalive,
  LanePace,
  LaneQueue,
  LaneSites,
  LimitName,
  Meter,
  MeterLimit,
  Pace,
  PaceBlock,
  TaintFamily,
} from '@/api/types';
import { clock, timeAgo, timeUntil } from '@/lib/format';
import { principalName } from '@/lib/principal';
import type { Tone } from '@/lib/labels';
import { STATE_WORDS } from '@/lib/states';

/** "08:00-22:00" becomes "08:00 and 22:00", so it reads after "between"; a day list in front stays, as written. */
export function windowWords(window: string): string {
  const match = /^(?:(.+?)\s+)?(\d{2}:\d{2})-(\d{2}:\d{2})$/.exec(window.trim());
  if (!match) return window;
  const [, days, from, to] = match;
  return days ? `${days} ${from} and ${to}` : `${from} and ${to}`;
}

/** "Posts go out between 08:00 and 22:00, at least 45 minutes apart, 3 a day." Null when nothing is paced. */
export function paceSentence(pace: Pace | null): string | null {
  if (!pace) return null;
  const parts: string[] = [];
  if (pace.active_hours?.length) parts.push(`between ${pace.active_hours.map(windowWords).join(' and ')}`);
  if (pace.min_gap_minutes) parts.push(`at least ${pace.min_gap_minutes} minutes apart`);
  if (pace.commits_per_day != null) parts.push(`${pace.commits_per_day} ${pace.commits_per_day === 1 ? 'post' : 'posts'} a day`);
  if (!parts.length) return null;
  const [first, ...rest] = parts;
  return `Posts go out ${first}${rest.length ? `, ${rest.join(', ')}` : ''}.`;
}

const BLOCKED: Record<PaceBlock, { word: string; tone: Tone }> = {
  active_hours: { word: `${STATE_WORDS.sleeping} for now`, tone: 'idle' },
  min_gap: { word: 'Between posts', tone: 'info' },
  commits_per_day: { word: 'Done for today', tone: 'idle' },
  timezone: { word: 'Clock not known', tone: 'warn' },
};

/** Whether the next post may go, in one word and one tone. */
export function paceStatus(pace: LanePace): { word: string; tone: Tone } {
  if (!pace.paced) return { word: 'Not paced', tone: 'neutral' };
  if (pace.may_post_now) return { word: 'May post now', tone: 'ok' };
  return pace.blocked_by ? BLOCKED[pace.blocked_by] : { word: 'Paused', tone: 'idle' };
}

/** "Lane time 00:29, on Europe/Vilnius." The clock the windows are read on. */
export function laneTimeWords(pace: LanePace): string {
  return `Lane time ${pace.local_now.slice(11, 16)}, on ${pace.zone.timezone}.`;
}

/** "Next post from 08:00 tomorrow.", or the date when it is further off. Null when nothing blocks it. */
export function nextPostWords(pace: LanePace): string | null {
  if (!pace.next_allowed_local) return null;
  const day = pace.next_allowed_local.slice(0, 10);
  const today = pace.local_now.slice(0, 10);
  const tomorrow = new Date(new Date(`${today}T12:00:00Z`).getTime() + 86_400_000).toISOString().slice(0, 10);
  const when = day === today ? 'today' : day === tomorrow ? 'tomorrow' : day;
  return `Next post from ${pace.next_allowed_local.slice(11, 16)} ${when}.`;
}

/**
 * "Quiet from 22:00 to 08:00." The hours outside one plain waking window, read on the same clock. Null when
 * there is no single window, or it has days in it, because then the quiet hours are not one stretch.
 */
export function quietHoursWords(hours: string[] | null | undefined): string | null {
  if (!hours || hours.length !== 1) return null;
  const match = /^(\d{2}:\d{2})-(\d{2}:\d{2})$/.exec((hours[0] ?? '').trim());
  if (!match) return null;
  const [, from, to] = match;
  if (from === to) return null;
  return `Quiet from ${to} to ${from}.`;
}

/** "Posted 2 of 3 today, the last 2h ago." */
export function postsWords(pace: LanePace): string {
  const cap = pace.pace?.commits_per_day;
  const count = cap != null ? `${pace.posts_today} of ${cap}` : `${pace.posts_today}`;
  const last = pace.last_post_at ? `, the last ${timeAgo(pace.last_post_at)}` : '';
  return `Posted ${count} today${last}.`;
}

/** A limit's name, in the words a person uses for it. */
export function limitName(name: LimitName): string {
  switch (name) {
    case 'commits_per_day':
      return 'Posts a day';
    case 'commits_per_hour':
      return 'Posts an hour';
    case 'usd_per_day':
      return 'Spending a day';
    case 'tokens_per_day':
      return 'Model use a day';
  }
}

/** "4 of 6 posts today." Money and model use are asked for in Technical details, so they never lead. */
export function limitUsed(limit: MeterLimit): string {
  if (limit.name === 'commits_per_day') return `${limit.used} of ${limit.value} posts today`;
  if (limit.name === 'commits_per_hour') return `${limit.used} of ${limit.value} posts this hour`;
  return `${limit.used} of ${limit.value} ${limit.unit}`;
}

/** The one word for a limit's state, with the tone it carries. */
export function limitStatus(state: MeterLimit['state']): { word: string; tone: Tone } {
  if (state === 'cut_off') return { word: 'Paused until it resets', tone: 'bad' };
  if (state === 'warning') return { word: 'Near its limit', tone: 'warn' };
  return { word: 'Within its limit', tone: 'ok' };
}

/** "Warns at 80%. At the limit, posts pause until it resets." From the node's `warn_at`. */
export function limitRule(meter: Meter): string {
  const at = meter.warn_at.map((fraction) => `${Math.round(fraction * 100)}%`);
  const warn = at.length ? `Warns at ${at.join(' and ')}.` : 'No warning comes before the limit.';
  return `${warn} At the limit, posts pause until it resets.`;
}

/** "Resets in 9h.", for the soonest day limit. */
export function resetWords(iso: string): string {
  const left = timeUntil(iso);
  return left && left !== 'Expired' ? `Resets in ${left}.` : 'Resets soon.';
}

/** Whoever waits or holds, in a person's words: the agent's name, or the routine's, never its run id. */
export function holderName(label: string, kind: string): string {
  const agent = /^agent (.+)$/.exec(label);
  if (agent) return principalName({ kind: 'agent', id: agent[1] ?? '', thread: null });
  const run = /^flow run \S+ \((.+)\)$/.exec(label);
  if (run) return `the ${(run[1] ?? '').replace(/[-_.]/g, ' ')} routine`;
  if (kind === 'flow') return 'a routine';
  return label;
}

const lowerFirst = (text: string) => text.charAt(0).toLowerCase() + text.slice(1);

/** "Claude Code is using the account, for post the morning photo." and the line for who waits behind it. */
export function queueSentence(queue: LaneQueue): { lead: string; waiting: string | null } {
  const claim = queue.claim;
  const purpose = claim?.purpose ? `, for ${lowerFirst(claim.purpose)}` : '';
  const lead = !claim
    ? 'Nobody holds the account now.'
    : claim.yours
      ? `You hold the account${purpose}.`
      : `${holderName(claim.holder, claim.kind)} holds the account${purpose}.`;
  const count = queue.queue.length;
  if (!count) return { lead, waiting: null };
  const names = queue.queue.map((item) => holderName(item.label, item.kind));
  const list = names.length === 1 ? names[0] : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
  return { lead, waiting: count === 1 ? `One thing waits: ${list}.` : `${count} things wait: ${list}.` };
}

/** What taint means for the next post: an agent read outside text, so its next post asks first. */
export function taintSentence(family: TaintFamily, owner: boolean): string {
  const read = family.after_untrusted ? `a page at ${family.after_untrusted.replace(/^a web page at /, '')}` : 'text from outside';
  return `An agent read ${read}, so its next post ${owner ? 'asks you first' : 'asks a person first'}.`;
}

/** The health of an account in one word, from the brief, which needs no probe. */
export function checkStatus(brief: Pick<LaneCheckBrief, 'level' | 'safe'>): { word: string; tone: Tone } {
  if (brief.safe === false || brief.level === 'fail') return { word: 'Not safe to use right now', tone: 'bad' };
  if (brief.level === 'warn') return { word: 'Safe to use, with a warning', tone: 'warn' };
  if (brief.level === 'ok') return { word: 'Safe to use', tone: 'ok' };
  return { word: 'Not checked yet', tone: 'neutral' };
}

const OUTCOME: Record<string, string> = {
  ok: 'it stayed signed in',
  signed_out: 'it found the sign-in gone',
  challenged: 'the site asked a question',
  skipped: 'it was skipped',
  failed: 'it did not finish',
};

/** "Last visit 2h ago, and it stayed signed in." */
export function visitWords(visit: KeepaliveVisit): string {
  return `Last visit ${timeAgo(visit.finished_at || visit.at)}, and ${OUTCOME[visit.outcome] ?? visit.outcome.replaceAll('_', ' ')}.`;
}

type Plan = Extract<LaneKeepalive, { available: true }>;

/** Whether the keepalive has anything to say: a problem, a visit, or a sign-in it keeps warm. Nothing else. */
export function keepaliveSaysSomething(keepalive: LaneKeepalive): keepalive is Plan {
  if (!keepalive.available) return false;
  if (keepalive.status !== null || keepalive.visits.length > 0) return true;
  return keepalive.enabled && keepalive.site !== null;
}

/** What keeps the sign-in warm, in plain words. A problem with the sign-in comes first. */
export function keepaliveSentence(keepalive: Plan): string {
  if (keepalive.status === 'signed_out') return 'The sign-in has run out. Take over and sign in once.';
  if (keepalive.status === 'challenged') return 'The site asked this account a question, so the visits wait until someone answers it.';
  // The node's reason for a pause is written for the console, so a person is told only that it is paused.
  if (keepalive.blocked) return 'It is paused for now, so no visit runs. It starts again by itself.';
  const site = keepalive.site ? `the ${keepalive.site} sign-in` : 'the sign-in';
  return `A short visit keeps ${site} warm, so it does not run out between posts.`;
}

/** "Next visit at 14:20.", or null when none is planned. */
export function nextVisitWords(keepalive: Plan): string | null {
  return keepalive.next_at ? `Next visit at ${clock(keepalive.next_at)}.` : null;
}

/** A frame on the list is a site a page may show, not one the account goes to itself. */
export function siteWords(site: string): { host: string; note: string | null } {
  return site.startsWith('frame:') ? { host: site.slice('frame:'.length), note: 'Only inside pages' } : { host: site, note: null };
}

export function sitesSentence(sites: LaneSites): string {
  return sites.sites.length
    ? 'This account reaches only these sites.'
    : 'No list: this account can reach any site.';
}

/** The limits in the order a person reads them: posts first, then money and model use. */
export function sortedLimits(meter: Meter): MeterLimit[] {
  const order: Record<LimitName, number> = { commits_per_day: 0, commits_per_hour: 1, usd_per_day: 2, tokens_per_day: 3 };
  return [...meter.limits].sort((a, b) => order[a.name] - order[b.name]);
}

/** Whether a limit is one a person sees without Technical details. Money and model use wait there. */
export const isPlainLimit = (name: LimitName) => name === 'commits_per_day' || name === 'commits_per_hour';

/** A whole number of zero or more from a field, or null when it is empty or not one. */
export function wholeOrNull(value: string): number | null {
  const text = value.trim();
  if (text === '') return null;
  const number = Number(text);
  return Number.isInteger(number) && number >= 0 ? number : null;
}

/** A site as a person types it: a host, optionally a `*.` pattern, optionally a `frame:` prefix. */
export function validSite(value: string): boolean {
  const text = value.trim().replace(/^frame:/, '');
  return /^(?:\*\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+$/i.test(text);
}

/** The window a person typed as the node writes it, "08:00-22:00", or null when either time is missing. */
export function windowText(from: string, to: string): string | null {
  return /^\d{2}:\d{2}$/.test(from) && /^\d{2}:\d{2}$/.test(to) && from !== to ? `${from}-${to}` : null;
}

/**
 * What the person did to an account, as the audit names it (`panel.<action>`), in their own words. Only
 * the changes: the audit also records the owner's reads of a page (`lane.check`, `lane.keepalive`,
 * `lane.sites`), and those are not in this list.
 */
const OWN_ACTION: Record<string, string> = {
  'lane.control': 'You changed who has control',
  freeze: 'You froze it',
  unfreeze: 'You lifted the freeze',
  'lane.pace': 'You changed its pace',
  'lane.limits': 'You changed its limits',
  'lane.worker.set': 'You changed its worker',
  'lane.worker.ask_first': 'You changed its worker',
  'ask_first.set': 'You changed Ask me first',
  'taint.clear': 'You cleared what it read from outside',
};

/**
 * The owner's own change to this account, in their words ("You froze it"), or null. Only a person's
 * change that took effect counts: a read (a check, a screenshot, a digest), a refusal and an agent's
 * own work are not this sentence.
 */
export function ownActionSentence(row: { tool: string; principal: unknown; status: string; control?: ControlAction | null }): string | null {
  const by = row.principal;
  if (typeof by !== 'object' || by === null || (by as { kind?: unknown }).kind !== 'human') return null;
  if (row.status === 'denied' || row.status === 'failed') return null;
  const tool = row.tool.replace(/^panel\./, '');
  if (tool === 'lane.control' && row.control) return CONTROL_WORDS[row.control];
  return OWN_ACTION[tool] ?? null;
}

/** A take over, a hand back and the rest, as the trace names the action (the body's `action`). */
const CONTROL_WORDS: Record<ControlAction, string> = {
  take: 'You took over',
  hand_back: 'You handed it back',
  stop_agent: 'You stopped the agent',
  deny_request: 'You kept control',
};
