/**
 * Routines, as a person comes to them: what ran, what comes next, and what is stuck. A routine and
 * a workflow are the same thing here. The page opens on one sentence and a line through now, puts
 * anything stuck first, then shows every routine by business, recognisable by its sites and the
 * plain sentence of what it does. Running one or changing its switch happens on its own page, so
 * nothing in this list can be flipped by a slip of the finger.
 */
import { Link, useParams, useSearch } from '@tanstack/react-router';
import { ArrowRight, ChevronRight, Hand, Lock, Pause, Plus, Search, Sparkles } from 'lucide-react';
import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useDeleteSchedule, useFlows, useNodes, useRoutineCatalog, useRoutineDay, useSchedules, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { Flow, RoutineCatalog, Schedule, Workspace } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Count } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { Input } from '@/components/ui/field';
import { StateGlyph } from '@/components/ui/state-badge';
import { EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { useActiveWorkspace } from '@/lib/active-workspace';
import { routineAsk } from '@/lib/ask-first';
import { cn } from '@/lib/cn';
import { plural, timeAgo } from '@/lib/format';
import { routineName } from '@/lib/principal';
import { useTechnical } from '@/lib/technical';
import { DayLine } from './day-line';
import { routineSentence, routineSites, routineTitle, whenNext } from './routine-words';
import { ScheduleDialog, SchedulesPanel } from './schedules';
import { NO_ROUTINE } from './schedule-timeline';
import { WeekAhead } from './week-ahead';

export type FlowsTab = 'flows' | 'schedules';

const SHOWN = 6;

/** Where a routine acts, large, with where it starts as a small badge when that differs. */
export function SiteStack({ sites }: { sites: string[] }) {
  const main = sites.at(-1) ?? '';
  const from = sites.length > 1 ? sites[0] : null;
  return (
    <span aria-hidden="true" className="relative block size-10 shrink-0">
      <SiteMark site={main} size={40} tile label={false} className="bg-card" />
      {from ? (
        <span className="absolute -right-1.5 -bottom-1.5 rounded-md bg-card p-px">
          <SiteMark site={from} size={20} tile label={false} className="bg-card" />
        </span>
      ) : null}
    </span>
  );
}

function Hero({ flows, ran, checked, soonest }: { flows: Flow[]; ran: number | null; checked: number | null; soonest: Flow | undefined }) {
  const stuck = flows.filter((flow) => flow.stuck).length;
  const waiting = flows.filter((flow) => flow.waiting).length;
  const lead = stuck ? `${plural(stuck, 'routine is', 'routines are')} stuck.` : waiting ? `${plural(waiting, 'routine is', 'routines are')} waiting for you.` : 'Your routines are all running.';
  const rest = stuck || waiting ? `The other ${flows.length - stuck - waiting} are fine.` : 'Nothing needs you.';
  return (
    <div className="max-w-3xl">
      <h1 className="text-headline text-balance sm:text-[40px] sm:leading-[44px] sm:tracking-[-0.045em]">
        {lead} <span className="text-faint">{rest}</span>
      </h1>
      <p className="mt-3 max-w-2xl text-lede text-pretty text-muted">
        {ran === null ? (
          <span className="invisible">Loading</span>
        ) : ran === 0 ? (
          'Nothing has run yet today.'
        ) : (
          <>
            <strong className="font-semibold text-fg">{plural(ran, 'run')}</strong> so far today
            {checked ? (
              <>
                , and <strong className="font-semibold text-fg">{checked}</strong> checked on the real site
              </>
            ) : null}
            .
          </>
        )}{' '}
        {soonest?.next_at ? (
          <>
            Next: {routineTitle(soonest)}, {whenNext(soonest.next_at)}.
          </>
        ) : null}
      </p>
    </div>
  );
}

/** One stuck or waiting routine: what happened and the one thing to do. */
function Problem({ org, flow, business }: { org: string; flow: Flow; business: Workspace | undefined }) {
  const stuck = flow.stuck;
  const site = (stuck ? flow.sites?.at(-1) : flow.sites?.[0]) ?? '';
  const [ws, lane] = (stuck?.lane ?? '').split('/');
  return (
    <li className={cn('grid gap-4 rounded-(--radius-card) border bg-card p-4 shadow-(--shadow-card) sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-center', stuck ? 'border-(--stuck-line)' : 'border-(--wait-line)')}>
      <span className="relative hidden sm:block">
        <SiteMark site={site} size={40} tile label={false} className="bg-card" />
        <span className="absolute -top-1 -right-1 rounded-full bg-card p-px">
          <StateGlyph state={stuck ? 'stuck' : 'waiting'} size={16} />
        </span>
      </span>
      <div className="min-w-0">
        <p className="flex items-center gap-2 text-caption text-faint">
          {business ? <BusinessMark name={business.name} color={business.color} size={16} /> : null}
          {business?.name}
        </p>
        <h3 className="mt-1 text-section-title">{stuck ? `${routineTitle(flow)} stopped at "${stuck.label}"` : `${routineTitle(flow)} is waiting for you`}</h3>
        <p className="mt-1 text-body text-pretty text-muted">{stuck ? stuck.words : 'It wrote this week\'s email and will send it once you say yes.'}</p>
      </div>
      <div className="flex gap-2 sm:justify-end">
        {stuck?.fix && ws && lane ? (
          <Button variant="primary" asChild>
            <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name: lane }}>
              {stuck.fix}
            </Link>
          </Button>
        ) : !stuck ? (
          <Button variant="primary" asChild>
            <Link to="/$org/needs-you" params={{ org }}>
              Look and decide
            </Link>
          </Button>
        ) : null}
        <Button asChild>
          <Link to="/$org/flows/$flow" params={{ org, flow: flow.id }}>
            {stuck ? 'See what happened' : 'Open'}
          </Link>
        </Button>
      </div>
    </li>
  );
}

