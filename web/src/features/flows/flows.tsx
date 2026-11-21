/**
 * Routines, as a person comes to them: what ran, what comes next, and what is stuck. A routine and
 * a workflow are the same thing here. The page opens on one sentence and a line through now, puts
 * anything stuck first, then shows every routine by business, recognisable by its sites and the
 * plain sentence of what it does. Running one or changing its switch happens on its own page, so
 * nothing in this list can be flipped by a slip of the finger.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowRight, Plus, Search, Sparkles } from 'lucide-react';
import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useDeleteSchedule, useFlows, useNodes, useRoutineCatalog, useRoutineDay, useSchedules, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { Flow, RoutineCatalog, Schedule, Workspace } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { Input } from '@/components/ui/field';
import { PageHeader } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { NoValue, Table, TableScroll, Td, Th, Tr } from '@/components/ui/table';
import { SegmentedControl } from '@/components/ui/tabs';
import { EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { useActiveWorkspace } from '@/lib/active-workspace';
import { routineAsk } from '@/lib/ask-first';
import { cn } from '@/lib/cn';
import { plural, timeAgo } from '@/lib/format';
import { routineName } from '@/lib/principal';
import { useTechnical } from '@/lib/technical';
import { DayLine } from './day-line';
import { routineSentence, routineTitle, whenNext } from './routine-words';
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

/** The page's lede: what is stuck or waiting, how the day has gone, and what runs next. The list answers before it lists. */
function Sentence({ flows, ran, checked, soonest }: { flows: Flow[]; ran: number | null; checked: number | null; soonest: Flow | undefined }) {
  const stuck = flows.filter((flow) => flow.stuck).length;
  const waiting = flows.filter((flow) => flow.waiting).length;
  const lead = stuck ? `${plural(stuck, 'routine is', 'routines are')} stuck.` : waiting ? `${plural(waiting, 'routine is', 'routines are')} waiting for you.` : 'Your routines are all running.';
  const rest = stuck || waiting ? `The other ${flows.length - stuck - waiting} are fine.` : 'Nothing needs you.';
  return (
    <>
      {lead} {rest}{' '}
      {ran === null ? null : ran === 0 ? (
        'Nothing has run yet today.'
      ) : (
        <>
          {plural(ran, 'run')} so far today{checked ? `, and ${checked} checked on the real site` : ''}.
        </>
      )}{' '}
      {soonest?.next_at ? (
        <>
          Next: {routineTitle(soonest)}, {whenNext(soonest.next_at)}.
        </>
      ) : null}
    </>
  );
}

/** One stuck or waiting routine: what happened and the one thing to do. */
function Problem({ org, flow, business }: { org: string; flow: Flow; business: Workspace | undefined }) {
  const stuck = flow.stuck;
  const [ws, lane] = (stuck?.lane ?? '').split('/');
  const title = stuck ? `${routineTitle(flow)} stopped at "${stuck.label}"` : `${routineTitle(flow)} is waiting for you`;
  return (
    <Card role="listitem" className={cn('flex flex-col gap-4 border p-5', stuck ? 'border-bad-line' : 'border-line')}>
      <div className="flex items-start gap-3">
        {business ? <BusinessMark name={business.name} color={business.color} size={32} label={business.name} /> : null}
        <div className="min-w-0 flex-1">
          <h3 className="text-label font-semibold text-pretty">{title}</h3>
          <p className="mt-1 text-secondary text-pretty text-muted">{stuck ? stuck.words : "It wrote this week's email and will send it once you say yes."}</p>
        </div>
        <Status chip status={stuck ? { label: 'Stopped', tone: 'bad' } : { label: 'Waits for you', tone: 'live' }} className="shrink-0" />
      </div>
      <div className="mt-auto flex flex-wrap justify-end gap-2">
        <Button variant={stuck ? 'secondary' : 'ghost'} asChild>
          <Link to="/$org/flows/$flow" params={{ org, flow: flow.id }}>
            {stuck ? 'See what happened' : 'Open'}
          </Link>
        </Button>
        {stuck?.fix && ws && lane ? (
          <Button asChild>
            <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name: lane }}>
              {stuck.fix}
            </Link>
          </Button>
        ) : !stuck ? (
          <Button asChild>
            <Link to="/$org/needs-you" params={{ org }}>
              Look and decide
            </Link>
          </Button>
        ) : null}
      </div>
    </Card>
  );
}

/** How it runs, as a chip: asks you first, on its own, or paused. */
function HowChip({ flow, canDecide }: { flow: Flow; canDecide: boolean }) {
  if (flow.enabled === false) return <Status chip status={{ label: 'Paused', tone: 'idle' }} />;
  const ask = routineAsk(flow, canDecide);
  if (!ask) return <NoValue />;
  return <Status chip status={{ label: ask.word, tone: ask.asks ? 'live' : 'ok' }} />;
}

