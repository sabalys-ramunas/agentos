/**
 * What needs a yes: one setting, drawn as the day it shapes. Most things flow on their own, a few
 * stop at a person, and each row says how strong a yes it takes. Concept stage, on the mock only.
 * A business can ask for more than the org and never less, so choosing one lifts the floor.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ArrowRight, RotateCcw } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useAccess, useConfirmation, useSetConfirmation, useWorkspaces } from '@/api/hooks';
import { isCancelled } from '@/api/http';
import type { ConfirmationView, ConfirmRow, Workspace } from '@/api/types';
import { AgentMark, BusinessMark } from '@/components/brand/identity';
import { Button } from '@/components/ui/button';
import { QueryView, Skeleton } from '@/components/ui/states';
import { Switch } from '@/components/ui/toggle';
import { describeError } from '@/lib/errors';
import { cn } from '@/lib/cn';
import { timeAgo } from '@/lib/format';
import type { Rung, Rungs } from '@/lib/confirm';
import { businessRungs, FLOORS, rungIndex, rungsOf, RUNG_WORDS, ROWS, shapeOf, withBusinessRung, withRung } from '@/lib/confirm';
import { SettingsHeader } from '@/features/settings/page';
import { DayShapeView } from './day-shape';
import { FixedRow, LadderHead, LadderRow } from './ladder';
import './confirmations.css';

const CHANGED_BY = { commit: 'confirmation.update', marked: 'confirmation.update', after_untrusted: 'confirmation.money', pay: 'confirmation.money', irreversible: 'confirmation.money' } as const;

/** What the row really did on the day, in the words of what happened. */
function seenOn(view: ConfirmationView, row: ConfirmRow, business: string | null): string {
  const mine = <T extends { business: string }>(items: T[]) => (business ? items.filter((item) => item.business === business) : items);
  if (row === 'commit') return `Yesterday: ${mine(view.day.ticks).filter((tick) => tick.kind === 'commit').length} of these`;
  const stops = mine(view.day.stops).filter((stop) => stop.row === row);
  const [first] = stops;
  if (!first) return 'None yesterday';
  return `Yesterday: ${first.title}${stops.length > 1 ? `, and ${stops.length - 1} more` : ''}`;
}

function Scope({ workspaces, view, scope, onScope }: { workspaces: Workspace[]; view: ConfirmationView; scope: string | null; onScope: (id: string | null) => void }) {
  const raised = (id: string) => Object.keys(view.overlays[id] ?? {}).length > 0;
  const item = (id: string | null, label: string, mark: React.ReactNode, more: boolean) => (
    <button
      key={id ?? 'all'}
      type="button"
      role="radio"
      aria-checked={scope === id}
      onClick={() => onScope(id)}
      className={cn(
        'flex h-8 items-center gap-2 rounded-md px-2 text-13 whitespace-nowrap transition-colors duration-(--duration-fast) ease-standard',
        scope === id ? 'bg-fill-2 text-fg' : 'text-muted hover:bg-fill-1 hover:text-fg',
      )}
    >
      {mark}
      {label}
      {more ? <span className="text-12 text-faint">asks for more</span> : null}
    </button>
  );
  return (
    <div role="radiogroup" aria-label="Where this applies" className="-mx-2 flex items-center gap-1 overflow-x-auto px-2">
      {item(null, 'Everywhere', null, false)}
      {workspaces.map((workspace) => item(workspace.id, workspace.name, <BusinessMark name={workspace.name} color={workspace.color} size={16} />, raised(workspace.id)))}
    </div>
  );
}

