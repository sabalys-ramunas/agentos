/**
 * "Runs on its own" or "Asks you first", where people look at a routine or an account. The switch
 * answers at once and rolls back with a line if the computer or the cloud says no. Turning one off
 * goes through the usual step-up, which http.ts runs. Who may do what is the cloud's to decide:
 * `canChange` only keeps a control away from someone it would refuse.
 */
import { Link } from '@tanstack/react-router';
import { Hand, Lock, Zap } from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { toast } from 'sonner';
import { useAccess, useLaneAskFirst, useSetFlowAskFirst, useSetLaneAskFirst, useWorkerAccount } from '@/api/hooks';
import { isCancelled } from '@/api/http';
import type { AskFirstSetBy, Flow, Lane, LaneAskFirst } from '@/api/types';
import { Switch } from '@/components/ui/toggle';
import { ACCOUNT_LIMIT, ALWAYS_ASKS, ALWAYS_ASKS_NO_SWITCH, ASKS, accountAsk, routineAsk, RUNS, whoSetIt, whoSetItShort, whyWords } from '@/lib/ask-first';
import type { AskView } from '@/lib/ask-first';
import { cn } from '@/lib/cn';
import { describeError } from '@/lib/errors';
import { timeAgo } from '@/lib/format';
import { useTechnical } from '@/lib/technical';
import { useMe, usePrincipalName } from '@/lib/use-principal';
import { accountName } from '@/features/home/words';
import { routineTitle } from '@/features/flows/routine-words';
import { AccountOverrides } from './overrides';

/** The hand is a person in the loop, the bolt is nobody needed, the lock is nobody's to change. */
function AskIcon({ view, className }: { view: AskView; className?: string }) {
  const Icon = view.asks ? (view.fixed ? Lock : Hand) : Zap;
  return <Icon aria-hidden="true" className={className} />;
}

/** Who set a switch: `inSentence` for the middle of one, "Set by you". */
function useSetterName(org: string, inSentence = false) {
  const nameOf = usePrincipalName(org);
  const me = useMe();
  const { role } = useAccess(org);
  return (by: AskFirstSetBy) => {
    // The node writes the owner as `owner`: to the owner reading it, that is "you".
    const id = by.kind === 'user' && by.id === 'owner' && role === 'owner' && me ? me.id : (by.id ?? '');
    return nameOf({ kind: by.kind === 'agent' ? 'agent' : 'human', id, thread: null }, { inSentence });
  };
}

/** Who may move this switch from where it is now. */
function useCanChange(org: string) {
  const access = useAccess(org);
  return { on: access.can('ask_first.on'), off: access.can('ask_first.off') };
}

/** Whether this person may decide in the business. Someone who may not sends a publishing run to Needs you. */
const useMayDecide = (org: string) => useAccess(org).can('approvals.decide');

/** Whoever sees no switch, or half of one, is told who to ask. */
function roleNote(view: AskView, can: { on: boolean; off: boolean }): string | null {
  if (!view.switchable) return null;
  if (!can.on) return 'An owner or admin can change this.';
  return view.checked && !can.off ? 'An owner or admin can turn this off.' : null;
}

interface FlipProps {
  name: string;
  view: AskView;
  busy: boolean;
  can: { on: boolean; off: boolean };
  onFlip: (on: boolean) => void;
  /** A row has no room for a label, so the switch names itself. */
  compact?: boolean;
}

/**
 * Stays in the page once it is flipped, even for a member who cannot flip it back, so focus has
 * somewhere to stay. Busy is `aria-disabled`, never `disabled`, for the same reason.
 */
function AskSwitch({ name, view, busy, can, onFlip, compact }: FlipProps) {
  const note = roleNote(view, can);
  const locked = view.checked && !can.off;
  return (
    <Switch
      aria-label={`Ask me first for ${name}${note ? `. ${note}` : ''}`}
      aria-disabled={busy || locked || undefined}
      checked={view.checked}
      onCheckedChange={(next) => {
        if (!busy && !locked) onFlip(next);
      }}
      className={cn('relative before:absolute before:-inset-x-2 before:-inset-y-3 aria-disabled:opacity-50', compact && 'align-middle')}
    />
  );
}

