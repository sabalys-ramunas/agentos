/**
 * What kind of thing an approval is, beyond posts: an email, a payment, a form, an order or
 * refund, a reply in a thread, files, things to delete, a command. Each kind has its own question,
 * buttons, plain facts and list name, so every job reads as designed, not as a generic fallback.
 *
 * The cloud says the kind (`preview.kind`, with `preview.data`); an older cloud does not, and then
 * the kind is read from the effect and the site.
 */
import type { Approval, PreviewData, PreviewKind } from '@/api/types';
import { siteOf } from '@/lib/sites';

/** Effects that change the computer or an account's setup, rather than post, mail or pay. */
const SETUP_EFFECTS: ReadonlySet<string> = new Set(['install_overlay', 'install_base', 'service', 'secret']);

const MAIL_SITES = new Set(['gmail', 'outlook', 'mail']);
const SOCIAL_SITES = new Set(['instagram', 'facebook', 'linkedin', 'reddit', 'x', 'threads', 'tiktok', 'youtube', 'pinterest', 'bluesky', 'medium', 'blog', 'substack', 'wordpress']);

export function kindOf(approval: Approval): PreviewKind {
  if (approval.preview.kind) return approval.preview.kind;
  const data = approval.preview.data;
  const site = approval.site?.key ?? siteOf(approval.lane)?.key ?? null;
  if (data?.subject !== undefined || data?.to) return 'email';
  if (data?.thread) return 'message';
  if (data?.fields) return 'form';
  if (data?.payee) return 'payment';
  if (data?.order) return 'order';
  if (data?.command && !data.items) return 'command';
  if (approval.effect === 'irreversible' && (data?.items || /^(delete|remove)\b/i.test(approval.summary))) return 'delete';
  if (approval.effect === 'exec') return 'command';
  // A change to the computer or an account's sign-in (a template, an install, a saved login): never a post.
  if (SETUP_EFFECTS.has(approval.effect)) return 'setup';
  if (approval.effect === 'pay') return 'payment';
  if (site && MAIL_SITES.has(site)) return 'email';
  if (approval.effect === 'commit' && (site === null || SOCIAL_SITES.has(site))) return 'post';
  return approval.effect === 'commit' ? 'form' : 'post';
}

/** The kinds this file words itself. Posts and the older effects keep the post wording. */
export const WORDED: ReadonlySet<PreviewKind> = new Set(['email', 'form', 'order', 'message', 'delete', 'command', 'files', 'setup', 'push', 'vm', 'lane']);

/** "€412.50", "$40", "€24": the cloud's amount in its currency, without a needless ".00". */
export function money(amount: number | undefined, currency = 'EUR'): string | null {
  if (amount === undefined || Number.isNaN(amount)) return null;
  const whole = Number.isInteger(amount);
  try {
    return new Intl.NumberFormat('en', { style: 'currency', currency, minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: 2 }).format(amount);
  } catch {
    return `${amount.toFixed(whole ? 0 : 2)} ${currency}`;
  }
}

/** "84 KB", "1.2 MB": a file size a person can read. */
export function size(bytes: number | null | undefined): string | null {
  if (!bytes && bytes !== 0) return null;
  if (bytes < 1_000) return `${bytes} B`;
  if (bytes < 1_000_000) return `${Math.round(bytes / 1_000)} KB`;
  if (bytes < 1_000_000_000) return `${(bytes / 1_000_000).toFixed(bytes < 10_000_000 ? 1 : 0)} MB`;
  return `${(bytes / 1_000_000_000).toFixed(1)} GB`;
}

const data = (approval: Approval): PreviewData => approval.preview.data ?? {};

/** The last part of a path: "/home/me/Downloads/old.dmg" gives "old.dmg". */
export const baseName = (path: string) => path.split('/').filter(Boolean).pop() ?? path;

const people = (list: string[] | undefined) => {
  const [first, ...rest] = list ?? [];
  if (!first) return 'someone';
  return rest.length ? `${first} and ${rest.length} more` : first;
};

/** The first name of a person, for a friendly sentence: "Jonas Petraitis" gives "Jonas Petraitis". */
const who = (name: string | undefined) => name ?? 'the customer';

