/**
 * An approval in the person's words: the question it asks, the buttons, the four plain facts,
 * how big it is, and what to say when its account cannot act. Pure, so every screen that shows
 * an approval (Needs you, the one-by-one view, a Review link) reads the same sentences.
 *
 * Nothing here says "approve", "effect", "commit" or "lane": those stay behind Technical details.
 */
import type { Approval, ApprovalLaneState, Effect, HealthState, Lane, Outcome } from '@/api/types';
import type { PrincipalLike } from '@/lib/principal';
import { actionVerbs } from '@/lib/vocabulary';
import { dayAndTime } from '@/lib/format';
import { laneName } from '@/lib/labels';
import { siteOf } from '@/lib/sites';
import { kindOf, kindWords, paymentWords } from './kinds';

/** Sites whose posts are articles rather than updates, so the words say "publish" and "story". */
const ARTICLE_SITES = new Set(['medium', 'blog', 'substack', 'wordpress']);

export interface Where {
  /** The business's own name: "Aster Coffee". */
  business: string | null;
  /** The site key, when known: "instagram". */
  site: string | null;
  /** The site's name: "Instagram", or the account's own name for a site we do not know. */
  siteLabel: string | null;
  /** "Aster Coffee's Instagram", ready for a sentence, or null without an account. */
  place: string | null;
  /** The handle it posts as, when the request names one: "@astercoffee". */
  handle: string | null;
  /** The site's address: "instagram.com". */
  domain: string | null;
}

/** Where the approval lands. The cloud's own `place` wins; a business name from the panel fills gaps. */
export function whereOf(approval: Approval, businessName?: string | null): Where {
  const [ws = '', name = ''] = approval.lane.split('/');
  const known = approval.site?.key ? siteOf(approval.site.key) : name ? siteOf(name) : null;
  const site = approval.site?.key ?? known?.key ?? null;
  const siteLabel = approval.site?.label ?? known?.name ?? (name ? laneName(name) : null);
  // The cloud's name is the business's own; the old seeded slug ("Aster") reads worse than the panel's.
  const business = businessName && businessName !== ws ? businessName : (approval.workspace_name ?? businessName ?? (ws || null));
  const place = business && siteLabel ? `${business}'s ${siteLabel}` : (approval.place ?? null);
  const handle = /\bas (@[\w.]+)/.exec(approval.summary)?.[1] ?? null;
  const domain = approval.site?.domain ?? known?.domains[0] ?? null;
  return { business, site, siteLabel, place, handle, domain };
}

export const isArticle = (where: Pick<Where, 'site'>) => where.site !== null && ARTICLE_SITES.has(where.site);

/** "$40.00" reads "$40"; the first amount in the request is the one it spends. */
export function amountOf(summary: string): string | null {
  const match = /\$\s?(\d[\d,]*(?:\.\d{1,2})?)/.exec(summary);
  if (!match?.[1]) return null;
  return `$${match[1].replace(/\.0+$/, '')}`;
}

/** "Delete 3 draft pages from the blog" gives 3 and "draft pages". */
export function deletionOf(summary: string): { count: number; noun: string } | null {
  const match = /^(?:delete|remove)\s+(\d+)\s+(.+?)(?:\s+(?:from|on|in)\s+.+)?$/i.exec(summary.trim());
  if (!match?.[1] || !match[2]) return null;
  return { count: Number(match[1]), noun: match[2] };
}

const photosOf = (summary: string) => Number(/(\d+)\s+photos?\b/i.exec(summary)?.[1] ?? 0);

const on = (where: Where) => (where.place ? ` on ${where.place}` : '');

