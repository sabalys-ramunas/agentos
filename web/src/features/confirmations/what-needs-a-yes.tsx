/**
 * What needs a yes: one setting, drawn as the day it shapes. Most things flow on their own, a few
 * stop at a person, and each row says how strong a yes it takes. Concept stage, on the mock only.
 * A business can ask for more than the org and never less, so choosing one lifts the floor.
 */
import { Link, useParams } from '@tanstack/react-router';
import { Card } from '@/components/ui/card';
import { ArrowRight, RotateCcw } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useAccess, useConfirmation, useSetConfirmation, useWorkspaces } from '@/api/hooks';
import { isCancelled } from '@/api/http';
import type { ConfirmationView, ConfirmRow, Workspace } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { Button } from '@/components/ui/button';
import { InlineError, QueryView, Skeleton } from '@/components/ui/states';
import { Switch } from '@/components/ui/toggle';
import { describeError } from '@/lib/errors';
import { cn } from '@/lib/cn';
import { timeAgo } from '@/lib/format';
import type { Rung, Rungs } from '@/lib/confirm';
import { businessRungs, FLOORS, rungIndex, rungsOf, RUNG_WORDS, shapeOf, withBusinessRung, withRung } from '@/lib/confirm';
import { SettingsHeader } from '@/features/settings/page';
import { DayShapeView } from './day-shape';
import { LadderLegend, RowNotes, YesGrid } from './ladder';
import type { GridScope } from './ladder';
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