export interface KindWords {
  question: string;
  yes: string;
  no: string;
  noMeans: string;
  doing: string;
  notDoing: string;
  /** A short name for the row in the list. */
  row: string;
  audience: { title: string; detail: string | null };
  undo: { title: string; detail: string | null };
}

/** The words for one of the kinds in `WORDED`, or null for a post (the post wording applies). */
export function kindWords(approval: Approval, place: string | null, siteLabel: string | null): KindWords | null {
  const kind = kindOf(approval);
  if (!WORDED.has(kind)) return null;
  const d = data(approval);
  const on = place ? ` on ${place}` : '';
  const site = siteLabel ?? 'the site';
  switch (kind) {
    case 'email':
      return {
        question: `Send this email to ${people(d.to)}?`,
        yes: 'Send email',
        no: "Don't send",
        noMeans: 'Nothing is sent.',
        doing: `Sending the email to ${people(d.to)}`,
        notDoing: 'Not sending the email',
        row: d.subject ? `Email: ${d.subject}` : 'Email to send',
        audience: { title: `Goes to ${people(d.to)}`, detail: d.from ? `From ${d.from}${place ? `, on ${place}` : ''}` : place },
        undo: { title: "An email can't be called back once sent", detail: 'You can send a correction after' },
      };
    case 'form': {
      const total = money(d.amount, d.currency);
      const final = approval.effect === 'irreversible';
      const name = d.form ?? 'the form';
      return {
        question: d.ask ?? (total ? `Submit "${name}" for ${total}${on}?` : `Submit "${name}"${on}?`),
        yes: d.yes ?? (total ? `Submit and pay ${total}` : 'Submit'),
        no: d.no ?? "Don't submit",
        noMeans: 'Nothing is submitted.',
        doing: `Submitting "${name}"`,
        notDoing: 'Not submitting',
        row: d.form ? `Form: ${d.form}` : `Form on ${site}`,
        audience: { title: `Sent to ${site}`, detail: place ? `As ${place}` : null },
        undo: final
          ? { title: "Once submitted, it can't be changed", detail: 'Read every field before you say yes' }
          : { title: 'You can change it again later', detail: `On ${site}, or ask the agent` },
      };
    }
    case 'order': {
      const amount = money(d.amount, d.currency);
      return {
        question: amount ? `Refund ${amount} to ${who(d.customer)}?` : `Refund ${who(d.customer)}?`,
        yes: amount ? `Refund ${amount}` : 'Refund',
        no: "Don't refund",
        noMeans: 'Nothing is refunded.',
        doing: `Refunding ${who(d.customer)}`,
        notDoing: 'Not refunding',
        row: amount ? `Refund ${amount}, order ${d.order ?? ''}`.trim() : 'Refund',
        audience: { title: `${amount ?? 'The money'} goes back to ${who(d.customer)}`, detail: d.order ? `Order ${d.order}${on}` : place },
        undo: { title: "A refund can't be taken back here", detail: 'The money leaves the shop at once' },
      };
    }
    case 'message': {
      const to = d.thread?.[0]?.from;
      return {
        question: to ? `Send this reply to ${to}?` : `Send this reply${on}?`,
        yes: 'Send reply',
        no: "Don't send",
        noMeans: 'Nothing is sent.',
        doing: to ? `Replying to ${to}` : 'Sending the reply',
        notDoing: 'Not replying',
        row: to ? `Reply to ${to}` : `Reply on ${site}`,
        audience: { title: `${to ?? 'They'} and anyone reading ${site}`, detail: place ? `Public, on ${place}` : 'It goes public' },
        undo: { title: 'You can delete the reply later', detail: `On ${site}, or ask the agent` },
      };
    }
    case 'delete': {
      const items = d.items ?? [];
      const count = items.length;
      // Without a list, the request's own words ("Delete 3 draft pages") say it better.
      if (count === 0) return null;
      const noun = count === 1 ? 'file' : 'files';
      const where = d.computer ? ` on ${d.computer}` : on;
      return {
        question: count ? `Delete ${count} ${noun}${where}?` : `Delete these${where}?`,
        yes: count ? `Delete ${count} ${noun}` : 'Delete',
        no: 'Keep them',
        noMeans: 'Nothing is deleted.',
        doing: count ? `Deleting ${count} ${noun}` : 'Deleting',
        notDoing: 'Keeping them',
        row: count ? `Delete ${count} ${noun}` : 'Delete',
        audience: { title: 'Gone for good', detail: d.computer ? `From ${d.computer}` : null },
        undo: { title: "These can't be brought back", detail: 'Only what is listed is deleted' },
      };
    }
    case 'command':
      return {
        question: `Run this command on ${d.computer ?? 'the computer'}?`,
        yes: 'Run it',
        no: "Don't run",
        noMeans: 'Nothing runs.',
        doing: `Running it on ${d.computer ?? 'the computer'}`,
        notDoing: 'Not running it',
        row: approval.summary.length < 48 ? approval.summary : 'Command to run',
        audience: { title: 'Only your computer', detail: 'Nothing goes public' },
        undo: { title: 'A technical person can undo it', detail: 'Ask before you say yes if unsure' },
      };
    case 'setup':
      // Setup changes come with their own summary; the words say what is asked, never the raw content.
      return {
        question: `Allow this change${on}?`,
        yes: 'Allow it',
        no: "Don't allow",
        noMeans: 'Nothing is changed.',
        doing: 'Making the change',
        notDoing: 'Not making the change',
        row: approval.summary.length > 0 && approval.summary.length < 48 ? approval.summary : 'A change to set up',
        audience: { title: 'A change to the computer or an account', detail: place },
        undo: { title: 'Check the summary before you say yes', detail: null },
      };
    case 'files': {
      const count = d.items?.length ?? 0;
      return {
        question: `Send ${count || 'these'} file${count === 1 ? '' : 's'} to ${d.destination ?? place ?? 'the site'}?`,
        yes: count ? `Send ${count} file${count === 1 ? '' : 's'}` : 'Send them',
        no: "Don't send",
        noMeans: 'Nothing is sent.',
        doing: 'Sending the files',
        notDoing: 'Not sending the files',
        row: count ? `${count} file${count === 1 ? '' : 's'} to send` : 'Files to send',
        audience: { title: `Goes to ${d.destination ?? place ?? 'the site'}`, detail: null },
        undo: { title: 'You can delete them there later', detail: null },
      };
    }
    case 'push': {
      const refs = d.push_refs ?? [];
      const [first] = refs;
      const branch = first?.name ?? 'a branch';
      const commits = refs.reduce((sum, ref) => sum + (ref.commits ?? 0), 0);
      const force = refs.some((ref) => ref.force);
      const repo = d.remote ? ` in ${repoName(d.remote)}` : '';
      const deleting = first?.kind === 'delete';
      return {
        question: deleting
          ? `Delete the branch ${branch}${repo}?`
          : `${force ? 'Force-push' : 'Push'} ${commits ? `${commits} commit${commits === 1 ? '' : 's'}` : 'changes'} to ${branch}${repo}?`,
        yes: 'Approve the push',
        no: "Don't push",
        noMeans: 'Nothing is pushed.',
        doing: `Pushing to ${branch}`,
        notDoing: 'Not pushing',
        row: refs.length > 1 ? `Push ${refs.length} branches${repo}` : `Push to ${branch}${repo}`,
        audience: {
          title: `Lands on ${d.remote ?? 'the remote'}`,
          detail: refs.some((ref) => ref.protected) ? `${refs.filter((ref) => ref.protected).map((ref) => ref.name).join(', ')} is a protected branch` : null,
        },
        undo: force
          ? { title: "A force-push can't be taken back", detail: 'Commits on the remote that are not in this push are lost' }
          : { title: 'A new commit can undo it', detail: 'The history stays' },
      };
    }
    case 'vm': {
      const cost = money(d.max_cost_eur ?? undefined, 'EUR');
      const minutes = d.ttl_minutes;
      const limit = minutes ? durationWords(minutes) : null;
      return {
        question: `Rent a ${d.server_type ?? 'cloud'} machine${limit ? ` for up to ${limit}` : ''}?`,
        yes: 'Rent it',
        no: "Don't rent",
        noMeans: 'Nothing is rented and nothing is paid.',
        doing: 'Renting the machine',
        notDoing: 'Not renting',
        row: cost ? `Rent a VM, ${cost} at most` : 'Rent a VM',
        audience: { title: 'Only your agents use it', detail: d.computer ? `As the lane ${d.computer}` : null },
        undo: limit
          ? { title: `It deletes itself after ${limit}`, detail: 'Even if the agent forgets' }
          : { title: 'It is deleted when the agent is done', detail: null },
      };
    }
    case 'lane': {
      const lane = d.computer ?? approval.lane;
      const fresh = Boolean(d.lane_preset);
      return {
        question: fresh ? `Make the lane ${lane} and run its first command?` : `Run this in ${lane}?`,
        yes: fresh ? 'Make the lane' : 'Run it',
        no: "Don't run",
        noMeans: 'Nothing runs.',
        doing: fresh ? 'Making the lane' : `Running it in ${lane}`,
        notDoing: 'Not running it',
        row: fresh ? `New lane ${lane}` : `Run in ${lane}`,
        audience: { title: 'Only the lane', detail: d.fields?.find((field) => field.label === 'Network')?.value ? `Network: ${d.fields.find((field) => field.label === 'Network')!.value}` : null },
        undo: { title: d.timeout_s ? `Stopped after ${durationWords(Math.round(d.timeout_s / 60))} at most` : 'You can stop it from its run', detail: null },
      };
    }
    default:
      return null;
  }
}

