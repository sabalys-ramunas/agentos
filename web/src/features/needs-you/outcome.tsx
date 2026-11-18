/**
 * What came of a decision, told honestly: an Undo while the cloud still holds it, then the real
 * result from the approval's `outcome` ("Posted · View post", "Couldn't post: ..."). Nothing here
 * ever says "Approved" as if the post went out.
 */
import { Link } from '@tanstack/react-router';
import { AlertTriangle, Check, ExternalLink, RotateCcw, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useAccess, useApproval, useClearOutcome, useMembers, useSession } from '@/api/hooks';
import type { Approval, Outcome } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Button, IconButton } from '@/components/ui/button';
import { clock, safeUrl } from '@/lib/format';
import { cn } from '@/lib/cn';
import { useNow } from '@/lib/use-now';
import type { Decided } from './decision-flow';
import { blockerOf, outcomeLine, outcomeWord, whereOf } from './words';
import { Spinner } from '@/components/ui/spinner';

/** How long a final result stays before the bar steps aside. */
const RESULT_MS = 10_000;
/** How long a yes is followed before the bar says it waits for the agent (and stops spinning). */
export const WAIT_MS = 15_000;

const isFailed = (outcome: Outcome | null | undefined) => outcome?.state === 'failed' || outcome?.state === 'partial';