function ChatAnswers({ view, canChange, busy, onChange }: { view: ConfirmationView; canChange: boolean; busy: boolean; onChange: (on: boolean) => void }) {
  const on = view.setting.chat;
  return (
    <Card role="region" aria-label="Answers in chat" className="grid gap-4 px-6 py-5">
      <div className="flex items-start gap-6">
        <div className="min-w-0 flex-1">
          <h3 className="text-section-title">Answers in chat</h3>
          <p className="mt-1 max-w-[56ch] text-secondary text-pretty text-muted">
            {on
              ? 'Your chat app can show a yes and a no, in words openajar wrote. Good for anyone without a Mac or an iPhone. Money, what can\'t be undone and anything after outside words never count there.'
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
              <span className="min-w-0 text-secondary">
                <span className="block truncate font-medium">{client.name}</span>
                <span className="tabular block truncate text-faint">
                  {client.answers} answers, last {timeAgo(client.last_at).toLowerCase()}
                </span>
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {on ? <p className="text-secondary text-pretty text-faint">A chat app that answers by itself would turn this yes into nothing. If you can&apos;t be sure of one, turn this off.</p> : null}
    </Card>
  );
}

function Page({ org, view, workspaces }: { org: string; view: ConfirmationView; workspaces: Workspace[] }) {
  const access = useAccess(org);
  const set = useSetConfirmation(org);
  const [peek, setPeek] = useState<{ scope: string | null; row: ConfirmRow; rung: Rung } | null>(null);
  const [said, setSaid] = useState<{ row: ConfirmRow | 'chat'; words: string; bad?: boolean } | null>(null);

  const orgRungs = useMemo(() => rungsOf(view.setting), [view.setting]);
  const system = useMemo(() => rungsOf(view.system), [view.system]);
  const rungsIn = (scope: string | null): Rungs => (scope ? businessRungs(view.setting, view.overlays[scope]) : orgRungs);
  const nameOf = (scope: string | null) => workspaces.find((item) => item.id === scope)?.name ?? null;
  const floorsIn = (scope: string | null): Record<ConfirmRow, Rung> => {
    const floors = { ...FLOORS };
    if (scope) for (const key of Object.keys(floors) as ConfirmRow[]) if (rungIndex(orgRungs[key]) > rungIndex(floors[key])) floors[key] = orgRungs[key];
    return floors;
  };

  // The day above is drawn for where the pointer is: everywhere, or the business being pointed at.
  const scope = peek?.scope ?? null;
  const rungs = rungsIn(scope);
  const seen = useMemo(() => {
    if (!peek) return rungs;
    const overlay = peek.scope ? view.overlays[peek.scope] : undefined;
    return { ...rungs, ...rungsOf(withRung({ ...view.setting, ...overlay }, peek.row, peek.rung)) };
  }, [peek, rungs, view.setting, view.overlays]);
  const shape = useMemo(() => shapeOf(view.day, seen, scope), [view.day, seen, scope]);
  const base = useMemo(() => shapeOf(view.day, peek ? rungs : system, scope), [view.day, peek, rungs, system, scope]);

  const pick = (where: string | null, row: ConfirmRow, rung: Rung) => {
    setPeek(null);
    setSaid({ row, words: 'Telling your computers.' });
    const body = where ? { business: where, overlay: withBusinessRung(view.setting, view.overlays[where], row, rung) } : { setting: withRung(view.setting, row, rung) };
    set.mutate(body, {
      onSuccess: () => setSaid({ row, words: `Saved: ${RUNG_WORDS[rung].word.toLowerCase()}.${where ? ` Only ${nameOf(where)}.` : ''}` }),
      onError: (error) => setSaid(isCancelled(error) ? null : { row, words: `Back to the last saved choice. ${describeError(error)}`, bad: true }),
    });
  };

  const setChat = (on: boolean) => {
    set.mutate({ setting: { ...view.setting, chat: on } }, { onError: (error) => !isCancelled(error) && setSaid({ row: 'chat', words: describeError(error), bad: true }) });
  };

  const isSystem = JSON.stringify(view.setting) === JSON.stringify(view.system);
  const more = shape.stopped - base.stopped;
  const name = nameOf(scope);
  const note = peek
    ? more === 0
      ? 'That would change how strong the yes is, and no more would stop.'
      : more > 0
        ? `That would stop ${more} more for a person on a day like this${name ? ` in ${name}` : ''}.`
        : `That would let ${-more} more go on their own on a day like this${name ? ` in ${name}` : ''}.`
    : more > 0
      ? `That is ${more} more than the system default would stop.`
      : 'This is the shape of the system default: most things on their own, money and the rest at a person.';
  const weak = (['pay', 'irreversible'] as const).some((row) => orgRungs[row] === 'signed_in');

  const canAny = access.can('confirmation.update');
  const scopes: GridScope[] = [
    { id: null, name: 'Everywhere', rungs: orgRungs, floors: floorsIn(null) },
    ...workspaces.map((workspace) => ({
      id: workspace.id,
      name: workspace.name,
      color: workspace.color,
      rungs: rungsIn(workspace.id),
      floors: floorsIn(workspace.id),
      raised: Object.keys(view.overlays[workspace.id] ?? {}).length > 0,
    })),
  ];

  return (
    <div className="grid gap-4">
      <DayShapeView day={view.day} shape={shape} note={note} lit={peek?.rung ?? null} />
      <Card role="region" aria-labelledby="yes-grid-title" className="shadow-lift">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-6 pt-5">
          <h3 id="yes-grid-title" className="text-section-title">
            How strong a yes each thing needs
          </h3>
          <span className="text-secondary text-faint">{canAny ? 'Click a step to change it' : 'An owner or admin can change this.'}</span>
          {!isSystem && canAny ? (
            <Button size="sm" variant="ghost" className="ml-auto" icon={<RotateCcw className="size-4" />} onClick={() => set.mutate({ setting: view.system })}>
              Back to the system default
            </Button>
          ) : null}
        </div>
        <div className="mt-4 border-y border-line px-6 py-3">
          <LadderLegend chat={view.setting.chat} />
        </div>
        <YesGrid
          scopes={scopes}
          may={(row) => access.can(CHANGED_BY[row])}
          onPeek={(where, row, rung) => setPeek(rung ? { scope: where, row, rung } : null)}
          onPick={pick}
        />
        <div className="grid gap-4 border-t border-line px-6 py-4">
          <p role="status" className={cn('min-h-5 text-secondary', said?.bad ? 'text-bad' : said ? 'text-muted' : 'text-faint')}>
            {said && said.row !== 'chat'
              ? said.words
              : weak
                ? 'Lower than the system default: anyone signed in on an open laptop could say yes to money or what cannot be undone.'
                : 'A business can ask for more than everywhere else, never less. What sits under a row\'s lowest step is drawn, and cannot be chosen.'}
          </p>
          <RowNotes seen={(row) => seenOn(view, row, null)} />
        </div>
      </Card>
      <ChatAnswers view={view} canChange={canAny} busy={set.isPending} onChange={setChat} />
      {said?.row === 'chat' ? (
        <InlineError>
          {said.words}
        </InlineError>
      ) : null}
      <p className="text-secondary text-muted">
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
