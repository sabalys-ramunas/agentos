/**
 * One approval as a person reads it: whose account, the plain question, the post as it will look,
 * a warning when the account cannot act, four plain facts, how big it is, and two big buttons
 * named for the action. Everything technical sits behind "Technical details".
 *
 * Used by Needs you (desk and phone), the one-by-one view and a Review link, so the same item
 * reads the same everywhere.
 */
import { Link } from '@tanstack/react-router';
import { AlertTriangle, Check, ChevronRight, Clock, Fingerprint, Hand, Lock, RotateCcw, ShieldAlert, Users, X } from 'lucide-react';
import type { ReactNode } from 'react';
import { useAccess } from '@/api/hooks';
import type { Approval } from '@/api/types';
import { AgentMark, BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { CopyButton, Detail, Kbd } from '@/components/ui/misc';
import { cn } from '@/lib/cn';
import { dateTime } from '@/lib/format';
import { usePrincipalName } from '@/lib/use-principal';
import { EFFECT_WORDS } from '@/lib/vocabulary';
import { TapLine, YesStep } from '@/features/confirmations/yes-step';
import { ChangedNotice, RefusedDelivery } from './approval-detail';
import { PostPreview } from './post-preview';
import type { Blocker, Fact, Verbs, Where } from './words';
import { SCALE, blockerOf, factsOf, questionOf, scaleOf, verbsOf, whereOf, whyAsked } from './words';

/** "How big is this?": four steps, filled up to this one. Money and the irreversible go red. */
export function ScaleBar({ step }: { step: number }) {
  const fill = step >= 2 ? 'bg-(--stuck-dot)' : step === 1 ? 'bg-(--wait-dot)' : 'bg-(--ok-dot)';
  return (
    <div role="img" aria-label={`How big is this: ${SCALE[step]}`} className="rounded-lg border border-line bg-card p-4">
      <p className="flex items-baseline justify-between gap-2 text-13">
        <span className="text-muted">How big is this?</span>
        <span className="font-semibold">{SCALE[step]}</span>
      </p>
      <div className="mt-3 grid grid-cols-4 gap-1" aria-hidden="true">
        {SCALE.map((label, index) => (
          <div key={label} className="grid gap-2">
            <span className={cn('h-1 rounded-full', index <= step ? fill : 'bg-fill-2')} />
            <span className={cn('text-11 leading-tight', index === step ? 'font-medium text-fg' : 'text-faint')}>{label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

const FACT_ICON = { why: Hand, audience: Users, undo: RotateCcw, deadline: Clock } as const;

/** Where to change why this one asked: its routine, or else its account. */
function ChangeLink({ org, approval }: { org: string; approval: Approval }) {
  const flow = approval.flow_target?.replace(/^flow:/, '').replace(/@.*$/, '');
  const [ws, name] = approval.lane.split('/');
  if (flow) {
    return (
      <Link to="/$org/flows/$flow" params={{ org, flow }} className="link">
        Change this
      </Link>
    );
  }
  if (!ws || !name) return null;
  return (
    <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name }} className="link">
      Change this
    </Link>
  );
}

/** Who asked, who will see it, can it be undone, until when: each an icon and two lines. */
export function Facts({ org, facts, approval }: { org: string; facts: Fact[]; approval: Approval }) {
  return (
    <ul className="grid gap-4">
      {facts.map((fact) => {
        const Icon = fact.key === 'asked' ? null : fact.locked ? Lock : FACT_ICON[fact.key];
        return (
          <li key={fact.key} className="flex items-start gap-3">
            {Icon ? (
              <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-line text-muted">
                <Icon aria-hidden="true" className="size-4" />
              </span>
            ) : (
              <AgentMark principal={approval.requested_by} size={32} />
            )}
            <span className="min-w-0">
              <span className="block text-14 break-words">{fact.title}</span>
              {fact.detail ? (
                <span className="block text-13 break-words text-faint">
                  {fact.detail}
                  {fact.key === 'why' && !fact.locked ? (
                    <>
                      {' '}
                      <ChangeLink org={org} approval={approval} />
                    </>
                  ) : null}
                </span>
              ) : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** It would have run on its own, and a safety check made it ask: said first, in plain words. */
export function SafetyNotice({ title, detail }: { title: string; detail: string | null }) {
  return (
    <div role="note" className="flex items-start gap-3">
      <span className="grid size-8 shrink-0 place-items-center rounded-lg border border-(--wait-line)">
        <ShieldAlert aria-hidden="true" className="size-4 text-amber" />
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-14 font-medium break-words">{title}</p>
        {detail ? <p className="text-13 text-pretty break-words text-muted">{detail}</p> : null}
      </div>
    </div>
  );
}

/** Before deciding: this account cannot act, so a yes waits. With the fix, never after the fact. */
export function BlockerNotice({ org, approval, blocker }: { org: string; approval: Approval; blocker: Blocker }) {
  const [ws = '', name = ''] = approval.lane.split('/');
  return (
    <div role="note" className="flex items-start gap-3 rounded-lg border border-(--wait-line) bg-(--wait-soft) p-4">
      <AlertTriangle aria-hidden="true" className="size-4 shrink-0 translate-y-0.5 text-amber" />
      <div className="min-w-0 flex-1">
        <p className="text-14 break-words">{blocker.sentence}</p>
        {ws && name ? (
          <Button asChild size="sm" className="mt-2">
            <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name }}>
              {blocker.fix}
            </Link>
          </Button>
        ) : null}
      </div>
    </div>
  );
}

interface ButtonsProps {
  verbs: Verbs;
  onYes: () => void;
  onNo: () => void;
  /** Off for a moment after the item arrives, so a tap meant for the one before cannot land. */
  armed: boolean;
  who: string;
  /** Show the A and D keys: on a desk only. */
  keys?: boolean;
  /** A tap isn't enough here, so the yes carries the fingerprint and nothing else does. */
  proof?: boolean;
}

/** No on the left, yes on the right, named for the action: full width on a phone, at the right on a desk. */
export function DecisionButtons({ verbs, onYes, onNo, armed, who, keys: wantKeys = true, proof }: ButtonsProps) {
  // A long label ("Post it once fixed") needs the room more than the key hint does.
  const keys = wantKeys && verbs.yes.length <= 10 && verbs.no.length <= 10;
  return (
    <div className="grid gap-2">
      <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:justify-end" data-armed={armed || undefined}>
        <Button size="lg" className="h-auto min-h-10 px-4 whitespace-normal" icon={<X className="size-4" />} disabled={!armed} onClick={onNo}>
          {verbs.no}
          {keys ? (
            <Kbd aria-hidden="true" className="ml-1 hidden lg:inline-flex">
              D
            </Kbd>
          ) : null}
        </Button>
        <Button variant="primary" size="lg" className="h-auto min-h-10 px-4 whitespace-normal" icon={proof ? <Fingerprint className="size-4" /> : <Check className="size-4" />} disabled={!armed} onClick={onYes}>
          {verbs.yes}
          {keys ? (
            <Kbd aria-hidden="true" className="ml-1 hidden border-on-primary/30 bg-transparent text-on-primary/70 lg:inline-flex">
              A
            </Kbd>
          ) : null}
        </Button>
      </div>
      <p className="text-center text-caption text-faint sm:text-right">
        {verbs.no} tells {who} no. {verbs.noMeans}
      </p>
    </div>
  );
}

/** Where the decided card was: done, with no button where the finger just was. */
export function DoneState({ approval, decision, label }: { approval: Approval; decision: 'approve' | 'deny'; label: string }) {
  const yes = decision === 'approve';
  return (
    <div role="status" className="flex min-h-10 animate-fade-in items-center justify-center gap-2 rounded-md border border-line bg-inset px-4 py-2 text-label sm:justify-end" data-done={approval.id}>
      {yes ? <Check aria-hidden="true" className="size-4 text-green" /> : <X aria-hidden="true" className="size-4 text-muted" />}
      {label}
    </div>
  );
}

/** The check drawn over the preview once it is decided. */
function DoneMark({ decision }: { decision: 'approve' | 'deny' }) {
  return (
    <div aria-hidden="true" className="absolute inset-0 flex animate-fade-in items-center justify-center">
      <span className={cn('flex size-20 animate-pop-in items-center justify-center rounded-full bg-bg shadow-popover', decision === 'approve' ? 'text-green' : 'text-muted')}>
        {decision === 'approve' ? <Check className="size-10" strokeWidth={2.5} /> : <X className="size-10" strokeWidth={2.5} />}
      </span>
    </div>
  );
}

interface TechnicalProps {
  org: string;
  approval: Approval;
  open: boolean;
  onToggle: () => void;
  /** "Always allow posts like this", for those who may set it up. */
  allowAgain?: ReactNode;
}

/** Ids, the effect, the routine, the content ID: for whoever wants them, closed by default. */
export function TechnicalDetails({ org, approval, open, onToggle, allowAgain }: TechnicalProps) {
  const nameOf = usePrincipalName(org);
  const what = approval.flow_target ?? approval.tool;
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="-ml-1 inline-flex h-8 items-center gap-1 rounded-sm px-1 text-13 text-faint hover:text-fg"
      >
        <ChevronRight aria-hidden="true" className={cn('size-4 transition-transform duration-(--duration-base) ease-standard', open && 'rotate-90')} />
        Technical details
      </button>
      {open ? (
        <div className="mt-2 grid gap-2">
          <dl className="divide-y divide-line rounded-lg border border-line px-3">
            <Detail label="The computer says">{approval.summary}</Detail>
            {approval.reason ? <Detail label="Why it asks">{approval.reason}</Detail> : null}
            <Detail label="Effect">
              <span className="mono">{approval.effect}</span> · {EFFECT_WORDS[approval.effect].word}
            </Detail>
            {what ? (
              <Detail label={approval.flow_target ? 'Routine' : 'Tool'}>
                <span className="mono break-all">{what}</span>
              </Detail>
            ) : null}
            {approval.lane ? (
              <Detail label="Account">
                <span className="mono">{approval.lane}</span>
              </Detail>
            ) : null}
            <Detail label="Asked by">
              {nameOf(approval.requested_by)} <span className="mono text-faint">{approval.requested_by.id}</span>
              {approval.requested_by.thread ? <span className="mono block text-faint">{approval.requested_by.thread}</span> : null}
            </Detail>
            <Detail label="Asked at">{dateTime(approval.created_at)}</Detail>
            <Detail label="Ends at">{dateTime(approval.expires_at)}</Detail>
          </dl>
          <div>
            <p className="text-13 text-faint">Content ID. A yes covers exactly this; if anything changes, you are asked again.</p>
            <div className="mt-1 flex items-start gap-2 rounded-md bg-subtle py-1 pr-1 pl-2">
              <code className="mono min-w-0 flex-1 py-1 text-12 break-all text-muted">{approval.content_hash}</code>
              <CopyButton text={approval.content_hash} label="Copy content ID" />
            </div>
          </div>
          {allowAgain}
        </div>
      ) : null}
    </div>
  );
}

export interface Shown {
  where: Where;
  question: string;
  verbs: Verbs;
  facts: Fact[];
  blocker: Blocker | null;
  who: string;
}

/** Everything the card says, worked out once. */
export function useShown(org: string, approval: Approval, businessName: string | null): Shown {
  const nameOf = usePrincipalName(org);
  // In a sentence: "Let you look at this?", "Don't post tells you no." (never "tells You no").
  const who = nameOf(approval.requested_by, { inSentence: true });
  const where = whereOf(approval, businessName);
  const blocker = blockerOf(approval, where);
  return {
    where,
    question: questionOf(approval, where, who),
    verbs: verbsOf(approval, where, blocker?.state ?? null),
    facts: factsOf(approval, where, who),
    blocker,
    who,
  };
}

interface CardProps {
  org: string;
  approval: Approval;
  shown: Shown;
  color: string | null;
  /** `split` puts the preview beside the facts (a desk); `stack` puts one under the other. */
  layout: 'split' | 'stack';
  /** The buttons, or the done state in their place. */
  actions: ReactNode;
  /** Decided here a moment ago: the check over the preview. */
  done?: 'approve' | 'deny' | null;
  changed?: boolean;
  technical: ReactNode;
  /** On a phone the buttons ride along at the bottom, in reach of a thumb. */
  stickyActions?: boolean;
}

export function DecisionCard({ org, approval, shown, color, layout, actions, done, changed, technical, stickyActions }: CardProps) {
  const { where } = shown;
  // "Just a tap" tells a person how to answer. Someone who cannot answer it is not told how.
  const canAnswer = useAccess(org).can('approvals.decide');
  const tap = canAnswer ? <TapLine approval={approval} /> : null;
  const why = whyAsked(approval);
  const safety = why?.kind === 'safety' ? why : null;
  const head = (
    <header className="grid gap-2">
      <p className="flex min-w-0 items-center gap-2 text-secondary text-muted">
        <BusinessMark name={where.business ?? 'Business'} color={color} size={20} />
        <span className="truncate font-medium text-fg">{where.business ?? 'No business'}</span>
        {where.siteLabel ? (
          <>
            <ChevronRight aria-hidden="true" className="size-3 shrink-0 text-faint" />
            {where.site ? <SiteMark site={where.site} size={16} label={false} /> : null}
            <span className="truncate">{where.handle ?? where.siteLabel}</span>
          </>
        ) : null}
      </p>
      <h2 className="text-page-title-compact break-words">{shown.question}</h2>
    </header>
  );
  const notices = (
    <>
      {changed ? <ChangedNotice /> : null}
      <RefusedDelivery approval={approval} />
      {safety ? <SafetyNotice title={safety.title} detail={safety.detail} /> : null}
      {shown.blocker ? <BlockerNotice org={org} approval={approval} blocker={shown.blocker} /> : null}
    </>
  );
  // Mono: what a yes does, under the one caps label, then the preview fitted to the kind.
  const preview = (
    <section aria-label="If you say yes" className="grid gap-3">
      <p className="text-eyebrow text-faint">If you say yes</p>
      <PostPreview approval={approval} where={where} color={color}>
        {done ? <DoneMark decision={done} /> : null}
      </PostPreview>
    </section>
  );
  const actionsBox = stickyActions ? (
    <div className="sticky bottom-[calc(var(--tabbar-height)+env(safe-area-inset-bottom))] z-10 -mx-4 border-t border-line bg-card/95 px-4 pt-3 pb-3 backdrop-blur md:bottom-0 lg:static lg:mx-0 lg:bg-transparent lg:px-0 lg:pb-0 lg:backdrop-blur-none">
      {actions}
    </div>
  ) : (
    <div className="border-t border-line pt-4">{actions}</div>
  );
  const details = (
    <div className={cn('grid items-start gap-6', layout === 'split' && 'xl:grid-cols-[minmax(0,1fr)_minmax(0,320px)]')}>
      <Facts org={org} facts={shown.facts} approval={approval} />
      <div className="grid gap-4">
        <ScaleBar step={scaleOf(approval.effect)} />
        {technical}
      </div>
    </div>
  );

  return (
    <article aria-label={shown.question} className="grid gap-5">
      {head}
      {notices}
      {preview}
      <YesStep approval={approval} />
      {actionsBox}
      {tap}
      {details}
    </article>
  );
}