/** The one question the item asks, as the person would ask it. */
export function questionOf(approval: Approval, where: Where, who: string): string {
  const { summary, effect, preview } = approval;
  const kind = kindWords(approval, where.place, where.siteLabel);
  if (kind) return kind.question;
  const pay = paymentWords(approval);
  if (pay.amount && pay.payee) return `Pay ${pay.amount} to ${pay.payee}?`;
  switch (effect) {
    case 'commit': {
      if (isArticle(where)) return `Publish this story${on(where)}?`;
      const photos = photosOf(summary);
      if (photos > 1) return `Post these ${photos} photos${on(where)}?`;
      if (preview.image || photos === 1) return `Post this photo${on(where)}?`;
      if (/\b(comment|repl)/i.test(summary)) return `Post this reply${on(where)}?`;
      return `Post this${on(where)}?`;
    }
    case 'pay': {
      const amount = amountOf(summary);
      if (!amount) return `Pay for this${on(where)}?`;
      if (/\bboost/i.test(summary)) return `Pay ${amount} to show a post to more people${on(where)}?`;
      return `Pay ${amount}${on(where)}?`;
    }
    case 'irreversible': {
      const gone = deletionOf(summary);
      return gone ? `Delete ${gone.count} ${gone.noun}${on(where)}?` : `Do this${on(where)}? It can't be undone.`;
    }
    case 'secret':
      return where.place ? `Sign in to ${where.place} with the saved login?` : 'Use a saved login?';
    case 'draft':
      return `Save a draft${on(where)}?`;
    case 'read':
      return `Let ${who} look at ${where.place ?? 'this'}?`;
    case 'exec':
      return `Let ${who} run a command on the computer?`;
    case 'install_overlay':
      return `Install something${on(where)}?`;
    case 'install_base':
      return 'Change the setup every account on this computer starts from?';
    case 'service':
      return `Start a service${on(where)}?`;
    case 'brain':
      return `Let ${who} use the AI model for this?`;
    case 'agent':
      return `Let ${who} work with another agent${on(where)}?`;
    case 'vm':
      return `Rent a cloud machine for ${who}?`;
  }
}

export interface Verbs {
  yes: string;
  no: string;
  /** What no means: "Nothing is posted." */
  noMeans: string;
}

/**
 * The buttons, named for the action. A yes on an account that cannot act says when it happens:
 * "Post it once fixed", "Post it once signed in". Never "later", which reads like "remind me".
 */
export function verbsOf(approval: Approval, where: Where, blocked: HealthState | null = null): Verbs {
  const kind = kindWords(approval, where.place, where.siteLabel);
  const gone = approval.effect === 'irreversible' ? deletionOf(approval.summary) : null;
  const amount = paymentWords(approval).amount ?? amountOf(approval.summary) ?? undefined;
  const base = actionVerbs(approval.effect, { amount, count: gone?.count, noun: gone?.noun.split(' ').pop() });
  const verbs = kind
    ? { yes: kind.yes, no: kind.no, noMeans: kind.noMeans }
    : approval.effect === 'commit' && isArticle(where)
      ? { yes: 'Publish it', no: "Don't publish", noMeans: 'Nothing is published.' }
      : base;
  if (!blocked) return verbs;
  return { ...verbs, yes: `${verbs.yes} ${blocked === 'logged_out' ? 'once signed in' : 'once fixed'}` };
}

/** Done words for after the tap, in the same verb: "Posting", "Paying". */
export function doingOf(approval: Approval, where: Where): { doing: string; notDoing: string } {
  const place = where.place ?? 'it';
  const kind = kindWords(approval, where.place, where.siteLabel);
  if (kind) return { doing: kind.doing, notDoing: kind.notDoing };
  const pay = paymentWords(approval);
  if (pay.amount && pay.payee) return { doing: `Paying ${pay.amount} to ${pay.payee}`, notDoing: 'Not paying' };
  switch (approval.effect) {
    case 'commit':
      return isArticle(where)
        ? { doing: `Publishing on ${place}`, notDoing: `Not publishing on ${place}` }
        : { doing: `Posting to ${place}`, notDoing: `Not posting to ${place}` };
    case 'pay':
      return { doing: `Paying from ${place}`, notDoing: 'Not paying' };
    case 'irreversible':
      return { doing: `Deleting on ${place}`, notDoing: 'Keeping them' };
    default:
      return { doing: where.place ? `Doing it on ${place}` : 'Doing it', notDoing: 'Not doing it' };
  }
}

/** "How big is this?": four steps a person can picture. */
export const SCALE = ['Private', 'Others see it', 'Costs money', "Can't undo"] as const;

export function scaleOf(effect: Effect): number {
  switch (effect) {
    case 'commit':
      return 1;
    case 'pay':
      return 2;
    case 'irreversible':
      return 3;
    default:
      return 0;
  }
}