/** "github.com/harbor-supply/shop" gives "harbor-supply/shop": the repository as people say it. */
export function repoName(remote: string): string {
  const parts = remote.replace(/\.git$/, '').split(/[/:]/).filter(Boolean);
  return parts.length >= 2 ? parts.slice(-2).join('/') : remote;
}

/** "90 min", "2 h", "1 h 30 min". */
export function durationWords(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

/** The payment wording, when the cloud sends a payment's own data. */
export function paymentWords(approval: Approval): { amount: string | null; payee: string | null } {
  const d = data(approval);
  return { amount: money(d.amount, d.currency), payee: d.payee ?? null };
}

/** The list a kind sits in on Needs you, heaviest first, with why it is there. */
export interface Stake {
  key: string;
  name: string;
  why: string;
}

const STAKES: Record<string, Stake> = {
  money: { key: 'money', name: 'Costs money', why: 'These never happen on their own.' },
  final: { key: 'final', name: "Can't be undone", why: 'There is no way back from these.' },
  email: { key: 'email', name: 'Emails to send', why: 'Read it as they will.' },
  message: { key: 'message', name: 'Replies to send', why: 'Your customers will read these.' },
  form: { key: 'form', name: 'Forms to submit', why: 'The changes are marked.' },
  post: { key: 'post', name: 'Posts to check', why: 'Read it as your customers will see it.' },
  files: { key: 'files', name: 'Files to send', why: 'Exactly these files, nothing else.' },
  secret: { key: 'secret', name: 'Saved logins', why: 'The agent never sees the password.' },
  draft: { key: 'draft', name: 'Drafts', why: 'Nothing goes public yet.' },
  read: { key: 'read', name: 'Looking', why: 'It only looks.' },
  brain: { key: 'brain', name: 'AI usage', why: 'It uses AI credit.' },
  push: { key: 'push', name: 'Pushes', why: 'Code that lands on a shared branch.' },
  command: { key: 'command', name: 'Commands to run', why: 'On the computer itself.' },
  tech: { key: 'tech', name: 'For a technical person', why: 'Changes on the computer itself.' },
};

export const STAKE_ORDER = ['money', 'final', 'push', 'email', 'message', 'form', 'post', 'files', 'secret', 'draft', 'read', 'brain', 'command', 'tech'];

export function stakeOf(approval: Approval): Stake {
  const kind = kindOf(approval);
  const pick = (key: string) => STAKES[key] as Stake;
  if (approval.effect === 'pay' || kind === 'vm') return pick('money');
  if (approval.effect === 'irreversible') return pick('final');
  if (kind === 'push') return pick('push');
  if (approval.effect === 'commit') {
    if (kind === 'email' || kind === 'message' || kind === 'form' || kind === 'files') return pick(kind);
    return pick('post');
  }
  if (approval.effect === 'secret' || approval.effect === 'draft' || approval.effect === 'read' || approval.effect === 'brain') return pick(approval.effect);
  if (kind === 'command' || kind === 'lane') return pick('command');
  return pick('tech');
}

/** Routine, low-risk kinds that may be said yes to together. Never money, the irreversible or email. */
export const batchKind = (approval: Approval) => approval.effect === 'commit' && (kindOf(approval) === 'post' || kindOf(approval) === 'message');
