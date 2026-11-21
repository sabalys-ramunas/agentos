/**
 * What Home and Accounts say, as plain functions: an account's state and the one thing it
 * needs, a business's status sentence, a sentence for what an agent did, and the page's answer
 * to "is everything OK, and what needs me?" (structure.md section 5, visual.md P2).
 */
import type { EnvState, HealthState, HomeFace, Lane, TraceEvent, Workspace } from '@/api/types';
import { laneName } from '@/lib/labels';
import { SITES, siteName, siteOf } from '@/lib/sites';
import type { Condition, LaneContext, PersonState } from '@/lib/states';
import { laneCondition, STATE_WORDS } from '@/lib/states';
import { FREEZE_WORDS } from '@/lib/vocabulary';

/** What an environment's agent is doing, in a few words under its picture on Home ("Codex · Starting its computer"). */
export const ENV_DOING: Record<EnvState, string> = {
  starting: 'Starting its computer',
  ready: 'Ready for its first step',
  working: 'At work now',
  idle: 'Waiting for its next step',
  closing: 'Closing its computer',
  closed: 'Closed',
  failed: 'Its computer did not start',
};

export const FACE_STATE: Record<HomeFace, PersonState> = {
  all_good: 'good',
  waiting: 'waiting',
  stuck: 'stuck',
  sleeping: 'sleeping',
  working: 'working',
};

/** What a person calls the account: the site's name, or the lane's own for a tool. */
export function accountName(lane: Pick<Lane, 'name' | 'site'>): string {
  return lane.site?.label ?? siteOf(lane.name)?.name ?? laneName(lane.name);
}

/** A website login, as opposed to a tool on the computer (a shell, a sync). */
export function isAccount(lane: Lane): boolean {
  if (lane.kind === 'browser' || lane.kind === 'android') return lane.site?.key !== 'terminal';
  return false;
}

/** A feed item that is one thing done, not a step inside one or a person looking. */
export const isStory = (item: Pick<TraceEvent, 'kind' | 'tool'>) => (item.kind === 'run' || item.kind === 'tool_call') && !/screenshot|snapshot/.test(item.tool);

const SENTENCES: Partial<Record<HealthState, (site: string) => string>> = {
  quarantined: (site) => `${site} is switched off for safety`,
  wrong_account: (site) => `${site} is signed in as someone else`,
  logged_out: (site) => `${site} needs you to sign in`,
  captcha: (site) => `${site} wants a quick human check`,
  checkpoint: (site) => `${site} wants to confirm it is you`,
  blocked: (site) => `${site} is refusing the account for now`,
  rate_limited: (site) => `${site} was asked to slow down`,
  offline: (site) => `${site} waits for its computer to come back`,
  missing: (site) => `${site} stopped reporting`,
};

/**
 * "Instagram is switched off for safety": an account's problem as a business-level sentence. Its state is
 * `accountCondition`'s, so a frozen account says it is frozen and a failing check says it is not safe.
 */
export function problemSentence(lane: Lane, me?: LaneContext['me']): string | null {
  const condition = accountCondition(lane, me);
  if (condition.state !== 'stuck' && condition.state !== 'waiting') return null;
  if (lane.frozen === true) return `${accountName(lane)} is frozen`;
  const state = lane.health?.state;
  const say = state ? SENTENCES[state] : undefined;
  if (say) return say(accountName(lane));
  return `${accountName(lane)}: ${lower(condition.sentence)}`;
}

/**
 * Each stuck account as Home's fallback names it, with its business: "Biz A's Social is frozen." The same
 * rule as the node's `/home` (api/web/home.py stuck_accounts), so the page says the same with or without it.
 */
export function stuckSentences(workspaces: Workspace[], me?: LaneContext['me']): string[] {
  return workspaces.flatMap((workspace) =>
    workspace.lanes
      .filter((lane) => accountCondition(lane, me).state === 'stuck')
      .map((lane) => {
        const place = `${workspace.name}'s ${accountName(lane)}`;
        if (lane.frozen === true) return `${place} is frozen.`;
        return `${place}: ${lower(accountCondition(lane, me).sentence)}.`;
      }),
  );
}

const lower = (text: string) => text.charAt(0).toLowerCase() + text.slice(1);

/**
 * An account in the person's words. The cloud's `health.face` decides the state, so the counts on
 * Home and the badge on a card always agree; the sentence comes from `laneCondition` when it says
 * the same, else from the cloud's own reason.
 */
export function accountCondition(lane: Lane, me?: LaneContext['me']): Condition {
  const local = laneCondition(lane, { me });
  // The account's own failing check, and a freeze, are the stop (as on its page and in the map), whatever the cloud's face says.
  if (lane.frozen === true || lane.check?.safe === false || lane.check?.level === 'fail') return local;
  const face = lane.health?.face;
  if (!face) return local;
  const state = FACE_STATE[face];
  if (local.state === state) return local;
  const reason = lane.health?.reason?.replace(/\.$/, '');
  if (state === 'working') return { state, sentence: 'An agent is working on it now' };
  if (state === 'sleeping') return { state, sentence: 'Resting until the next job' };
  if (state === 'good') return { state, sentence: 'Ready' };
  return { state, sentence: reason ?? STATE_WORDS[state] };
}