export interface Fact {
  key: 'why' | 'asked' | 'audience' | 'undo' | 'deadline';
  title: string;
  detail: string | null;
  /** Nobody's switch changes this one: money, or what can't be undone. */
  locked?: boolean;
}

export interface WhyAsked {
  /** `safety`: it would have run on its own, and a check stopped it. */
  kind: 'safety' | 'chosen' | 'always';
  title: string;
  detail: string | null;
}

/**
 * Why this one asks, in plain words. The node's `reason` is its own wording ("it follows untrusted
 * text from a web page at x", the lethal trifecta's line, "Ask me first is on for routine y"), which
 * stays under Technical details. What it names (a host, a kind of page) is still only text.
 */
export function whyAsked(approval: Pick<Approval, 'reason' | 'effect'>): WhyAsked | null {
  const reason = approval.reason ?? '';
  const follows = /it follows untrusted text from ([^;]+)/i.exec(reason)?.[1]?.trim();
  const leak = /private data/i.test(reason);
  if (follows || leak) {
    const host = /sends data to ([^;]+)/i.exec(reason)?.[1]?.trim();
    const read = follows ? `It read text from ${follows}, which nobody here wrote and which could be steering it.` : 'It read private information and text from outside.';
    const sends = leak ? ` This would send data to ${host ?? 'a place'} it has not been allowed to reach.` : '';
    const usual = approval.effect === 'commit' ? 'This would normally run on its own. ' : '';
    return { kind: 'safety', title: 'A safety check stopped this', detail: `${usual}${read}${sends}` };
  }
  if (approval.effect === 'pay') return { kind: 'always', title: 'Money always asks', detail: 'Whatever any switch says.' };
  if (approval.effect === 'irreversible') return { kind: 'always', title: "This can't be undone, so it always asks", detail: 'Whatever any switch says.' };
  const chosen = /^ask me first is on(?: for (routine|account)?)?/i.exec(reason);
  if (chosen) {
    const of = chosen[1]?.toLowerCase();
    return { kind: 'chosen', title: 'Someone asked to see this first', detail: of ? `Ask me first is on for this ${of}.` : 'Ask me first is on.' };
  }
  if (/not act on one account|no one account/i.test(reason)) return { kind: 'always', title: 'It is not on one account, so it always asks', detail: null };
  return null;
}