function ChatAnswers({ view, canChange, busy, onChange }: { view: ConfirmationView; canChange: boolean; busy: boolean; onChange: (on: boolean) => void }) {
  const on = view.setting.chat;
  return (
    <section aria-label="Answers in chat" className="grid gap-4 rounded-xl border border-line p-6">
      <div className="flex items-start gap-6">
        <div className="min-w-0 flex-1">
          <h3 className="text-16 font-semibold">Answers in chat</h3>
          <p className="mt-1 max-w-[56ch] text-14 text-pretty text-muted">
            {on
              ? 'Your chat app can show a yes and a no, in words agentos wrote. Good for anyone without a Mac or an iPhone. Money, what can\'t be undone and anything after outside words never count there.'
              : 'A yes in a chat counts for nothing now. Everything is answered here or in the Mac app, and a chat can still say no.'}
          </p>
        </div>
        <Switch aria-label="Let people answer in chat" aria-disabled={busy || !canChange || undefined} checked={on} onCheckedChange={(next) => !busy && canChange && onChange(next)} className="relative mt-1 aria-disabled:opacity-50" />
      </div>
      {on && view.clients.length > 0 ? (
        <ul className="grid gap-2 border-t border-line pt-4 sm:grid-cols-2">
          {view.clients.map((client) => (
            <li key={client.name} className="flex min-w-0 items-center gap-3">
              <AgentMark principal={{ kind: 'agent', id: client.agent, thread: null }} name={client.name} size={24} />
              <span className="min-w-0 text-13">
                <span className="block truncate font-medium">{client.name}</span>
                <span className="tabular block truncate text-faint">
                  {client.answers} answers, last {timeAgo(client.last_at).toLowerCase()}
                </span>
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {on ? <p className="text-13 text-pretty text-faint">A chat app that answers by itself would turn this yes into nothing. If you can&apos;t be sure of one, turn this off.</p> : null}
    </section>
  );
}

function Page({ org, view, workspaces }: { org: string; view: ConfirmationView; workspaces: Workspace[] }) {
  const access = useAccess(org);
  const set = useSetConfirmation(org);
  const [scope, setScope] = useState<string | null>(null);
  const [peek, setPeek] = useState<{ row: ConfirmRow; rung: Rung } | null>(null);
  const [lit, setLit] = useState<Rung | null>(null);
  const [said, setSaid] = useState<{ row: ConfirmRow | 'chat'; words: string; bad?: boolean } | null>(null);

  const org_rungs = useMemo(() => rungsOf(view.setting), [view.setting]);
  const system = useMemo(() => rungsOf(view.system), [view.system]);
  const overlay = scope ? view.overlays[scope] : undefined;
  const rungs: Rungs = useMemo(() => (scope ? businessRungs(view.setting, overlay) : org_rungs), [scope, view.setting, overlay, org_rungs]);
  const name = workspaces.find((item) => item.id === scope)?.name ?? null;

  /** The rows with what is being pointed at. Marked and posts share one level once posts ask. */
  const seen = useMemo(() => {
    if (!peek) return rungs;
    const moved = rungsOf(withRung({ ...view.setting, ...overlay }, peek.row, peek.rung));
    return { ...rungs, ...moved };
  }, [peek, rungs, view.setting, overlay]);
  const shape = useMemo(() => shapeOf(view.day, seen, scope), [view.day, seen, scope]);
  const base = useMemo(() => shapeOf(view.day, peek ? rungs : system, scope), [view.day, peek, rungs, system, scope]);

  const pick = (row: ConfirmRow, rung: Rung) => {
    setPeek(null);
    setSaid({ row, words: 'Telling your computers.' });
    const body = scope ? { business: scope, overlay: withBusinessRung(view.setting, overlay, row, rung) } : { setting: withRung(view.setting, row, rung) };
    set.mutate(body, {
      onSuccess: () => setSaid({ row, words: `Saved: ${RUNG_WORDS[rung].word.toLowerCase()}.` }),
      onError: (error) => setSaid(isCancelled(error) ? null : { row, words: `Back to the last saved choice. ${describeError(error)}`, bad: true }),
    });
  };

  const setChat = (on: boolean) => {
    set.mutate({ setting: { ...view.setting, chat: on } }, { onError: (error) => !isCancelled(error) && setSaid({ row: 'chat', words: describeError(error), bad: true }) });
  };

  const isSystem = !scope && JSON.stringify(view.setting) === JSON.stringify(view.system);
  const more = shape.stopped - base.stopped;
  const note = peek
    ? more === 0
      ? 'That would change how strong the yes is, and no more would stop.'
      : more > 0
        ? `That would stop ${more} more for a person on a day like this.`
        : `That would let ${-more} more go on their own on a day like this.`
    : scope
      ? `Only ${name}. It can ask for more than everywhere else, never less.`
      : more > 0
        ? `That is ${more} more than the system default would stop.`
        : 'This is the shape of the system default: most things on their own, money and the rest at a person.';

  const noteOf = (row: ConfirmRow): { words: string | null; warn?: boolean } => {
    if (said?.row === row) return { words: said.words, warn: said.bad };
    if (!scope && (row === 'pay' || row === 'irreversible') && rungs[row] === 'signed_in') return { words: 'Lower than the system default: anyone signed in on an open laptop could say yes.', warn: true };
    if (row === 'marked' && rungs.commit !== 'auto') return { words: 'Everything asks now, so these ask the same.' };
    if (scope && rungIndex(rungs[row]) > rungIndex(org_rungs[row])) return { words: `Above everywhere else, for ${name}.` };
    return { words: null };
  };

  const floorOf = (row: ConfirmRow): Rung => {
    const floor = FLOORS[row];
    if (!scope) return floor;
    return rungIndex(org_rungs[row]) > rungIndex(floor) ? org_rungs[row] : floor;
  };

  const canAny = access.can('confirmation.update');
  const looks = view.day.ticks.filter((tick) => tick.kind === 'look' && (!scope || tick.business === scope)).length;

  return (
    <div className="grid gap-8">
      <DayShapeView day={view.day} shape={shape} note={note} lit={peek?.rung ?? lit} />
      <section aria-label="How strong a yes each thing needs" className="grid gap-4">
        <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2">
          <div className="min-w-0">
            <h3 className="text-16 font-semibold">How strong a yes each thing needs</h3>
            <p className="mt-1 text-13 text-muted">{canAny ? 'Move a dot to change it. Point at a stop to see the day above change first.' : 'An owner or admin can change this.'}</p>
          </div>
          {!isSystem && canAny && !scope ? (
            <Button size="sm" variant="ghost" icon={<RotateCcw className="size-4" />} onClick={() => set.mutate({ setting: view.system })}>
              Back to the system default
            </Button>
          ) : null}
        </div>
        <Scope workspaces={workspaces} view={view} scope={scope} onScope={(id) => { setScope(id); setSaid(null); }} />
        <LadderHead chat={view.setting.chat} lit={lit} onLit={setLit} />
        <ul>
          <FixedRow seen={`Yesterday: ${looks} of these`} />
          {ROWS.map((row) => {
            const words = noteOf(row.key);
            const may = access.can(CHANGED_BY[row.key]);
            return (
              <LadderRow
                key={row.key}
                row={row}
                rung={rungs[row.key]}
                floor={floorOf(row.key)}
                system={scope ? undefined : system[row.key]}
                locked={row.key === 'pay' || row.key === 'irreversible'}
                note={words.words}
                warn={words.warn}
                seen={seenOn(view, row.key, scope)}
                onPeek={may ? (key, rung) => setPeek(rung ? { row: key, rung } : null) : undefined}
                onPick={may ? pick : undefined}
                onHover={setLit}
              />
            );
          })}
        </ul>
      </section>
      <ChatAnswers view={view} canChange={canAny} busy={set.isPending} onChange={setChat} />
      {said?.row === 'chat' ? (
        <p role="alert" className="text-13 text-red">
          {said.words}
        </p>
      ) : null}
      <p className="text-13 text-muted">
        <Link to="/$org/settings/say-yes" params={{ org }} className="inline-flex items-center gap-1 hover:text-fg">
          See where you can say yes
          <ArrowRight aria-hidden="true" className="size-4" />
        </Link>
      </p>
    </div>
  );
}

function PageSkeleton() {
  return (
    <div className="grid gap-8" role="status" aria-label="Loading">
      <Skeleton className="h-96 rounded-xl" />
      <div className="grid gap-4">
        <Skeleton className="h-6 w-1/3" />
        {Array.from({ length: 5 }, (_, index) => (
          <Skeleton key={index} className="h-20 rounded-lg" />
        ))}
      </div>
    </div>
  );
}

export function WhatNeedsAYes() {
  const { org } = useParams({ from: '/$org/settings/yes' });
  const query = useConfirmation(org);
  const workspaces = useWorkspaces(org).data ?? [];
  return (
    <>
      <SettingsHeader title="What needs a yes" description="Most things just happen. This is what stops for a person, and how much of a person it takes." />
      <QueryView query={query} card skeleton={<PageSkeleton />}>
        {(view) => <Page org={org} view={view} workspaces={workspaces} />}
      </QueryView>
    </>
  );
}