export interface NextStep {
  label: string;
  /** Where the step is done: the account page, or the sign-in list. */
  to: 'account' | 'logins';
}

/** The one thing an account needs from a person, if any. */
export function nextStep(lane: Lane): NextStep | null {
  switch (lane.health?.state ?? null) {
    case 'logged_out':
      return { label: 'Sign in', to: 'account' };
    case 'captcha':
      return { label: 'Do the check', to: 'account' };
    case 'checkpoint':
      return { label: 'Confirm it is you', to: 'account' };
    case 'wrong_account':
      return { label: 'Sign in again', to: 'account' };
    case 'quarantined':
    case 'blocked':
    case 'missing':
    case 'offline':
      return { label: 'See what happened', to: 'account' };
    default:
      break;
  }
  const condition = accountCondition(lane);
  if (condition.state === 'working') return { label: 'Watch', to: 'account' };
  if (condition.state === 'waiting') return { label: 'Take a look', to: 'account' };
  return null;
}

/** "All 6 accounts fine", "Instagram is switched off for safety", "Resting". */
export function businessSentence(workspace: Workspace, options: { asks?: number; next?: string | null; me?: LaneContext['me'] } = {}): { state: PersonState; text: string; label?: string } {
  const lanes = workspace.lanes;
  const { me } = options;
  const faces = lanes.map((lane) => accountCondition(lane, me).state);
  const problems = lanes.filter((lane) => {
    const state = accountCondition(lane, me).state;
    return state === 'stuck' || state === 'waiting';
  });
  const stuck = problems.filter((lane) => accountCondition(lane, me).state === 'stuck');
  const first = stuck[0] ?? problems[0];
  const state: PersonState = workspace.state ? FACE_STATE[workspace.state] : stuck.length ? 'stuck' : problems.length || options.asks ? 'waiting' : faces.includes('working') ? 'working' : faces.length && faces.every((face) => face === 'sleeping') ? 'sleeping' : 'good';

  if (lanes.length === 0) return { state: 'sleeping', text: 'No accounts yet' };
  // Two problems name each one's own state, as its account page does; more than two are counted.
  if (problems.length === 2) return { state, text: `${problemSentence(problems[0]!, me) ?? accountName(problems[0]!)}, and ${problemSentence(problems[1]!, me) ?? accountName(problems[1]!)}` };
  if (problems.length > 2) return { state, text: `${problems.length} accounts need a look` };
  // One frozen account is Frozen on the card as on its page; the state word alone would say Stuck.
  if (first) return { state, text: problemSentence(first, me) ?? `${accountName(first)} needs a look`, label: first.frozen === true && problems.length === 1 ? FREEZE_WORDS.frozen.word : undefined };
  const asks = options.asks ?? 0;
  if (asks > 0) return { state: 'waiting', text: asks === 1 ? 'One thing waits for your yes' : `${asks} things wait for your yes` };
  const working = lanes.filter((lane) => accountCondition(lane).state === 'working');
  if (working.length) return { state: 'working', text: `Working on ${listOf(working.map(accountName))} now` };
  if (faces.every((face) => face === 'sleeping')) return { state: 'sleeping', text: options.next ? `${STATE_WORDS.sleeping}. Next: ${options.next}` : `${STATE_WORDS.sleeping} until the next job` };
  return { state: 'good', text: lanes.length === 1 ? 'Its account is fine' : `All ${lanes.length} accounts fine` };
}

function listOf(names: string[]): string {
  const unique = [...new Set(names)];
  if (unique.length <= 1) return unique[0] ?? '';
  if (unique.length === 2) return `${unique[0]} and ${unique[1]}`;
  return `${unique.slice(0, -1).join(', ')} and ${unique.at(-1)}`;
}

const PAST: Record<string, string> = {
  check: 'Checked',
  post: 'Posted',
  publish: 'Published',
  reply: 'Replied',
  read: 'Read',
  boost: 'Boosted',
  delete: 'Deleted',
  save: 'Saved',
  send: 'Sent',
  draft: 'Drafted',
  release: 'Released',
  rebuild: 'Rebuilt',
  summarise: 'Summarised',
  summarize: 'Summarised',
  sync: 'Synced',
};

const SITE_NAMES = new RegExp(`\\bto (${[...SITES.values()].map((site) => site.name).join('|')})\\b`, 'g');

const BASE: Record<string, string> = Object.fromEntries(Object.entries(PAST).map(([base, past]) => [past.toLowerCase(), base]));