function Business({ org, name, color, flows, catalog }: { org: string; name: string; color?: string | null; flows: Flow[]; catalog: RoutineCatalog | undefined }) {
  const [all, setAll] = useState(false);
  const navigate = useNavigate();
  const canDecide = useAccess(org).can('approvals.decide');
  const listed = all ? flows : flows.slice(0, SHOWN);
  return (
    <section aria-label={name} className="grid gap-3">
      <div className="flex items-center gap-3">
        <BusinessMark name={name} color={color} size={24} />
        <h2 className="text-section-title">{name}</h2>
        <span className="text-secondary text-faint">{plural(flows.length, 'routine')}</span>
      </div>
      <Card className="overflow-hidden">
        <TableScroll>
          <Table stack aria-label={`Routines of ${name}`}>
            <thead>
              <tr>
                <Th>Routine</Th>
                <Th phone="hide">Last</Th>
                <Th phone="hide">Next</Th>
                <Th>How</Th>
              </tr>
            </thead>
            <tbody>
              {listed.map((flow) => {
                const sentence = routineSentence(flow, catalog);
                const next = flow.enabled === false ? 'Paused' : flow.next_at ? `Next ${whenNext(flow.next_at)}` : flow.trigger?.kind === 'schedule' ? null : 'When it is needed';
                return (
                  <Tr key={flow.id} onActivate={() => void navigate({ to: '/$org/flows/$flow', params: { org, flow: flow.id } })} className={flow.enabled === false ? '[&>td]:opacity-70' : undefined}>
                    <Td main className="whitespace-normal">
                      <Link
                        to="/$org/flows/$flow"
                        params={{ org, flow: flow.id }}
                        aria-label={`${routineTitle(flow)}: ${sentence}`}
                        tabIndex={-1}
                        onClick={(event) => event.stopPropagation()}
                        className="grid min-w-0 py-1"
                      >
                        <span className="truncate font-medium text-fg">{routineTitle(flow)}</span>
                        <span title={sentence} className="line-clamp-2 text-secondary text-pretty text-muted">
                          {sentence}
                        </span>
                      </Link>
                    </Td>
                    <Td phone="hide" className="w-64 max-w-64 whitespace-normal">
                      <span className="line-clamp-2 text-secondary text-fg">
                        {flow.latest ? `${flow.latest.status === 'failed' ? 'Did not work' : flow.latest.result}, ${timeAgo(flow.latest.at).toLowerCase()}` : <span className="text-faint">Has not run yet</span>}
                      </span>
                    </Td>
                    <Td phone="hide" className="text-secondary text-muted tabular">
                      {next ?? <NoValue />}
                    </Td>
                    <Td>
                      <HowChip flow={flow} canDecide={canDecide} />
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        </TableScroll>
        {flows.length > SHOWN ? (
          <div className="border-t border-line px-3 py-2">
            <Button variant="ghost" size="sm" onClick={() => setAll(!all)}>
              {all ? 'Show fewer' : `Show ${flows.length - SHOWN} more`}
            </Button>
          </div>
        ) : null}
      </Card>
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
    <div className="grid gap-8">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <SegmentedControl
          label="Show"
          size="md"
          value={show}
          onValueChange={setShow}
          options={pills
            .filter((pill) => pill.count > 0 || pill.value === 'all')
            .map((pill) => ({
              value: pill.value,
              label: (
                <>
                  {pill.label} <span className="tabular font-normal text-faint">{pill.count}</span>
                </>
              ),
            }))}
        />
        <div className="relative sm:w-64">
          <Search aria-hidden="true" className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-faint" />
          <Input aria-label="Find a routine" placeholder="Find a routine" value={query} onChange={(event) => setQuery(event.target.value)} className="pl-9" />
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
    <Button variant="primary" asChild>
      <Link to="/$org/flows/new" params={{ org }} aria-label="New routine">
        <Plus className="size-4" />
        <span className="max-sm:hidden">New routine</span>
      </Link>
    </Button>
  ) : null;

  // Today or the week ahead: two views of the same routines, kept in the address.
  const views = (
    <nav aria-label="Routines views" className="inline-flex gap-px justify-self-start rounded-full border border-line bg-inset p-1">
      {[
        { label: 'Today', search: {}, on: tab !== 'schedules' },
        { label: 'The week ahead', search: { tab: 'schedules' as const }, on: tab === 'schedules' },
      ].map((view) => (
        <Link
          key={view.label}
          to="/$org/flows"
          params={{ org }}
          search={view.search}
          replace
          aria-current={view.on ? 'page' : undefined}
          className={cn(
            'flex h-8 items-center rounded-full border px-3 text-label transition-colors duration-(--duration-fast) ease-standard',
            view.on ? 'border-line bg-card font-semibold text-fg shadow-lift' : 'border-transparent text-muted hover:text-fg',
          )}
        >
          {view.label}
        </Link>
      ))}
    </nav>
  );

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
    <>
      <PageHeader
        title="Routines"
        description={<Sentence flows={visible} ran={day.data?.ran_today ?? null} checked={day.data?.checked_today ?? null} soonest={soonest} />}
        actions={newRoutine}
      />
      <div className="grid gap-8">
      {/* What is stuck (today only), then the views, always in the same place so switching keeps the focus on them. */}
      {tab !== 'schedules' && problems.length ? (
        <ul aria-label="Needs a look" className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {problems.map((flow) => (
            <Problem key={flow.id} org={org} flow={flow} business={workspaces.find((item) => item.id === flow.lane.split('/')[0])} />
          ))}
        </ul>
      ) : null}
      {views}
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
          {day.data ? <DayLine org={org} day={day.data} flows={visible} /> : day.isPending ? <Skeleton className="h-36 rounded-xl" /> : null}
          <Everything org={org} flows={visible} workspaces={workspaces} />
        </>
      )}
      </div>
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
    </>
  );
}
