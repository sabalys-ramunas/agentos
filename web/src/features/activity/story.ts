/**
 * History in words. Every run becomes one sentence a person understands ("Posted a photo on
 * Harbor Supply's Instagram"), a result in one of four kinds, and, when it did not work, what did
 * not happen and why. Built from the structured fields (lane, effect, status) and the node's
 * summary, which reads like `Post 1 photo with caption to instagram.com as @harborsupply.
 * Readback confirmed it.` from a real node or `Posted 1 photo to Instagram as harbor` from older
 * data. Nothing here renders: the screens do.
 */
import type { Effect, RunStatus, RunStep, TraceEvent } from '@/api/types';
import { siteOf } from '@/lib/sites';

/** The four results a person reads, plus Waiting for you while a run waits on a decision. */
export type Result = 'checked' | 'unchecked' | 'failed' | 'working' | 'waiting';

export interface Story {
  /** "Posted a photo on Harbor Supply's Instagram". */
  sentence: string;
  /** The same, for after a name: "posted a photo on Harbor Supply's Instagram". */
  predicate: string;
  /** While it runs: "posting a photo on Harbor Supply's Instagram". */
  doing: string;
  /** "Harbor Supply's Instagram". */
  place: string;
  /** The site's name: "Instagram", "blog". */
  site: string;
  /** The site on its own in a sentence: "Instagram", "the blog". */
  named: string;
  /** The site key for its mark, or the lane id when the site is not one we know. */
  siteKey: string;
  workspace: string;
  business: string;
  result: Result;
  /** The word on the pill: "Live, checked", "Checked", "Not checked", "Did not work", "Partly done". */
  label: string;
  /** One line under the sentence, said only when it matters: what did not happen, and why. */
  note: string | null;
  /** The verdict in the detail, bold: "It is live, and we checked." */
  verdict: string;
  /** The verdict's reason, a full sentence. */
  because: string;
  /** Looking only: these collapse into one line a day per account. */
  routineRead: boolean;
  /** Something a person can fix on the account itself (signed out, a robot check). */
  accountProblem: boolean;
  /** What the node said, for Technical details. */
  detail: string | null;
}

interface Verb {
  base: string;
  past: string;
  ing: string;
}

const VERBS: Verb[] = [
  { base: 'post', past: 'Posted', ing: 'posting' },
  { base: 'publish', past: 'Published', ing: 'publishing' },
  { base: 'check', past: 'Checked', ing: 'checking' },
  { base: 'read', past: 'Read', ing: 'reading' },
  { base: 'reply', past: 'Replied', ing: 'replying' },
  { base: 'save', past: 'Saved', ing: 'saving' },
  { base: 'draft', past: 'Drafted', ing: 'drafting' },
  { base: 'send', past: 'Sent', ing: 'sending' },
  { base: 'pay', past: 'Paid', ing: 'paying' },
  { base: 'comment', past: 'Commented', ing: 'commenting' },
  { base: 'like', past: 'Liked', ing: 'liking' },
  { base: 'follow', past: 'Followed', ing: 'following' },
  { base: 'upload', past: 'Uploaded', ing: 'uploading' },
  { base: 'update', past: 'Updated', ing: 'updating' },
  { base: 'delete', past: 'Deleted', ing: 'deleting' },
  { base: 'schedule', past: 'Scheduled', ing: 'scheduling' },
  { base: 'share', past: 'Shared', ing: 'sharing' },
  { base: 'open', past: 'Opened', ing: 'opening' },
];

const BY_WORD = new Map<string, Verb>(
  VERBS.flatMap((verb) => [
    [verb.base, verb],
    [verb.past.toLowerCase(), verb],
    [verb.ing, verb],
  ]),
);

/** When the summary says nothing usable, the effect says what kind of thing happened. */
const BY_EFFECT: Partial<Record<Effect, { verb: Verb; object: string }>> = {
  commit: { verb: BY_WORD.get('post')!, object: '' },
  read: { verb: BY_WORD.get('check')!, object: '' },
  draft: { verb: BY_WORD.get('save')!, object: 'a draft' },
  pay: { verb: BY_WORD.get('pay')!, object: '' },
};

/** What a failure means did not happen, by what the run was for. */
const NOTHING: Partial<Record<string, string>> = {
  post: 'Nothing was posted.',
  publish: 'Nothing was published.',
  reply: 'No reply went out.',
  comment: 'No comment went out.',
  send: 'Nothing was sent.',
  save: 'Nothing was saved.',
  draft: 'No draft was saved.',
  pay: 'Nothing was paid.',
  delete: 'Nothing was deleted.',
  upload: 'Nothing was uploaded.',
  share: 'Nothing was shared.',
};