/** What a mutation needs to say about itself: the answer, or the choice that is back. */
function useFlipWords(name: string) {
  const [refused, setRefused] = useState<string | null>(null);
  return {
    refused,
    clear: () => setRefused(null),
    said: (asks: boolean) => toast(asks ? `${name} asks you first now` : `${name} runs on its own now`),
    /** Silent when the person closed the step-up: they know, and the switch is back. */
    failed: (error: unknown, wanted: boolean, inline: boolean) => {
      if (isCancelled(error)) return;
      const line = `Back to ${wanted ? RUNS : ASKS}. ${describeError(error)}`;
      if (inline) setRefused(line);
      else toast.error(line);
    },
  };
}

function useRoutineFlip(org: string, flow: Flow, name: string, inline: boolean) {
  const set = useSetFlowAskFirst(org);
  const words = useFlipWords(name);
  const flip = (on: boolean) => {
    words.clear();
    set.mutate(
      { id: flow.id, on },
      {
        onSuccess: (data) => {
          if (data.asks_first || on) words.said(Boolean(data.asks_first));
          else toast(`${name} runs on its own now`, { action: { label: 'Undo', onClick: () => flip(true) } });
        },
        onError: (error) => words.failed(error, on, inline),
      },
    );
  };
  return { flip, busy: set.isPending, refused: words.refused };
}

function useAccountFlip(org: string, lane: string, name: string, inline: boolean) {
  const set = useSetLaneAskFirst(org);
  const words = useFlipWords(name);
  const flip = (on: boolean) => {
    words.clear();
    set.mutate(
      { lane, on },
      {
        onSuccess: (data) => {
          if (data.asks_first || on) words.said(data.asks_first);
          else toast(`${name} runs on its own now`, { action: { label: 'Undo', onClick: () => flip(true) } });
        },
        onError: (error) => words.failed(error, on, inline),
      },
    );
  };
  return { flip, busy: set.isPending, refused: words.refused };
}

/** The word, with one quiet line under it when the row needs to say why. */
function Word({ view, detail }: { view: AskView; detail?: string | null }) {
  const line = view.short ?? detail;
  return (
    <span className="block min-w-0">
      <span className="flex items-center gap-2 text-13 whitespace-nowrap">
        <AskIcon view={view} className="size-4 shrink-0 text-faint" />
        {view.word}
      </span>
      {line ? <span className="block truncate pl-6 text-12 text-faint">{line}</span> : null}
    </span>
  );
}

export function RoutineAskWord({ org, flow }: { org: string; flow: Flow }) {
  const nameOf = useSetterName(org, true);
  const view = routineAsk(flow, useMayDecide(org));
  if (!view) return null;
  return <Word view={view} detail={whoSetItShort({ by: flow.ask_first_set_by, on: Boolean(flow.ask_first) }, nameOf)} />;
}

interface PanelProps {
  view: AskView;
  control: ReactNode;
  line: ReactNode;
  refused: string | null;
  busy: boolean;
  /** A narrow column: the switch gets its own row, so the word stays on one line. */
  stacked?: boolean;
  /** What this switch cannot promise, beside the switch. */
  limit?: string | null;
}

/**
 * The state, what it means, who set it, and what no switch ever changes, as a quiet strip: it belongs
 * to the routine or the account, so it never competes with them. `stacked` is the same in a narrow
 * column. The slot under the word stays open wherever a flip can fill it, so nothing moves.
 */
function Panel({ view, control, line, refused, busy, stacked, limit }: PanelProps) {
  // Something that always asks has just said so.
  const always = view.fixed && view.asks ? null : view.switchable ? ALWAYS_ASKS : ALWAYS_ASKS_NO_SWITCH;
  return (
    <section aria-label="Ask me first" className={cn(stacked ? 'border-t border-line pt-4' : 'border-y border-line py-3')}>
      <div className={cn('flex items-center gap-3', stacked && 'flex-wrap')}>
        <AskIcon view={view} className="size-4 shrink-0 text-faint" />
        <div className={cn('min-w-0 flex-1', !stacked && 'md:flex md:items-baseline md:gap-3')}>
          <h2 className="text-14 font-medium whitespace-nowrap">{view.word}</h2>
          <p className={cn('text-13 text-pretty text-muted', view.switchable && stacked && 'min-h-15')}>{view.sentence}</p>
        </div>
        {control}
      </div>
      <p aria-live="polite" className={cn('mt-1 pl-7 text-12 text-pretty', view.switchable && (stacked ? 'min-h-28' : 'min-h-12 md:min-h-4'), refused ? 'text-red' : 'text-faint')}>
        {refused ?? (busy ? 'Telling the computer.' : [line, limit, always].filter(Boolean).map((part, index) => (index ? <span key={index}> {part}</span> : <span key={index}>{part}</span>)))}
      </p>
    </section>
  );
}

