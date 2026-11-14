/**
 * "If yes": what happens if you approve, in one short line fitted to the kind of request (Mono, Needs
 * you). The queue, Home's Needs you and the open request all read it from here, so a request says the
 * same thing everywhere. Built only from the request's own preview data; a kind with no data says the
 * request's own summary.
 */
import type { Approval, BrandProposal } from '@/api/types';
import { BranchPill } from '@/components/ui/branch';
import type { AnyKind } from '@/components/ui/kind';
import { plural } from '@/lib/format';
import { durationWords, kindOf, money, paymentWords, repoName, size } from './kinds';
import { previewText } from './post-preview';
import type { Where } from './words';
import { amountOf } from './words';

/** The tile a request shows in the queue: a kind of work, or a neutral one with its own icon. */
export function tileOf(approval: Approval): AnyKind {
  switch (kindOf(approval)) {
    case 'push':
      return 'push';
    case 'vm':
      return 'vm';
    case 'lane':
    case 'command':
      return 'lane';
    case 'payment':
    case 'order':
      return 'payment';
    case 'email':
      return 'email';
    case 'delete':
    case 'files':
    case 'setup':
      return 'machine';
    default:
      return approval.effect === 'pay' ? 'payment' : 'post';
  }
}

/** The kind's word for the request's eyebrow: "Push", "Disposable VM", "Post". */
export function kindLabel(approval: Approval): string {
  switch (kindOf(approval)) {
    case 'push':
      return 'Push';
    case 'vm':
      return 'Disposable VM';
    case 'lane':
      return approval.preview.data?.lane_preset ? 'New lane' : 'Command in a lane';
    case 'command':
      return 'Command';
    case 'payment':
      return 'Payment';
    case 'order':
      return 'Refund';
    case 'email':
      return 'Email';
    case 'message':
      return 'Reply';
    case 'form':
      return 'Form';
    case 'delete':
      return 'Delete';
    case 'files':
      return 'Files';
    case 'setup':
      return 'Setup';
    default:
      return approval.effect === 'pay' ? 'Payment' : approval.effect === 'commit' ? 'Post' : 'Request';
  }
}

const cut = (text: string, max = 80) => (text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text);

/** The line as plain words: the title of a cut line, and what tests read. */
export function ifYesWords(approval: Approval, where: Where): string {
  const d = approval.preview.data ?? {};
  switch (kindOf(approval)) {
    case 'push': {
      const refs = d.push_refs ?? [];
      if (refs.length === 0) return cut(approval.summary);
      const commits = refs.reduce((sum, ref) => sum + (ref.commits ?? 0), 0);
      const names = refs.map((ref) => ref.name).join(', ');
      const into = refs[0]?.kind === 'delete' ? `${names} is deleted` : `${plural(commits, 'commit')} into ${names}`;
      const notes = [refs.some((ref) => ref.force) ? 'forced' : null, refs.some((ref) => ref.protected) ? 'a protected branch' : null].filter(Boolean);
      return [into, ...notes].join(', ') + (d.remote ? ` in ${repoName(d.remote)}` : '');
    }
    case 'vm': {
      const max = money(d.max_cost_eur ?? undefined, 'EUR');
      const hourly = d.price_hourly_eur != null ? `${money(d.price_hourly_eur, 'EUR')} an hour` : null;
      const limit = d.ttl_minutes ? `deletes itself after ${durationWords(d.ttl_minutes)}` : null;
      return [max ? `${max} at most` : hourly, limit].filter(Boolean).join(', ') || cut(approval.summary);
    }
    case 'lane': {
      const network = d.fields?.find((field) => field.label === 'Network')?.value;
      const limit = d.timeout_s ? `up to ${durationWords(Math.round(d.timeout_s / 60))}` : null;
      const head = d.command ? `runs ${d.command}` : null;
      return [head, network ? `network ${network}` : null, limit].filter(Boolean).join(', ') || cut(approval.summary);
    }
    case 'payment': {
      const pay = paymentWords(approval);
      const amount = pay.amount ?? amountOf(approval.summary);
      return amount ? `${amount}${pay.payee ? ` to ${pay.payee}` : ''} is paid` : cut(approval.summary);
    }
    case 'order': {
      const amount = money(d.amount, d.currency);
      return `${amount ?? 'The money'} goes back to ${d.customer ?? 'the customer'}`;
    }
    case 'email':
      return [`Sent to ${(d.to ?? []).join(', ') || 'the recipient'}`, d.subject ? `“${d.subject}”` : null].filter(Boolean).join(': ');
    case 'message':
      return `A public reply${d.thread?.[0]?.from ? ` to ${d.thread[0].from}` : ''}${where.siteLabel ? ` on ${where.siteLabel}` : ''}`;
    case 'form': {
      const changed = d.changed?.length ?? 0;
      const site = where.siteLabel ?? 'the site';
      return changed ? `${plural(changed, 'field')} change on ${site}` : `${plural(d.fields?.length ?? 0, 'field')} sent to ${site}`;
    }
    case 'delete': {
      const items = d.items ?? [];
      const total = items.reduce((sum, item) => sum + (item.size ?? 0), 0);
      return items.length ? `${plural(items.length, 'file')} gone for good${total ? `, ${size(total)}` : ''}` : cut(approval.summary);
    }
    case 'command':
      return d.command ? `runs ${d.command}${d.computer ? ` on ${d.computer}` : ''}` : cut(approval.summary);
    case 'files':
      return `${plural(d.items?.length ?? 0, 'file')} sent to ${d.destination ?? where.place ?? 'the site'}`;
    case 'setup':
      return cut(approval.summary);
    default: {
      if (approval.effect === 'pay') {
        const amount = amountOf(approval.summary);
        return amount ? `${amount} is paid${where.place ? ` from ${where.place}` : ''}` : cut(approval.summary);
      }
      if (approval.effect === 'commit' || approval.effect === 'draft') {
        const text = previewText(approval).text ?? '';
        const parts = [
          text ? `${plural(text.length, 'character')}${approval.preview.image ? ' and a picture' : ''}` : approval.preview.image ? 'A picture' : null,
          where.handle ? `as ${where.handle}` : where.place ? `on ${where.place}` : null,
        ].filter(Boolean);
        if (approval.effect === 'draft') return `A draft is saved${where.place ? ` on ${where.place}` : ''}, nothing goes public`;
        return parts.length ? parts.join(', ') : cut(approval.summary);
      }
      if (approval.effect === 'secret') return 'Signs in with the saved login; the agent never sees it';
      if (approval.effect === 'read') return 'It only looks';
      return cut(approval.summary);
    }
  }
}