function ResultLine({ outcome, effect, fallback }: { outcome: Outcome | null | undefined; effect: Approval['effect']; fallback: string }) {
  if (!outcome || outcome.state === 'pending') {
    return (
      <span className="flex min-w-0 items-center gap-2">
        <Spinner className="shrink-0 text-muted" />
        <span className="min-w-0">{fallback}</span>
      </span>
    );
  }
  const url = safeUrl(outcome.proof?.url);
  if (outcome.state === 'done') {
    return (
      <span className="flex min-w-0 flex-wrap items-center gap-x-2">
        <Check aria-hidden="true" className="size-4 shrink-0 text-green" />
        <span className="min-w-0">{outcome.title}</span>
        {url ? (
          <a href={url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-blue hover:underline">
            {effect === 'commit' ? 'View post' : 'View it'}
            <ExternalLink aria-hidden="true" className="size-3" />
          </a>
        ) : null}
      </span>
    );
  }
  if (isFailed(outcome)) {
    return (
      <span className="flex min-w-0 items-start gap-2">
        <AlertTriangle aria-hidden="true" className="translate-y-0.5 size-4 shrink-0 text-amber" />
        <span className="min-w-0">{outcomeLine(outcome)} It is back in your list.</span>
      </span>
    );
  }
  return (
    <span className="flex min-w-0 items-start gap-2">
      <X aria-hidden="true" className="translate-y-0.5 size-4 shrink-0 text-muted" />
      <span className="min-w-0">{outcome.reason ?? outcome.title}</span>
    </span>
  );
}

/** One item, followed until the computer says how it went. */
function Followed({ org, item, fallback, onDone }: { org: string; item: Approval; fallback: string; onDone: () => void }) {
  const { data } = useApproval(org, item.id, true);
  const outcome = data?.outcome;
  const state = outcome?.state;
  const final = state !== undefined && state !== 'pending';
  // No result after a while: the yes stands, the agent has not reported yet. Said plainly, not spun on.
  const [waiting, setWaiting] = useState(false);
  useEffect(() => {
    if (final) return;
    const timer = setTimeout(() => setWaiting(true), WAIT_MS);
    return () => clearTimeout(timer);
  }, [final]);
  // Once the computer has said how it went, the bar stays long enough to read, then steps aside.
  useEffect(() => {
    if (!final) return;
    const timer = setTimeout(onDone, RESULT_MS);
    return () => clearTimeout(timer);
  }, [final, onDone]);
  if (!final && waiting) {
    const url = safeUrl(outcome?.proof?.url);
    return (
      <span className="flex min-w-0 flex-wrap items-center gap-x-2">
        <Check aria-hidden="true" className="size-4 shrink-0 text-green" />
        <span className="min-w-0">Approved, waiting for the agent.</span>
        {url ? (
          <a href={url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-blue hover:underline">
            {item.effect === 'commit' ? 'View post' : 'View it'}
            <ExternalLink aria-hidden="true" className="size-3" />
          </a>
        ) : null}
      </span>
    );
  }
  return <ResultLine outcome={outcome} effect={item.effect} fallback={fallback} />;
}

/**
 * The bar after a decision. On a phone it sits at the top, away from the thumb and the buttons;
 * on a desk at the bottom of the screen, away from the buttons on the card.
 */
export function OutcomeBar({
  org,
  last,
  onUndo,
  onDismiss,
  undoing,
  top = false,
}: {
  org: string;
  last: Decided | null;
  onUndo: () => void;
  onDismiss: () => void;
  undoing: boolean;
  /** Always at the top: where the buttons sit at the bottom on every width. */
  top?: boolean;
}) {
  const now = useNow(last !== null);
  const left = last ? Math.ceil((last.undoUntil - now) / 1000) : 0;
  const canUndo = last !== null && !last.undone && left > 0;
  // Steps aside by itself once there is nothing more to say.
  useEffect(() => {
    const single = last && last.items.length === 1 && !last.undone && !last.undoError;
    if (!last || canUndo || (!last.taken && !last.undone) || single) return;
    const timer = setTimeout(onDismiss, last.undone ? 4_000 : RESULT_MS);
    return () => clearTimeout(timer);
  }, [last, canUndo, onDismiss]);
  if (!last) return null;
  const [first] = last.items;
  const many = last.items.length > 1;
  const yes = last.decision === 'approve';

  let body;
  if (last.undone) body = <span>Undone. {many ? 'They are' : 'It is'} back in your list, and nothing was sent.</span>;
  else if (!last.taken) {
    body = (
      <span className="flex items-center gap-2">
        <Spinner className="text-muted" />
        {last.label}
      </span>
    );
  } else if (canUndo) body = <span className="tabular">{yes ? `${last.label} in ${left} s` : `${last.label}. Nothing is sent.`}</span>;
  else if (many || !first) body = <span>{last.label}. Each result shows under You decided today.</span>;
  else body = <Followed org={org} item={first} fallback={last.label} onDone={onDismiss} />;

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'fixed inset-x-4 top-[calc(var(--topbar-height)+8px)] z-40 mx-auto max-w-[520px] animate-pop-in overflow-hidden rounded-xl border border-line bg-raised text-14 shadow-popover',
        !top && 'md:top-auto md:bottom-6',
      )}
    >
      <div className="flex min-h-14 items-center gap-2 py-2 pr-2 pl-4">
        <div className="min-w-0 flex-1">
          {body}
          {last.undoError ? <span className="mt-1 block text-13 text-muted">{last.undoError}</span> : null}
        </div>
        {canUndo ? (
          <Button size="lg" icon={<RotateCcw className="size-4" />} onClick={onUndo} loading={undoing}>
            Undo
          </Button>
        ) : (
          <IconButton label="Close" size="md" onClick={onDismiss}>
            <X className="size-4" />
          </IconButton>
        )}
      </div>
      {canUndo ? (
        <div className="h-1 bg-fill-1">
          <div className="h-full bg-fg/30 transition-[width] duration-1000 ease-linear" style={{ width: `${Math.max(0, Math.min(100, ((last.undoUntil - now) / (last.undoUntil - last.key)) * 100))}%` }} />
        </div>
      ) : null}
    </div>
  );
}

/**
 * The node writes who decided as its own word: "owner" for the owner in person, "human:owner" for the owner's yes
 * from a routine or the node's CLI, a member's handle for a member, and "agent:…" for an agent. The `human:` is the
 * kind of decider, not its name, so it goes before anything is compared or named.
 */
const decidedWord = (decidedBy: string): string => decidedBy.replace(/^human:/, '');

/** The decider as the app matches it: the node's id when it sent one, since a member's yes is written under their handle. */
export const deciderKey = (item: Pick<Approval, 'decided_by' | 'decided_by_id'>): string | null | undefined => item.decided_by_id ?? item.decided_by;

/**
 * Whether a decision is the signed-in person's own. Matched on the decider's word (kind taken off), and by a
 * mock's or an older node's name. Only then is it "You decided".
 */