function LabelledSwitch({ children, stacked }: { children: ReactNode; stacked?: boolean }) {
  return (
    <span className={cn('flex shrink-0 items-center gap-2 text-13 text-muted', stacked && 'w-full justify-between pl-7')}>
      <span aria-hidden="true">Ask me first</span>
      {children}
    </span>
  );
}

/** Who set it, who could change it, and for a switch nobody has touched, that nobody has. */
function lineOf(who: string | null, note: string | null): string {
  return [who, note].filter(Boolean).join(' ');
}

export function RoutineAskPanel({ org, flow }: { org: string; flow: Flow }) {
  const [technical] = useTechnical();
  const name = technical ? flow.id : routineTitle(flow);
  const can = useCanChange(org);
  const nameOf = useSetterName(org);
  const { flip, busy, refused } = useRoutineFlip(org, flow, name, true);
  const view = routineAsk(flow, useMayDecide(org));
  if (!view) return null;
  const note = roleNote(view, can);
  const who = whoSetIt({ by: flow.ask_first_set_by, on: Boolean(flow.ask_first), why: whyWords(flow.ask_first_why) }, nameOf, flow.ask_first_set_at ? timeAgo(flow.ask_first_set_at).toLowerCase() : null, Boolean(note));
  const [ws, lane] = (view.account ?? '').split('/');
  const place =
    ws && lane && !view.account?.includes('*') ? (
      <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name: lane }} className="link">
        Change it on the account
      </Link>
    ) : null;
  return (
    <Panel
      view={view}
      busy={busy}
      refused={refused}
      control={view.switchable && can.on ? <LabelledSwitch><AskSwitch name={name} view={view} busy={busy} can={can} onFlip={flip} /></LabelledSwitch> : null}
      line={place ?? lineOf(who, note)}
    />
  );
}

export function AccountAskPanel({ org, lane }: { org: string; lane: Lane }) {
  const ask = useLaneAskFirst(org).data?.find((item) => item.lane === lane.id);
  const name = accountName(lane);
  const can = useCanChange(org);
  const nameOf = useSetterName(org);
  const { flip, busy, refused } = useAccountFlip(org, lane.id, name, true);
  const worker = useWorkerAccount(org, lane.id).data;
  const view = accountAsk(ask, worker);
  if (!view) return null;
  const note = roleNote(view, can);
  const who = whoSetIt({ by: ask?.ask_first_set_by, on: Boolean(ask?.ask_first) }, nameOf, ask?.ask_first_set_at ? timeAgo(ask.ask_first_set_at).toLowerCase() : null, Boolean(note));
  return (
    <>
      <Panel
        stacked
        limit={view.switchable ? ACCOUNT_LIMIT : null}
        view={view}
        busy={busy}
        refused={refused}
        control={view.switchable && can.on ? <LabelledSwitch stacked><AskSwitch name={name} view={view} busy={busy} can={can} onFlip={flip} /></LabelledSwitch> : null}
        line={lineOf(who, note)}
      />
      <AccountOverrides org={org} laneId={lane.id} />
    </>
  );
}

/** One line on an account's card. Above the card's own link, so the switch is its own target. */
export function AccountAskRow({ org, lane, ask }: { org: string; lane: Lane; ask: LaneAskFirst | undefined }) {
  const name = accountName(lane);
  const can = useCanChange(org);
  const nameOf = useSetterName(org, true);
  const { flip, busy } = useAccountFlip(org, lane.id, name, false);
  const view = accountAsk(ask);
  if (!view) return null;
  return (
    <div className="relative z-10 flex min-h-11 items-center justify-between gap-3 border-t border-line px-4 py-2">
      <Word view={view} detail={whoSetItShort({ by: ask?.ask_first_set_by, on: Boolean(ask?.ask_first) }, nameOf)} />
      {view.switchable && can.on ? <AskSwitch name={name} view={view} busy={busy} can={can} onFlip={flip} compact /> : null}
    </div>
  );
}