/** Where it stands, on the right of a row: what it last did, and when it runs next. */
function Facts({ flow, canDecide }: { flow: Flow; canDecide: boolean }) {
  const ask = routineAsk(flow, canDecide);
  const next = flow.enabled === false ? 'Paused' : flow.next_at ? `Next ${whenNext(flow.next_at)}` : flow.trigger?.kind === 'schedule' ? null : 'When it is needed';
  return (
    <span className="tabular block min-w-0 text-secondary md:text-right">
      <span className="block truncate text-muted">{flow.latest ? `${flow.latest.status === 'failed' ? 'Did not work' : flow.latest.result}, ${timeAgo(flow.latest.at).toLowerCase()}` : 'Has not run yet'}</span>
      <span className="flex items-center gap-2 truncate text-caption text-faint md:justify-end">
        {flow.enabled === false ? <Pause aria-hidden="true" className="size-3 shrink-0" /> : null}
        {next}
        {ask?.asks ? (
          <>
            <span aria-hidden="true">·</span>
            {ask.fixed ? <Lock aria-hidden="true" className="size-3 shrink-0" /> : <Hand aria-hidden="true" className="size-3 shrink-0" />}
            <span>{ask.word}</span>
          </>
        ) : ask ? (
          <span className="sr-only">{ask.word}</span>
        ) : null}
      </span>
    </span>
  );
}

function Row({ org, flow, catalog }: { org: string; flow: Flow; catalog: RoutineCatalog | undefined }) {
  const canDecide = useAccess(org).can('approvals.decide');
  return (
    <li>
      <Link
        to="/$org/flows/$flow"
        params={{ org, flow: flow.id }}
        className="group/row -mx-2 grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-4 gap-y-1 rounded-lg px-2 py-3 transition-colors duration-(--duration-fast) ease-standard hover:bg-hover md:grid-cols-[auto_minmax(0,1fr)_minmax(0,18rem)_auto]"
      >
        <SiteStack sites={routineSites(flow)} />
        <span className="min-w-0">
          <span className="block truncate text-label">{routineTitle(flow)}</span>
          <span className="line-clamp-2 text-secondary text-pretty text-muted md:line-clamp-1">{routineSentence(flow, catalog)}</span>
        </span>
        <span className="col-start-2 md:col-start-auto">
          <Facts flow={flow} canDecide={canDecide} />
        </span>
        <ChevronRight aria-hidden="true" className="hidden size-4 text-faint opacity-0 transition-opacity duration-(--duration-fast) ease-standard group-hover/row:opacity-100 md:block" />
      </Link>
    </li>
  );
}

function Business({ org, name, color, flows, catalog }: { org: string; name: string; color?: string | null; flows: Flow[]; catalog: RoutineCatalog | undefined }) {
  const [all, setAll] = useState(false);
  const listed = all ? flows : flows.slice(0, SHOWN);
  return (
    <section aria-label={name}>
      <div className="mb-2 flex items-center gap-2">
        <BusinessMark name={name} color={color} size={20} />
        <h2 className="text-section-title">{name}</h2>
        <Count value={flows.length} />
      </div>
      <ul className="divide-y divide-line">
        {listed.map((flow) => (
          <Row key={flow.id} org={org} flow={flow} catalog={catalog} />
        ))}
      </ul>
      {flows.length > SHOWN ? (
        <Button variant="ghost" size="sm" className="mt-1" onClick={() => setAll(!all)}>
          {all ? 'Show fewer' : `Show ${flows.length - SHOWN} more`}
        </Button>
      ) : null}
    </section>
  );
}

type Show = 'all' | 'asks' | 'paused';