const NUMBERS = ['', 'a', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine'];

/** "1 photo with caption" gives "a photo", "3 comments" stays, "1 update" gives "an update". */
function objectWords(count: string | undefined, rest: string): string {
  const noun = rest
    .replace(/\s+with\s+(a\s+)?caption$/i, '')
    .replace(/\s+(to|on|in|at|from)$/i, '')
    .trim();
  if (!noun) return '';
  if (count === undefined) return noun;
  const many = Number(count);
  if (many === 1) return `${/^[aeiou]/i.test(noun) ? 'an' : 'a'} ${noun}`;
  return `${many} ${noun}`;
}

interface Parsed {
  verb: Verb;
  object: string;
  /** Whatever the node said after the action: "Readback confirmed it.", "Failed: ..." */
  tail: string;
}

/**
 * Takes a node's summary apart. Real nodes: `Check medium.com as @astercoffee. Readback confirmed
 * it.`, `Post 1 link to reddit.com as meridian. Failed: the page changed.` Older data: `Posted 1
 * photo to Instagram as aster`, `Read 24 new comments on Instagram`, `Failed: <the rest>`.
 */
export function parseSummary(raw: string): Parsed | null {
  let text = raw.trim();
  let failedPrefix = false;
  if (/^failed:\s*/i.test(text)) {
    failedPrefix = true;
    text = text.replace(/^failed:\s*/i, '');
  }
  // The sentence is everything up to the first full stop that ends a word, then the tail.
  const split = /^(.*?[^.\s])\.(?:\s+(.*))?$/.exec(text);
  const head = (split?.[1] ?? text).trim();
  const tail = (failedPrefix ? 'Failed. ' : '') + (split?.[2] ?? '').trim();
  const words = head.split(/\s+/);
  const verb = BY_WORD.get((words[0] ?? '').toLowerCase());
  if (!verb) return null;
  // Drop who it was done as, and where: "as @astercoffee", "to instagram.com", "on Instagram".
  let rest = words.slice(1).join(' ');
  rest = rest.replace(/\s+as\s+@?[\w.-]+$/i, '');
  rest = rest.replace(/^(?:the\s+)?[\w-]+(?:\.[\w-]+)+$/i, '');
  rest = rest.replace(/\s+(?:to|on|in|at)\s+(?:[\w-]+\/[\w-]+|[\w-]+(?:\.[\w-]+)+|[A-Z][\w-]*(?:\s[A-Z][\w-]*)?)$/, '');
  const counted = /^(\d+)\s+(.*)$/.exec(rest);
  let object = counted ? objectWords(counted[1], counted[2] ?? '') : objectWords(undefined, rest);
  if (verb.base === 'reply' && object && !/^to\b/.test(object)) object = `to ${object}`;
  if (NUMBERS.includes(object)) object = '';
  return { verb, object, tail };
}

export const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
export const lower = (text: string) => text.charAt(0).toLowerCase() + text.slice(1);

const possessive = (name: string) => (/s$/i.test(name) ? `${name}'` : `${name}'s`);

/** Our own kinds of account read in lower case in a sentence, as a person says them. */
export const OWN_WORDS: Record<string, string> = { blog: 'blog', mail: 'inbox', shell: 'terminal', terminal: 'terminal' };

/** Where: "Harbor Supply's Instagram", "your personal LinkedIn", "Aster Coffee's blog". */
export function placeOf(lane: string, businessName: (workspace: string) => string | undefined) {
  const [workspace = '', name = ''] = lane.split('/');
  // An agent's environment (env/<id>, env/<id>-browser) is its own computer, not someone's account.
  if (workspace === 'env') {
    const title = businessName(lane);
    const place = title ?? 'its environment';
    return { workspace, business: '', site: place, named: place, siteKey: 'environment', place };
  }
  const known = siteOf(lane);
  const site = known ? (OWN_WORDS[known.key] ?? known.name) : name ? name.replaceAll('-', ' ') : 'the site';
  const business = businessName(workspace) ?? (workspace ? workspace.charAt(0).toUpperCase() + workspace.slice(1) : '');
  const place = !business ? site : business.toLowerCase() === 'personal' ? `your personal ${site}` : `${possessive(business)} ${site}`;
  // How the site reads on its own in a sentence: "Instagram", "the blog".
  const named = known && OWN_WORDS[known.key] ? `the ${site}` : site;
  return { workspace, business, site, named, siteKey: known?.key ?? lane, place };
}

const SIGNAL_REASONS: Record<string, (site: string) => string> = {
  captcha: (site) => `${capital(site)} wanted a robot check, so the agent stopped.`,
  checkpoint: (site) => `${capital(site)} wanted to confirm it is really you, so the agent stopped.`,
  logged_out: (site) => `The account was signed out of ${site}, so the agent stopped.`,
  blocked: (site) => `${capital(site)} is refusing the account for now.`,
  rate_limited: (site) => `${capital(site)} asked the account to slow down. It carries on by itself.`,
};

interface Reason {
  /** Plain words, a full sentence. */
  text: string;
  /** A person can fix it on the account. */
  account: boolean;
}

/** Why it did not work, from what the node said. Null when it said nothing a person can use. */
export function reasonOf(tail: string, site: string): Reason | null {
  const said = tail.replace(/^failed[.:]?\s*/i, '').trim();
  const signal = /lane signal is (\w+)/i.exec(said)?.[1]?.toLowerCase();
  const fromSignal = signal ? SIGNAL_REASONS[signal] : undefined;
  if (fromSignal) return { text: fromSignal(site), account: signal !== 'rate_limited' };
  if (/quarantined/i.test(said)) return { text: 'The account is switched off for safety.', account: true };
  if (/wrong.account|signed in as someone else/i.test(said)) return { text: 'The account is signed in as someone else, so the agent stopped.', account: true };
  if (/page changed|layout|selector/i.test(said)) return { text: `${capital(site)} looked different from usual, so the agent stopped rather than guess.`, account: false };
  if (/no decision|expired/i.test(said)) return { text: 'Nobody answered in time, so nothing was done.', account: false };
  if (/denied|said no/i.test(said)) return { text: 'Someone said no, so nothing was done.', account: false };
  if (/offline|not connected|unreachable/i.test(said)) return { text: 'The computer it runs on was not connected.', account: false };
  if (/text went out.*media did not/i.test(said)) return { text: 'The text went out, but the photo or video did not.', account: false };
  if (/^cancel+ed\.?$/i.test(said)) return { text: 'It was stopped before it finished.', account: false };
  return null;
}

type Source = Pick<TraceEvent, 'lane' | 'effect' | 'status' | 'summary'>;

const RESULT_OF: Record<RunStatus, Result> = {
  succeeded_verified: 'checked',
  succeeded_unverified: 'unchecked',
  partial: 'failed',
  failed: 'failed',
  cancelled: 'failed',
  running: 'working',
  queued: 'working',
  waiting_approval: 'waiting',
};


/** A summary with the engine's tail and suffix taken off, for when nothing parses. */
function tidySummary(summary: string): string {
  return summary
    .replace(/^failed:\s*/i, '')
    .replace(/\.\s.*$/, '')
    .replace(/\s+as\s+@?[\w.-]+\.?$/i, '')
    .replace(/\.$/, '')
    .trim();
}

export function storyOf(source: Source, businessName: (workspace: string) => string | undefined = () => undefined): Story {
  const where = placeOf(source.lane, businessName);
  const parsed = parseSummary(source.summary);
  const fallback = BY_EFFECT[source.effect];
  const verb = parsed?.verb ?? fallback?.verb;
  const object = parsed ? parsed.object : (fallback?.object ?? '');
  const tail = parsed?.tail ?? source.summary;
  // In an agent's environment there is no account to check, so what ran is done, not "not checked".
  const inEnvironment = where.siteKey === 'environment' && source.status === 'succeeded_unverified';
  const result = inEnvironment ? 'checked' : RESULT_OF[source.status];
  const reason = reasonOf(tail, where.named);
  const notTried = /^not run\b/i.test(source.summary) || (source.status === 'cancelled' && /no decision|expired/i.test(source.summary));
  const routineRead = source.effect === 'read' && (result === 'checked' || result === 'unchecked');

  const onPlace = (words: string) => (verb?.base === 'check' && !object ? `${words} ${where.place}` : `${words} on ${where.place}`);
  const what = object ? ` ${object}` : '';
  let sentence: string;
  let doing: string;
  if (verb) {
    doing = onPlace(`${verb.ing}${what}`);
    if (source.status === 'failed') sentence = onPlace(`Could not ${verb.base}${what}`);
    else if (source.status === 'cancelled') sentence = onPlace(`Did not ${verb.base}${what}`);
    else if (result === 'waiting') sentence = onPlace(`Wants to ${verb.base}${what}`);
    else if (result === 'working') sentence = capital(doing);
    else sentence = onPlace(`${verb.past}${what}`);
  } else {
    const plain = tidySummary(source.summary) || 'Did something';
    const mentions = plain.toLowerCase().includes(where.site.toLowerCase());
    sentence = mentions ? capital(plain) : `${capital(plain)} on ${where.place}`;
    doing = lower(sentence);
  }

  const nothing = NOTHING[verb?.base ?? ''] ?? (source.effect === 'commit' ? 'Nothing was posted.' : null);
  const site = where.named;
  const isPublic = source.effect === 'commit';
  let label: string;
  let note: string | null = null;
  let verdict: string;
  let because: string;
  switch (source.status) {
    case 'succeeded_verified':
      label = isPublic ? 'Live, checked' : 'Checked';
      verdict = isPublic ? 'It is live, and we checked.' : 'Done, and we checked.';
      because = isPublic
        ? `Afterwards, agentos opened ${site} as a visitor and found it. This is what it saw.`
        : `agentos looked at ${site} afterwards and saw it was done.`;
      break;
    case 'succeeded_unverified':
      if (inEnvironment) {
        label = 'Done';
        verdict = 'Done.';
        because = "It ran on the agent's own computer, so there is no account to check.";
        break;
      }
      label = 'Not checked';
      note = `It ran, but we could not see it on ${site} afterwards.`;
      verdict = 'Done, but not checked.';
      because = `It ran, but agentos could not see it on ${site} afterwards. It may still be fine: open the account to look.`;
      break;
    case 'partial':
      label = 'Partly done';
      note = reason?.text ?? 'Only part of it went out.';
      verdict = 'Only part of it went out.';
      because = reason?.text ?? 'Some steps finished and others did not. Look at the account before trying again.';
      break;
    case 'failed':
      label = 'Did not work';
      note = [nothing, reason?.text ?? `Something went wrong on ${site}, so the agent stopped.`].filter(Boolean).join(' ');
      verdict = 'It did not work.';
      because = note;
      break;
    case 'cancelled': {
      label = 'Stopped';
      const why = reason?.text ?? 'It was stopped before it finished.';
      note = notTried && nothing ? why.replace('nothing was done.', lower(nothing)) : [nothing, why].filter(Boolean).join(' ');
      verdict = notTried ? 'It was never tried.' : 'It was stopped.';
      because = note;
      break;
    }
    case 'waiting_approval':
      label = 'Waiting for you';
      verdict = 'It waits for a yes.';
      because = 'Nothing happens until someone decides in Needs you.';
      break;
    default:
      label = 'Working now';
      verdict = 'Working on it now.';
      because = 'An agent is on it right now. You can watch it live.';
  }

  return {
    sentence,
    predicate: lower(sentence),
    doing,
    place: where.place,
    site: where.site,
    named: where.named,
    siteKey: where.siteKey,
    workspace: where.workspace,
    business: where.business,
    result,
    label,
    note,
    verdict,
    because,
    routineRead,
    accountProblem: Boolean(reason?.account),
    detail: tail.replace(/^failed[.:]?\s*/i, '') || null,
  };
}

/** A step of a run in words, as a person would tick it off: "Opened Instagram as Harbor Supply". */
export function stepWords(step: Pick<RunStep, 'tool' | 'summary' | 'status'>, story: Pick<Story, 'named' | 'business' | 'result'>, last: boolean): string {
  const tool = step.tool.toLowerCase().replace(/[.:]/g, '_');
  const site = story.named;
  const as = story.business && story.business.toLowerCase() !== 'personal' ? ` as ${story.business}` : '';
  if (/open_lane|lane_open|browser_open|^open$/.test(tool)) return `Opened ${site}${as}`;
  if (/readback|verify|proof/.test(tool)) {
    if (story.result === 'checked') return 'Looked it up from outside and found it';
    return last && story.result === 'failed' ? 'Looked it up from outside' : 'Looked it up from outside, and could not find it yet';
  }
  if (/compose|fill|type|write/.test(tool)) return 'Wrote the post';
  if (/publish|submit|^post$|share/.test(tool)) return 'Shared it';
  if (/read_page|snapshot|^read$/.test(tool)) return 'Read the page';
  if (/navigate|goto/.test(tool)) return 'Went to the page';
  if (/act|click/.test(tool)) return 'Clicked and typed on the page';
  if (/upload|media|photo/.test(tool)) return 'Added the photo';
  // Older data already says it in words.
  if (/\s/.test(step.summary)) return capital(step.summary.replace(/\.$/, ''));
  return capital(step.summary.replaceAll('_', ' '));
}

/**
 * A routine as the subject of a sentence: "The Instagram post routine", "The weekly posts
 * routine". A site's name keeps its capital.
 */
export function routineSubject(name: string): string {
  const first = name.split(' ')[0] ?? '';
  return `The ${siteOf(first) ? name : lower(name)}`;
}