/** Domains and lane ids in a summary, as the site's name: "to instagram.com" reads "on Instagram". */
function sitesInWords(text: string): string {
  return text
    .replace(/\b(to|on|from) ((?:[a-z0-9-]+\.)+[a-z]{2,})\b/gi, (_match, preposition: string, host: string) => {
      const known = siteOf(host);
      const where = known ? known.name : host.startsWith('blog.') ? 'the blog' : host;
      return `${preposition.toLowerCase() === 'from' ? 'from' : 'on'} ${where}`;
    })
    .replace(/\bon ([a-z0-9-]+)\/([a-z0-9-]+)\b/gi, (_match, _ws: string, name: string) => `on ${siteName(name)}`)
    .replace(/(^|\s)((?:[a-z0-9-]+\.)+[a-z]{2,})(?=$|\s|[.,])/gi, (_match, space: string, host: string) => {
      const known = siteOf(host);
      return `${space}${known ? known.name : host.startsWith('blog.') ? 'the blog' : host}`;
    })
    .replace(SITE_NAMES, 'on $1');
}

/**
 * One sentence for what an agent did, from a feed item: "Checked LinkedIn", "Posted a photo on
 * Instagram", "Could not post a photo on Facebook". Strips the node's "as @handle" and its
 * "Readback confirmed it.", which the status already says.
 */
export function didSentence(item: Pick<TraceEvent, 'summary' | 'status'>): string {
  let text = item.summary
    .trim()
    .replace(/^Failed:\s*/i, '')
    .replace(/\.?\s*Readback (confirmed|could not confirm|found) it\.?$/i, '')
    .replace(/\s+as @[\w.-]+/gi, '')
    .replace(/\s+as @?[\w.-]+\.?$/i, '')
    .replace(/\s+with caption\b/i, '')
    .replace(/\.$/, '');
  text = sitesInWords(text).replace(/\b1 (?=[a-z])/g, (_one, offset: number, whole: string) => (/^[aeiou]/i.test(whole.slice(offset + 2)) ? 'an ' : 'a '));
  const [first = '', ...rest] = text.split(' ');
  const verb = first.toLowerCase();
  const tail = rest.join(' ');
  if (item.status === 'failed') {
    const base = PAST[verb] ? verb : (BASE[verb] ?? verb.replace(/ed$/, '').replace(/i$/, 'y'));
    return `Could not ${base}${tail ? ` ${tail}` : ''}`;
  }
  const past = PAST[verb] ?? (first ? first.charAt(0).toUpperCase() + first.slice(1) : '');
  const sentence = `${past}${tail ? ` ${tail}` : ''}`;
  return item.status === 'partial' ? `Partly done: ${lower(sentence)}` : sentence;
}

export interface Hero {
  /** The answer, in a heavy weight. */
  lead: string;
  /** The rest of the sentence, in a lighter one. */
  rest: string;
  tone: PersonState;
}

/** The page title: one sentence that answers the question before any number does. */
export function heroSentence(input: { needs: number; offline: string[]; accounts: number; canSetUp?: boolean; stuck?: string[] }): Hero {
  if (input.offline.length === 1) return { lead: `${input.offline[0]} is offline.`, rest: 'Its accounts wait until it is back.', tone: 'stuck' };
  if (input.offline.length > 1) return { lead: `${input.offline.length} computers are offline.`, rest: 'Their accounts wait until they are back.', tone: 'stuck' };
  if (input.accounts === 0) {
    const rest = input.canSetUp === false ? 'Your workspaces show here once they are set up.' : 'Add your first account, and this page fills up.';
    return { lead: 'Welcome.', rest, tone: 'sleeping' };
  }
  // A stuck account is never "everything else is fine": it is the rest of the sentence, as the node says it.
  const stuck = input.stuck?.[0];
  if (input.needs === 1) return { lead: 'One thing needs you.', rest: stuck ?? 'Everything else is fine.', tone: stuck ? 'stuck' : 'waiting' };
  if (input.needs > 1) return { lead: `${input.needs} things need you.`, rest: stuck ?? 'Everything else is fine.', tone: stuck ? 'stuck' : 'waiting' };
  if (stuck) return { lead: stuck, rest: 'Nothing needs you.', tone: 'stuck' };
  return { lead: 'All good.', rest: 'Nothing needs you.', tone: 'good' };
}

/** Whose account it is, for a stand-in: the handle the site shows when known, else the workspace. */
export function accountTitle(lane: Pick<Lane, 'account'>, workspaceName?: string | null): string | null {
  const id = lane.account?.user_id;
  if (id) return id.includes('@') || id.includes(' ') ? id : `@${id.replace(/^@/, '')}`;
  return workspaceName ?? null;
}

/** "Posted a photo · 4h ago", for the line under a stand-in. */
export function lastLine(last: Pick<TraceEvent, 'summary' | 'status' | 'at'> | null | undefined, lastUsed?: string | null, ago: (iso: string) => string = (iso) => iso): string | null {
  if (last) return `${didSentence(last)} · ${ago(last.at).replace(/^Just now$/, 'just now')}`;
  if (lastUsed) return `Last used ${ago(lastUsed).replace(/^Just now$/, 'just now')}`;
  return 'Nothing done yet';
}