export function decidedByMe(decidedBy: string | null | undefined, user: { id: string; name?: string } | undefined): boolean {
  if (!decidedBy || !user) return false;
  const word = decidedWord(decidedBy);
  if (word === user.id || (user.name !== undefined && word === user.name)) return true;
  return user.id === 'owner' && (word === 'owner' || word.startsWith('owner:'));
}

/**
 * Who decided, as a person reads it: the node's own name for a member when it sent one, else "The owner", a
 * member's name from the members list (when the viewer may see the list), else "Someone on your team" for an id
 * nobody can name.
 */
export function deciderName(decidedBy: string | null | undefined, people?: ReadonlyMap<string, string>, name?: string | null): string {
  if (name) return name;
  if (!decidedBy) return 'Someone on your team';
  const word = decidedWord(decidedBy);
  if (word === 'owner' || word.startsWith('owner:')) return 'The owner';
  const named = people?.get(word) ?? people?.get(decidedBy);
  if (named) return named;
  if (word.includes(':') || /^(m_|usr_)/.test(word)) return 'Someone on your team';
  return word;
}

/** `deciderName` with the org's members filled in, so a member's decision carries their name. */
export function useDeciderName(org: string): (decidedBy: string | null | undefined, name?: string | null) => string {
  const members = useMembers(org).data;
  const people = useMemo(
    () =>
      new Map(
        (members ?? []).flatMap((member) => {
          const name = member.user.name || member.user.email;
          return name ? [[member.id, name], [member.user.id, name]] : [];
        }) as [string, string][],
      ),
    [members],
  );
  return useCallback((decidedBy, name) => deciderName(decidedBy, people, name), [people]);
}

/** "You" for the signed-in person's own decision, else the decider's name. */
function useDecider(org: string, item: Approval): { mine: boolean; who: string } {
  const user = useSession().data?.user;
  const nameOf = useDeciderName(org);
  const mine = decidedByMe(deciderKey(item), user);
  return { mine, who: mine ? 'You' : nameOf(item.decided_by, item.decided_by_name) };
}

/** One thing that did not work after a yes: a compact row with why, the fix and "Got it". */
function FailedRow({ org, approval, businessName, color }: { org: string; approval: Approval; businessName: string | null; color: string | null }) {
  const clear = useClearOutcome(org);
  const decider = useDecider(org, approval);
  // "Got it" takes it away for everyone, so only someone who may decide sees the button (the node refuses the rest).
  const canClear = useAccess(org).can('approvals.decide');
  const where = whereOf(approval, businessName);
  const blocker = blockerOf(approval, where);
  const [ws = '', name = ''] = approval.lane.split('/');
  const outcome = approval.outcome;
  if (!outcome) return null;
  return (
    <li aria-label={outcome.title} className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2">
      <span className="shrink-0">
        {where.site ? <SiteMark site={where.site} size={20} label={false} /> : <BusinessMark name={where.business ?? '?'} color={color} size={20} />}
      </span>
      <span className="min-w-0 flex-1 basis-48 text-13">
        <span className="block font-medium break-words">{outcome.title}</span>
        <span className="block text-muted">
          {outcome.reason ?? 'It did not happen.'} {decider.mine ? 'You said' : `${decider.who} said`} yes{approval.decided_at ? ` at ${clock(approval.decided_at)}` : ''}.
        </span>
      </span>
      <span className="ml-auto flex shrink-0 gap-2">
        {blocker && ws && name ? (
          <Button asChild variant="primary" size="sm">
            <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name }}>
              {blocker.fix}
            </Link>
          </Button>
        ) : null}
        {canClear ? (
          <Button size="sm" onClick={() => clear.mutate(approval.id)} loading={clear.isPending}>
            Got it
          </Button>
        ) : null}
      </span>
    </li>
  );
}

/**
 * Said yes to, and it did not happen: back here with why. A compact strip, so it never pushes the
 * next decision off the first screen of a phone.
 */