/**
 * The line as shown: words, with a branch as a pill and a command in mono where that reads faster. The
 * full words are its title, so a cut line can still be read.
 */
export function IfYesValue({ approval, where }: { approval: Approval; where: Where }) {
  const d = approval.preview.data ?? {};
  const kind = kindOf(approval);
  if (kind === 'push' && d.push_refs?.length) {
    const refs = d.push_refs;
    const commits = refs.reduce((sum, ref) => sum + (ref.commits ?? 0), 0);
    const force = refs.some((ref) => ref.force);
    const guarded = refs.some((ref) => ref.protected);
    return (
      <>
        <span className="shrink-0">{refs[0]?.kind === 'delete' ? 'deletes' : `${plural(commits, 'commit')} into`}</span>
        {refs.slice(0, 2).map((ref) => (
          <BranchPill key={ref.ref} name={ref.name} main={ref.protected} />
        ))}
        {force ? <span className="shrink-0 text-bad">forced</span> : null}
        {guarded && !force ? <span className="shrink-0">protected</span> : null}
      </>
    );
  }
  if ((kind === 'command' || kind === 'lane') && d.command) {
    return (
      <>
        <span className="shrink-0">runs</span>
        <code title={d.command} className="truncate font-mono text-code-caption text-fg">
          {d.command}
        </code>
      </>
    );
  }
  const words = ifYesWords(approval, where);
  return (
    <span title={words} className="truncate">
      {words}
    </span>
  );
}

/** A brand change's own line: how many changes, to which profile. */
export function brandIfYes(proposal: BrandProposal): string {
  const changes = proposal.diff.length;
  return `${proposal.display_name} becomes version ${proposal.version}${changes ? `, ${plural(changes, 'change')}` : ''}`;
}

/**
 * The colours a brand change swaps, read from its diff lines (a token that "changed" from one hex colour "->" another). Data,
 * shown as swatches; empty when the change is not a colour.
 */
export function brandColours(diff: string[]): { name: string; from: string; to: string }[] {
  const out: { name: string; from: string; to: string }[] = [];
  for (const line of diff) {
    const match = /^(\S+) changed: (#[0-9a-f]{3,8}) -> (#[0-9a-f]{3,8})\s*$/i.exec(line);
    if (match?.[1] && match[2] && match[3]) out.push({ name: match[1].split('.').slice(-1)[0] ?? match[1], from: match[2], to: match[3] });
  }
  return out;
}