const matches = (flow: Flow, query: string) => `${routineTitle(flow)} ${routineSentence(flow)} ${(flow.sites ?? []).join(' ')}`.toLowerCase().includes(query.trim().toLowerCase());

/** Sorted so what matters sits on top: the next to run first, then the rest by name. */
function ordered(flows: Flow[]) {
  return [...flows].sort((a, b) => (a.next_at ?? '9').localeCompare(b.next_at ?? '9') || routineTitle(a).localeCompare(routineTitle(b)));
}

function Everything({ org, flows, workspaces }: { org: string; flows: Flow[]; workspaces: Workspace[] }) {
  const catalog = useRoutineCatalog(org).data;
  const [query, setQuery] = useState('');
  const [show, setShow] = useState<Show>('all');
  const asks = flows.filter((flow) => flow.asks_first);
  const paused = flows.filter((flow) => flow.enabled === false);
  const chosen = (show === 'asks' ? asks : show === 'paused' ? paused : flows).filter((flow) => matches(flow, query));
  const groups = useMemo(() => {
    const byBusiness = new Map<string, Flow[]>();
    for (const flow of chosen) {
      const key = flow.lane.split('/')[0] ?? '';
      byBusiness.set(key, [...(byBusiness.get(key) ?? []), flow]);
    }
    return [...byBusiness.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [chosen]);
  const pills: { value: Show; label: string; count: number }[] = [
    { value: 'all', label: 'All', count: flows.length },
    { value: 'asks', label: 'Asks you first', count: asks.length },
    { value: 'paused', label: 'Paused', count: paused.length },
  ];
  return (
    <div className="grid gap-10">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div role="radiogroup" aria-label="Show" className="scrollbar-none -mx-1 flex gap-1 overflow-x-auto px-1">
          {pills
            .filter((pill) => pill.count > 0 || pill.value === 'all')
            .map((pill) => (
              <button
                key={pill.value}
                type="button"
                role="radio"
                aria-checked={show === pill.value}
                onClick={() => setShow(pill.value)}
                className={cn(
                  'tabular flex h-8 shrink-0 items-center gap-2 rounded-full px-3 text-secondary transition-colors duration-(--duration-fast) ease-standard',
                  show === pill.value ? 'bg-pressed text-fg' : 'text-muted hover:bg-hover hover:text-fg',
                )}
              >
                {pill.label}
                <span className="text-faint">{pill.count}</span>
              </button>
            ))}
        </div>
        <div className="relative sm:w-64">
          <Search aria-hidden="true" className="pointer-events-none absolute top-2 left-2 size-4 text-faint" />
          <Input aria-label="Find a routine" placeholder="Find a routine" value={query} onChange={(event) => setQuery(event.target.value)} className="pl-8" />
        </div>
      </div>
      {groups.length ? (
        groups.map(([key, items]) => {
          const business = workspaces.find((item) => item.id === key);
          return <Business key={key} org={org} name={business?.name ?? key} color={business?.color} flows={ordered(items)} catalog={catalog} />;
        })
      ) : (
        <p className="py-8 text-center text-body text-muted">Nothing matches. Try another word.</p>
      )}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div role="status" aria-label="Loading" className="grid gap-10">
      <div className="max-w-3xl">
        <Skeleton className="h-11 w-3/4" />
        <Skeleton className="mt-3 h-6 w-2/3" />
      </div>
      <Skeleton className="h-44 rounded-(--radius-card)" />
      <div className="grid gap-4">
        {Array.from({ length: 5 }, (_, index) => (
          <div key={index} className="flex items-center gap-4 py-2">
            <Skeleton className="size-10 rounded-xl" />
            <div className="grid flex-1 gap-2">
              <Skeleton className="w-48" />
              <Skeleton className="w-2/3" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function Flows() {
  const { org } = useParams({ from: '/$org/flows' });
  const { tab = 'flows' } = useSearch({ from: '/$org/flows' });
  const [technical] = useTechnical();
  const flows = useFlows(org);
  const day = useRoutineDay(org);
  const schedules = useSchedules(org);
  const nodes = useNodes(org);
  const workspaces = useWorkspaces(org).data ?? [];
  const remove = useDeleteSchedule(org);
  const [active] = useActiveWorkspace(org);
  const [deleting, setDeleting] = useState<Schedule | null>(null);
  const [adding, setAdding] = useState(false);
  const canOperate = useAccess(org).can('lanes.operate');

  const inScope = (id: string) => !active || id.startsWith(`${active}.`);
  const visible = (flows.data ?? []).filter((item) => inScope(item.id));
  // A schedule with no routine has no business to be in, so it shows only when no business is picked.
  const visibleSchedules = (schedules.data ?? []).filter((item) => (item.flow ? inScope(item.flow) : !active));
  const titles = new Map(visible.map((flow) => [flow.id, routineTitle(flow)]));
  const deletingTitle = deleting ? (deleting.flow ? (titles.get(deleting.flow) ?? routineName(deleting.flow)) : NO_ROUTINE) : '';
  const hasNode = (nodes.data?.length ?? 0) > 0;
  const problems = visible.filter((flow) => flow.stuck || flow.waiting).sort((a, b) => Number(Boolean(b.stuck)) - Number(Boolean(a.stuck)));
  const soonest = visible.filter((flow) => flow.next_at).sort((a, b) => (a.next_at ?? '').localeCompare(b.next_at ?? ''))[0];
  const newRoutine = canOperate ? (
    <Button asChild>
      <Link to="/$org/flows/new" params={{ org }} aria-label="New routine">
        <Plus className="size-4" />
        <span className="max-sm:hidden">New routine</span>
      </Link>
    </Button>
  ) : null;

  if (flows.isPending) return <ListSkeleton />;
  if (isNotServed(flows.error)) return <Card><NotLive /></Card>;
  if (flows.isError) return <Card><ErrorState error={flows.error} onRetry={() => void flows.refetch()} /></Card>;

  if (!visible.length) {
    return (
      <Card>
        <EmptyState
          icon={<Sparkles />}
          title="Nothing runs on its own yet"
          description={hasNode ? 'Start from a job you already do: invoices, a newsletter, new orders. It takes a minute, and you can always change it.' : 'Connect a computer first. A routine runs there, with your logins, and this page shows what it does.'}
          action={
            canOperate && hasNode ? (
              <Button variant="primary" asChild>
                <Link to="/$org/flows/new" params={{ org }}>
                  Start a routine <ArrowRight className="size-4" />
                </Link>
              </Button>
            ) : undefined
          }
        />
      </Card>
    );
  }

  return (
    <div className="grid gap-10 md:gap-12">
      <header className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-4">
        <Hero flows={visible} ran={day.data?.ran_today ?? null} checked={day.data?.checked_today ?? null} soonest={soonest} />
        {newRoutine}
        <nav aria-label="Routines views" className="col-span-2 flex gap-1 text-secondary">
          {[
            { label: 'Today', search: {}, on: tab !== 'schedules' },
            { label: 'The week ahead', search: { tab: 'schedules' as const }, on: tab === 'schedules' },
          ].map((view) => (
            <Link key={view.label} to="/$org/flows" params={{ org }} search={view.search} replace aria-current={view.on ? 'page' : undefined} className={cn('flex h-8 items-center rounded-full px-3 transition-colors duration-(--duration-fast) ease-standard', view.on ? 'bg-pressed text-fg' : 'text-muted hover:bg-hover hover:text-fg')}>
              {view.label}
            </Link>
          ))}
        </nav>
      </header>
      {tab === 'schedules' ? (
        <>
          <WeekAhead org={org} flows={visible} schedules={visibleSchedules} onNew={canOperate ? () => setAdding(true) : undefined} />
          {visibleSchedules.length ? (
            <details open={technical} className="group/more">
              <summary className="cursor-pointer text-body text-muted hover:text-fg">Pause or remove a schedule</summary>
              <div className="mt-4">
                <SchedulesPanel org={org} schedules={visibleSchedules} titles={titles} canOperate={canOperate} onDelete={setDeleting} />
              </div>
            </details>
          ) : null}
          <ScheduleDialog org={org} routines={visible.filter((flow) => !flow.trigger)} open={adding} onOpenChange={setAdding} />
        </>
      ) : (
        <>
          {day.data ? <DayLine org={org} day={day.data} flows={visible} /> : day.isPending ? <Skeleton className="h-36 rounded-(--radius-card)" /> : null}
          {problems.length ? (
            <ul aria-label="Needs a look" className="grid gap-3">
              {problems.map((flow) => (
                <Problem key={flow.id} org={org} flow={flow} business={workspaces.find((item) => item.id === flow.lane.split('/')[0])} />
              ))}
            </ul>
          ) : null}
          <Everything org={org} flows={visible} workspaces={workspaces} />
        </>
      )}
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title="Delete this schedule?"
        description={
          <>
            <span className="text-fg">{deletingTitle}</span> stops running on this schedule. The routine itself stays, and you can run it by hand.
          </>
        }
        confirmLabel="Delete schedule"
        danger
        loading={remove.isPending}
        onConfirm={() => {
          if (!deleting) return;
          const routine = deleting.routine;
          remove.mutate(deleting.id, {
            onSuccess: () => {
              setDeleting(null);
              // A routine's schedule is its routine's switch: deleting it turns the routine off, and says so.
              toast(routine ? `Turned ${deletingTitle} off` : 'Schedule deleted');
            },
          });
        }}
      />
    </div>
  );
}