/** "4 minutes ago", "2 hours ago", "yesterday": for a sentence, not a column. */
export function agoWords(iso: string, now = Date.now()): string {
  const minutes = Math.round((now - new Date(iso).getTime()) / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? 'yesterday' : `${days} days ago`;
}

/** "today at 14:00", "tomorrow at 09:00", or the date. */
export function untilWords(iso: string, now = Date.now()): string {
  const text = dayAndTime(iso, now);
  const match = /^(Today|Tomorrow) (.+)$/.exec(text);
  return match?.[1] && match[2] ? `${match[1].toLowerCase()} at ${match[2]}` : text;
}

/** A name that opens a sentence: "You" or "Claude Code", never "you". */
const sentenceStart = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/** The four plain rows: who asked, who will see it, can it be undone, until when. */
export function factsOf(approval: Approval, where: Where, who: string, now = Date.now()): Fact[] {
  const site = where.siteLabel ?? 'the site';
  const verbs = verbsOf(approval, where);
  const asked: Fact = {
    key: 'asked',
    title: `${sentenceStart(who)} asked ${agoWords(approval.created_at, now)}`,
    detail: approval.flow_target ? 'As part of a routine' : 'It waits for your answer',
  };
  const deadline: Fact = {
    key: 'deadline',
    title: `Waits for you until ${untilWords(approval.expires_at, now)}`,
    detail: `If nobody answers, it is dropped. ${verbs.noMeans}`,
  };
  const address = where.domain ? `${where.domain}${where.handle ? `/${where.handle.replace(/^@/, '')}` : ''}` : null;
  const kind = kindWords(approval, where.place, where.siteLabel);
  const pay = paymentWords(approval);
  const data = approval.preview.data ?? {};
  const [audience, undo]: [Omit<Fact, 'key'>, Omit<Fact, 'key'>] = (() => {
    if (kind) return [kind.audience, kind.undo];
    if (approval.effect === 'pay' && pay.amount && pay.payee) {
      return [
        { title: `${pay.amount} leaves ${data.from_account ?? 'the account'}`, detail: `To ${pay.payee}${data.reference ? `, reference ${data.reference}` : ''}` },
        { title: "Money sent can't be taken back here", detail: data.due ? `It is due ${data.due}` : 'Only the payee can refund it' },
      ];
    }
    switch (approval.effect) {
      case 'commit':
        return [
          { title: `Anyone on ${site} can see it`, detail: address ? `Public, on ${address}` : 'It goes public' },
          { title: 'You can delete it later', detail: `On ${site}, or ask ${who}` },
        ];
      case 'pay':
        return [
          { title: `${amountOf(approval.summary) ?? 'Money'} is spent`, detail: where.place ? `Paid from ${where.place}` : 'Paid with the card on the account' },
          { title: "Money spent can't be taken back here", detail: 'Only the site can refund it' },
        ];
      case 'irreversible':
        return [
          { title: 'Gone for everyone', detail: 'Nobody can open it afterwards' },
          { title: "This can't be undone", detail: 'Once it is gone, it is gone' },
        ];
      case 'draft':
        return [
          { title: 'Nobody else sees it yet', detail: 'It stays a draft until someone posts it' },
          { title: 'You can delete the draft later', detail: null },
        ];
      case 'read':
        return [
          { title: 'Nobody: it only looks', detail: 'Nothing changes on the site' },
          { title: 'Nothing to undo', detail: null },
        ];
      case 'secret':
        return [
          { title: 'Nobody sees the password', detail: `The computer fills it in. It never shows the password to ${who}` },
          { title: 'Nothing to undo', detail: 'It only signs in' },
        ];
      default:
        return [
          { title: 'Only your computer', detail: 'Nothing goes public' },
          { title: 'A technical person can undo it', detail: null },
        ];
    }
  })();
  const why = whyAsked(approval);
  const reasonRow: Fact[] = why && why.kind !== 'safety' ? [{ key: 'why', title: why.title, detail: why.detail, locked: why.kind === 'always' }] : [];
  return [...reasonRow, asked, { key: 'audience', ...audience }, { key: 'undo', ...undo }, deadline];
}

/** The account cannot act right now, so a yes only waits. Said before the buttons, with the fix. */
export interface Blocker {
  state: HealthState;
  sentence: string;
  fix: string;
}

const BLOCKED_AS: Partial<Record<HealthState, [string, string]>> = {
  // [what is wrong, the fix button]
  quarantined: ['is switched off for safety', 'See what is wrong'],
  wrong_account: ['is signed in as someone else', 'Fix the sign-in'],
  logged_out: ['is signed out', 'Sign in'],
  captcha: ['wants proof that a real person is there', 'Do the check'],
  checkpoint: ['wants to check it is really you', "Confirm it's you"],
  blocked: ['is being refused by the site for now', 'See what is wrong'],
  offline: ['runs on a computer that is not connected', 'See the computer'],
  missing: ['is no longer reported by its computer', 'See what is wrong'],
};

const WAITS_FOR: Partial<Record<HealthState, string>> = {
  logged_out: 'until someone signs in',
  captcha: 'until someone does the check',
  checkpoint: 'until someone confirms it',
  offline: 'until the computer is back',
};

export function blockerOf(approval: Approval, where: Where, laneState: ApprovalLaneState | null | undefined = approval.lane_state): Blocker | null {
  if (!laneState?.blocks) return null;
  const words = BLOCKED_AS[laneState.state];
  if (!words) {
    const sentence = laneState.warning ?? `${where.place ?? 'This account'} cannot act right now, so if you say yes, nothing happens until it can.`;
    return { state: laneState.state, sentence, fix: 'Open the account' };
  }
  const subject = where.place ?? 'This account';
  const kind = kindOf(approval);
  const action =
    approval.effect === 'pay'
      ? 'pay'
      : kind === 'email' || kind === 'message' || kind === 'files'
        ? 'send'
        : kind === 'form'
          ? 'be submitted'
          : approval.effect === 'commit'
            ? isArticle(where)
              ? 'publish'
              : 'post'
            : 'happen';
  const verb = action === 'happen' ? 'nothing happens' : `it will not ${action}`;
  const when = WAITS_FOR[laneState.state] ?? 'until it is fixed';
  return { state: laneState.state, sentence: `${subject} ${words[0]}, so if you say yes, ${verb} ${when}.`, fix: words[1] };
}

/** The real result, in one line: "Posted to Aster Coffee's Instagram", with why when it did not work. */
export function outcomeLine(outcome: Outcome): string {
  if ((outcome.state === 'failed' || outcome.state === 'partial') && outcome.reason) {
    const reason = outcome.reason.replace(/\.$/, '');
    return `${outcome.title}: ${reason.charAt(0).toLowerCase()}${reason.slice(1)}.`;
  }
  return outcome.title;
}

/** Short words for a chip or a list: "Posted", "Not posted", "Didn't work". */
export function outcomeWord(outcome: Outcome | null | undefined, effect: Effect): string {
  const post = effect === 'commit';
  switch (outcome?.state) {
    case 'done':
      return post ? 'Posted' : 'Done';
    case 'pending':
      return post ? 'Posting' : 'Under way';
    case 'not_run':
      return post ? 'Not posted' : 'Not done';
    case 'failed':
      return "Didn't work";
    case 'partial':
      return 'Partly done';
    case 'unknown':
      return 'Not sure yet';
    default:
      return 'Decided';
  }
}

/** A short name for a row in the list: "Instagram post", "Pay $40", "Delete 3 draft pages". */
export function rowTitleOf(approval: Approval, where: Where): string {
  const site = where.siteLabel ?? 'Website';
  const kind = kindWords(approval, where.place, where.siteLabel);
  if (kind) return kind.row;
  const pay = paymentWords(approval);
  if (pay.amount && pay.payee) return `Pay ${pay.amount} to ${pay.payee}`;
  switch (approval.effect) {
    case 'commit': {
      if (isArticle(where)) return `${site} story`;
      const photos = photosOf(approval.summary);
      return photos > 1 ? `${site} post, ${photos} photos` : `${site} post`;
    }
    case 'pay': {
      const amount = amountOf(approval.summary);
      return amount ? `Pay ${amount} on ${site}` : `Payment on ${site}`;
    }
    case 'irreversible': {
      const gone = deletionOf(approval.summary);
      return gone ? `Delete ${gone.count} ${gone.noun}` : `Something that can't be undone`;
    }
    case 'secret':
      return `Saved login for ${site}`;
    case 'draft':
      return `${site} draft`;
    case 'read':
      return `Look at ${site}`;
    default:
      return 'A change on the computer';
  }
}

/** An account that needs a person, as what to do: "Sign in to Solstice Studio's Facebook once". */
export interface Hand {
  title: string;
  /** The button. */
  action: string;
}

export function handOf(lane: Lane, business: string | null, nameOf: (principal: PrincipalLike) => string): Hand {
  const known = siteOf(lane.site?.key ?? lane.name);
  const site = lane.site?.label ?? known?.name ?? laneName(lane.name);
  const place = business ? `${business}'s ${site}` : site;
  if (lane.control.takeback_requested) {
    const who = lane.control.takeback_by ? nameOf(lane.control.takeback_by) : 'An agent';
    return { title: `${who} asks for ${place} back`, action: 'Hand it back' };
  }
  if (lane.missing_since) return { title: `${place} is no longer reported by its computer`, action: 'See why' };
  if (lane.account?.status === 'wrong_account') return { title: `${place} is signed in as someone else`, action: 'Fix the sign-in' };
  if (lane.state === 'quarantined') return { title: `${place} is switched off for safety`, action: 'See why' };
  if (lane.login_request) return { title: `Log in to ${place}`, action: 'Log in' };
  switch (lane.signal) {
    case 'logged_out':
      return { title: `Sign in to ${place} once`, action: 'Sign in' };
    case 'captcha':
      return { title: `Show ${site} a real person is there, on ${place}`, action: 'Do the check' };
    case 'checkpoint':
      return { title: `Confirm it's you on ${place}`, action: 'Confirm' };
    case 'blocked':
      return { title: `${site} is refusing ${place} for now`, action: 'See why' };
    default:
      return { title: `${place} needs a hand`, action: 'Open it' };
  }
}