export function FailedStrip({
  org,
  items,
  businessOf,
  colorOf,
  className,
}: {
  org: string;
  items: Approval[];
  businessOf: (lane: string) => string | null;
  colorOf: (lane: string) => string | null;
  className?: string;
}) {
  if (items.length === 0) return null;
  return (
    <section aria-label="Did not go through" className={cn('overflow-hidden rounded-xl border border-(--wait-line) bg-(--wait-soft)', className)}>
      <h2 className="flex items-center gap-2 px-3 pt-3 text-13 font-semibold">
        <AlertTriangle aria-hidden="true" className="size-4 text-amber" />
        {items.length === 1 ? 'One thing did not go through' : `${items.length} things did not go through`}
        <span className="font-normal text-muted">· Said yes, but it did not happen</span>
      </h2>
      <ul className="divide-y divide-(--wait-line)">
        {items.map((item) => (
          <FailedRow key={item.id} org={org} approval={item} businessName={businessOf(item.lane)} color={colorOf(item.lane)} />
        ))}
      </ul>
    </section>
  );
}

const isToday = (iso: string | null | undefined, now: number) => {
  if (!iso) return false;
  const start = new Date(now);
  start.setHours(0, 0, 0, 0);
  return new Date(iso).getTime() >= start.getTime();
};

/** "You decided today": every decision with what came of it. Someone else's decision says who made it. */
export function DecidedList({ org, items, nameOf, className }: { org: string; items: Approval[]; nameOf: (lane: string) => { name: string | null; color: string | null }; className?: string }) {
  const now = Date.now();
  const user = useSession().data?.user;
  const deciderOf = useDeciderName(org);
  const today = items.filter((item) => isToday(item.decided_at, now)).slice(0, 8);
  if (today.length === 0) return null;
  const mine = (item: Approval) => decidedByMe(deciderKey(item), user);
  const label = today.every(mine) ? 'You decided today' : 'Decided today';
  return (
    <section aria-label={label} className={className}>
      <h2 className="mb-2 text-13 font-medium text-muted">{label}</h2>
      <ul className="divide-y divide-line">
        {today.map((item) => {
          const where = whereOf(item, nameOf(item.lane).name);
          const outcome = item.outcome;
          const url = safeUrl(outcome?.proof?.url);
          const failed = isFailed(outcome);
          const by = mine(item) ? null : deciderOf(item.decided_by, item.decided_by_name);
          return (
            <li key={item.id} className="flex items-start gap-3 py-2">
              <span className="translate-y-0.5">{where.site ? <SiteMark site={where.site} size={16} label={false} /> : <Check aria-hidden="true" className="size-4 text-muted" />}</span>
              <span className="min-w-0 flex-1 text-13">
                <span className={cn('block break-words', failed && 'text-amber')}>
                  {outcome?.title ?? `${by ? by : 'You'} said ${item.state === 'denied' ? 'no' : 'yes'}${where.place ? ` to ${where.place}` : ''}`}
                </span>
                <span className="block text-12 text-faint">
                  {by ? `${by} · ` : ''}
                  {item.decided_at ? clock(item.decided_at) : ''}
                  {outcome?.state === 'pending' ? ' · On its way' : outcome?.reason ? ` · ${outcome.reason}` : ''}
                  {url ? (
                    <>
                      {' · '}
                      <a href={url} target="_blank" rel="noreferrer" className="text-blue hover:underline">
                        {item.effect === 'commit' ? 'View post' : 'View it'}
                      </a>
                    </>
                  ) : null}
                </span>
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** Chips of what was decided, for the All clear state: "Posted", "Not posted". */
export function DecidedChips({ org, items, max = 6 }: { org: string; items: Approval[]; max?: number }) {
  const now = Date.now();
  const user = useSession().data?.user;
  const deciderOf = useDeciderName(org);
  const today = items.filter((item) => isToday(item.decided_at, now)).slice(0, max);
  if (today.length === 0) return null;
  return (
    <ul aria-label="What was decided today" className="flex flex-wrap justify-center gap-2">
      {today.map((item) => {
        const mine = decidedByMe(deciderKey(item), user);
        const said = item.state === 'denied' ? 'no' : 'yes';
        return (
          <li key={item.id} className="inline-flex h-8 items-center gap-2 rounded-full border border-line bg-bg px-3 text-13 text-muted">
            <SiteMark site={item.site?.key ?? item.lane} size={14} label={false} />
            {item.outcome ? outcomeWord(item.outcome, item.effect) : `${mine ? 'You' : deciderOf(item.decided_by, item.decided_by_name)} said ${said}`}
          </li>
        );
      })}
    </ul>
  );
}
