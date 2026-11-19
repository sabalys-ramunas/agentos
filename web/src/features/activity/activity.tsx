/**
 * History: everything the agents did, told as sentences with proof (structure.md, section 7).
 * Days are headings, each thing done is one line a person understands, problems say what did not
 * happen and what to do, and looking-only runs fold into one line a day. The stories come told
 * from the cloud (`GET /history`). With Technical details on, the raw log, the chart and every
 * filter are one switch away.
 *
 * The page reads its org and search from whatever route mounts it, so it works at /activity and
 * at /history alike.
 */
import { SegmentedControl } from '@/components/ui/tabs';
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowUp, Download, ListFilter } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useActivity, useHistory, useRetention, useWorkspaces } from '@/api/hooks';
import type { ActivityFilters } from '@/api/client';
import type { ActivityRange, Effect, HistoryDay, HistoryPage, RunStatus, StoryKind, TraceEvent } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Menu, MenuContent, MenuItem, MenuTrigger } from '@/components/ui/menu';
import { PageHeader } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { effectLabel, runStatus } from '@/lib/labels';
import { useTechnical } from '@/lib/technical';
import { usePrincipalName } from '@/lib/use-principal';
import { PLACES } from '@/lib/vocabulary';
import { ActivityTable, RunsChart } from './activity-table';
import { activityCsv } from './csv';
import { dayLabel, quietLine } from './feed';
import { DaySection, WorkingNow } from './history-feed';
import { Chosen, RunDrawer } from './run-drawer';
import { kindOf, KINDS } from './story-view';

const ALL = 'all';
const EFFECTS: Effect[] = ['read', 'draft', 'commit', 'pay', 'irreversible', 'exec'];
const STATUSES: RunStatus[] = ['succeeded_verified', 'succeeded_unverified', 'partial', 'failed', 'cancelled'];
const RANGES: { value: ActivityRange; label: string }[] = [
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
];
/** Days in a page of History. A busy fleet does hundreds of things a day. */
const DAYS_A_PAGE = 2;

export interface ActivitySearch {
  workspace?: string;
  lane?: string;
  principal?: string;
  effect?: string;
  status?: string;
  run?: string;
  range?: string;
  from?: string;
  to?: string;
  /** A kind of work: `sent`, `money`, `forms`, `research`, `shop`, `files`. */
  kind?: string;
}

/**
 * The calm filter: everything, a kind of work, or problems. Kinds come from what the stories are
 * (social posting is one use case of many), and only kinds that happened are offered. Problems
 * rides on `status=failed`, so the raw log under Technical details agrees with it; old links with
 * `effect=commit` or `effect=pay` still land on their kind.
 */
type Show = 'everything' | 'problems' | StoryKind;

function showOf(search: ActivitySearch): Show {
  const kind = KINDS.find((item) => item.kind === search.kind)?.kind;
  if (kind) return kind;
  if (search.status === 'failed' && !search.effect) return 'problems';
  if (search.effect === 'commit' && !search.status) return 'sent';
  if (search.effect === 'pay' && !search.status) return 'money';
  return 'everything';
}

const labelOf = (show: Show) =>
  show === 'everything' ? 'Everything' : show === 'problems' ? 'Problems' : (KINDS.find((item) => item.kind === show)?.label ?? show);

function emptyDay(show: Show): string {
  if (show === 'everything') return 'Nothing went out this day.';
  if (show === 'problems') return 'No problems this day.';
  return `Nothing in ${labelOf(show).toLowerCase()} this day.`;
}

function exportCsv(rows: TraceEvent[]) {
  const body = activityCsv(rows);
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([body], { type: 'text/csv' }));
  link.download = 'agentos-history.csv';
  link.click();
  URL.revokeObjectURL(link.href);
}

const WIDE = '(min-width: 1280px)';
function subscribeWide(listener: () => void) {
  const query = window.matchMedia(WIDE);
  query.addEventListener('change', listener);
  return () => query.removeEventListener('change', listener);
}
/** At 1280 and up, the chosen story sits beside the list instead of in a drawer. */
const useWide = () => useSyncExternalStore(subscribeWide, () => window.matchMedia(WIDE).matches, () => false);

function FeedSkeleton() {
  return (
    <div role="status" aria-label="Loading" className="grid gap-2">
      <Skeleton className="h-4 w-40" />
      <div className="overflow-hidden rounded-xl border border-line">
        {Array.from({ length: 5 }, (_, index) => (
          <div key={index} className="flex items-center gap-4 border-b border-line px-4 py-3 last:border-b-0">
            <Skeleton className="hidden w-10 md:block" />
            <Skeleton className="size-14 shrink-0 rounded-lg" />
            <div className="grid flex-1 gap-2">
              <Skeleton className="h-3 w-2/3" />
              <Skeleton className="h-3 w-1/3" />
            </div>
            <Skeleton className="hidden h-6 w-24 rounded-full sm:block" />
          </div>
        ))}
      </div>
    </div>
  );
}

/** A picture frame on the horizon, waiting for its first picture (visual.md P6). */
function EmptyFrame() {
  return (
    <svg viewBox="0 0 200 120" width={200} height={120} aria-hidden="true" className="text-line-strong">
      <path d="M8 96h184" fill="none" stroke="currentColor" strokeWidth={1.25} strokeLinecap="round" />
      <rect x={62} y={22} width={76} height={60} rx={6} fill="var(--background)" stroke="currentColor" strokeWidth={1.25} />
      <rect x={70} y={30} width={60} height={44} rx={3} fill="none" stroke="currentColor" strokeWidth={1.25} strokeDasharray="3 3" />
      <path d="M86 62l9-10 8 8 6-6 13 14" fill="none" stroke="var(--ok-dot)" strokeWidth={1.25} strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={84} cy={44} r={4} fill="none" stroke="var(--ok-dot)" strokeWidth={1.25} />
      <path d="M100 82v14" fill="none" stroke="currentColor" strokeWidth={1.25} />
    </svg>
  );
}

/** A stretch of days with nothing in it, said once and quietly. */
function Quiet({ children }: { children: string }) {
  return (
    <p className="flex items-center gap-3 px-1 text-secondary text-faint">
      <span aria-hidden="true" className="h-px w-6 bg-line" />
      {children}
    </p>
  );
}

const labelOfDate = (date: string) => dayLabel(`${date}T12:00:00`);

/** The raw log, for Technical details. */
function TechnicalLog({ org, search, onOpen }: { org: string; search: ActivitySearch; onOpen: (id: string) => void }) {
  const filters: ActivityFilters = {
    workspace: search.workspace,
    lane: search.lane,
    principal: search.principal,
    effect: search.effect,
    status: search.status,
    from: search.from,
    to: search.to,
  };
  const activity = useActivity(org, filters);
  const rows = useMemo(() => activity.data?.pages.flatMap((page) => page.items) ?? [], [activity.data]);
  // New rows wait behind a pill, so nothing moves under the reader's eye.
  const [pinned, setPinned] = useState<string | null>(null);
  const waiting = Math.max(0, pinned ? rows.findIndex((row) => row.id === pinned) : 0);
  if (pinned === null && rows[0]) setPinned(rows[0].id);
  const shown = waiting ? rows.slice(waiting) : rows;
  if (activity.isPending) return <FeedSkeleton />;
  if (activity.isError) return <ErrorState error={activity.error} onRetry={() => void activity.refetch()} />;
  return (
    <div className="relative">
      {waiting > 0 ? (
        <div className="absolute inset-x-0 -top-3 z-10 flex justify-center">
          <Button size="sm" className="rounded-full shadow-popover" icon={<ArrowUp className="size-4" />} onClick={() => setPinned(rows[0]?.id ?? null)}>
            {plural(waiting, 'new event')}
          </Button>
        </div>
      ) : null}
      <Card className="overflow-hidden">
        {rows.length === 0 ? (
          <EmptyState icon={<ListFilter />} title="Nothing matches these filters" description="Try a wider filter, or clear them to see everything." />
        ) : (
          <ActivityTable org={org} rows={shown} onOpen={onOpen} />
        )}
        {activity.hasNextPage ? (
          <div className="flex justify-center border-t border-line p-2">
            <Button size="sm" variant="ghost" loading={activity.isFetchingNextPage} onClick={() => void activity.fetchNextPage()}>
              Load older
            </Button>
          </div>
        ) : null}
      </Card>
      <div className="mt-2 flex justify-end">
        <Button size="sm" variant="ghost" icon={<Download className="size-4" />} disabled={rows.length === 0} onClick={() => exportCsv(rows)}>
          These rows as CSV
        </Button>
      </div>
    </div>
  );
}

export function Activity() {
  const { org = '' } = useParams({ strict: false }) as { org?: string };
  const search = useSearch({ strict: false }) as ActivitySearch;
  const navigate = useNavigate();
  const setSearch = useCallback(
    (patch: Partial<ActivitySearch> | null, replace = false) =>
      void navigate({ to: '.', replace, search: (patch === null ? {} : (current: ActivitySearch) => ({ ...current, ...patch })) as never }),
    [navigate],
  );
  const [technical] = useTechnical();
  const [view, setView] = useState<'story' | 'table'>('story');
  const table = technical && view === 'table';
  const wide = useWide();

  const retention = useRetention(org).data;
  const workspaces = useWorkspaces(org).data;
  const show = showOf(search);
  // Problems the cloud filters; a kind of work is kept here, from the days already read.
  const history = useHistory(org, { workspace: search.workspace, show: show === 'problems' ? 'problems' : 'everything' }, DAYS_A_PAGE);
  const pages = useMemo<HistoryPage[]>(() => history.data?.pages ?? [], [history.data]);
  const allDays = useMemo(() => pages.flatMap((page) => page.days), [pages]);
  const kindsSeen = useMemo(() => {
    const seen = new Set<StoryKind>();
    for (const story of [...(pages[0]?.working_now ?? []), ...allDays.flatMap((day) => day.stories)]) {
      const kind = kindOf(story);
      if (kind) seen.add(kind);
    }
    return seen;
  }, [pages, allDays]);
  const days = useMemo<HistoryDay[]>(() => {
    if (show === 'everything' || show === 'problems') return allDays;
    return allDays
      .map((day) => ({ ...day, stories: day.stories.filter((story) => kindOf(story) === show), reads: null }))
      .filter((day) => day.stories.length > 0);
  }, [allDays, show]);
  const working = useMemo(() => {
    const now = pages[0]?.working_now ?? [];
    // Under a filter, only what is of that kind is working "now" on this page.
    if (show === 'everything') return now;
    if (show === 'problems') return [];
    return now.filter((story) => kindOf(story) === show);
  }, [pages, show]);
  const counts = pages[0]?.counts;
  const stories = useMemo(() => new Map([...working, ...days.flatMap((day) => day.stories)].map((story) => [story.id, story])), [working, days]);
  const nameOf = usePrincipalName(org);

  // On a wide screen the newest thing done is open beside the list from the start.
  const first = days.find((day) => day.stories.length > 0)?.stories[0]?.id ?? null;
  const selected = search.run ?? (wide && !table ? first : null);
  const select = (id: string) => setSearch({ run: id }, wide);

  const setFilter = (key: keyof ActivitySearch, value: string) =>
    setSearch({ [key]: value === ALL ? undefined : value, ...(key === 'workspace' ? { lane: undefined } : {}), run: undefined });
  const setShow = (value: Show) => {
    if (value === 'problems') setSearch({ status: 'failed', effect: undefined, kind: undefined, run: undefined });
    else if (value === 'everything') setSearch({ status: undefined, effect: undefined, kind: undefined, run: undefined });
    else setSearch({ kind: value, status: undefined, effect: undefined, run: undefined });
  };
  // On a phone the filter row scrolls sideways; keep the chosen kind in view.
  useEffect(() => {
    document.querySelector('[role="radiogroup"][aria-label="Show"] [data-state="checked"]')?.scrollIntoView({ inline: 'center', block: 'nearest' });
  }, [show]);
  const showOptions: Show[] = ['everything', ...KINDS.map((item) => item.kind).filter((kind) => kindsSeen.has(kind) || kind === show), 'problems'];
  const narrowed = Boolean(search.workspace) || show !== 'everything';
  const technicalFilters = Boolean(search.lane || search.principal || search.from || search.to || (search.effect && showOf({ effect: search.effect }) === 'everything') || (search.status && search.status !== 'failed'));

  // Only for the raw log: who started things, by id, from what the log has shown.
  const principals = useMemo(() => {
    const seen = new Map([...stories.values()].map((story) => [story.who.id, { kind: story.who.kind, id: story.who.id }] as const));
    if (search.principal && !seen.has(search.principal)) seen.set(search.principal, { kind: 'agent', id: search.principal });
    return [...seen].map(([value, principal]) => ({ value, label: nameOf(principal) })).sort((a, b) => a.label.localeCompare(b.label));
  }, [stories, search.principal, nameOf]);
  const laneOptions = (workspaces ?? [])
    .filter((item) => !search.workspace || item.id === search.workspace)
    .flatMap((item) => item.lanes.map((lane) => ({ value: lane.id, label: lane.id })));
  const filterSelect = (key: keyof ActivityFilters, label: string, options: { value: string; label: string }[]) => (
    <Select
      size="sm"
      aria-label={label}
      className={cn('w-36 shrink-0 sm:w-40', search[key] && 'max-sm:order-first')}
      value={search[key] ?? ALL}
      onValueChange={(value) => setFilter(key, value)}
      options={[{ value: ALL, label: `${label}: all` }, ...options]}
    />
  );

  const kept = retention ? ` Kept for ${retention.kept_days} days.` : '';
  const newestIsToday = days[0]?.label === 'Today';
  const problems = counts?.problems ?? 0;

  let feed;
  if (history.isPending) feed = <FeedSkeleton />;
  else if (history.isError) feed = <ErrorState error={history.error} onRetry={() => void history.refetch()} />;
  else if (days.length === 0 && (narrowed || working.length === 0))
    feed = narrowed ? (
      <Card>
        <EmptyState
          icon={<ListFilter />}
          title={show === 'problems' ? 'No problems lately' : 'Nothing like that lately'}
          description={
            show === 'problems'
              ? 'Everything your agents tried in these days worked. Show everything to see all of it.'
              : show === 'everything'
                ? 'Nothing from this workspace in these days. Look further back, or show everything.'
                : 'Nothing of this kind in these days. Look further back, or show everything.'
          }
          action={
            <div className="flex flex-wrap justify-center gap-2">
              {history.hasNextPage ? (
                <Button loading={history.isFetchingNextPage} onClick={() => void history.fetchNextPage()}>
                  Look further back
                </Button>
              ) : null}
              <Button onClick={() => setSearch(null)}>Show everything</Button>
            </div>
          }
        />
      </Card>
    ) : (
      <Card className="flex flex-col items-center px-6 py-12 text-center">
        <EmptyFrame />
        <h2 className="mt-4 text-section-title">Nothing has gone out yet</h2>
        <p className="mt-1 max-w-sm text-body text-muted">
          When an agent sends an email, posts, pays an invoice, fills in a form or looks something up, you will see it here, with how we know it worked.
        </p>
      </Card>
    );
  else
    feed = (
      <div className="grid gap-6" data-feed="">
        {working.length > 0 ? <WorkingNow org={org} stories={working} /> : null}
        {!newestIsToday ? (
          <section aria-label="Today" className="grid gap-2">
            <h2 className="text-body font-semibold">Today</h2>
            <p className="rounded-xl border border-dashed border-line px-4 py-3 text-secondary text-muted">
              {working.length > 0 ? 'Nothing finished yet today. Something is on its way.' : show === 'problems' ? 'No problems today.' : 'Nothing yet today. The agents are resting.'}
            </p>
          </section>
        ) : null}
        {days.map((day, index) => {
          const older = days[index + 1];
          const quiet = older ? quietLine(day.date, older.date, labelOfDate) : null;
          return (
            <div key={day.date} className="grid gap-6">
              <DaySection org={org} day={day} selected={selected} onSelect={select} emptyNote={emptyDay(show)} filtered={show !== 'everything'} />
              {quiet ? <Quiet>{quiet}</Quiet> : null}
            </div>
          );
        })}
        {history.hasNextPage ? (
          <div className="flex justify-center">
            <Button size="sm" variant="ghost" loading={history.isFetchingNextPage} onClick={() => void history.fetchNextPage()}>
              Show older days
            </Button>
          </div>
        ) : days.length > 0 ? (
          <Quiet>{retention ? `That is everything. History is kept for ${retention.kept_days} days.` : 'That is everything.'}</Quiet>
        ) : null}
      </div>
    );

  return (
    <>
      <PageHeader
        title={PLACES.history.word}
        description={`Everything your agents did, and how each one went.${kept}`}
        actions={
          <div className="flex items-center gap-2">
          <Button asChild>
            <Link to="/$org/activity/proof" params={{ org }}>
              Proof
            </Link>
          </Button>
          {retention?.can_export ? (
            <Menu>
              <MenuTrigger asChild>
                <Button className="hidden sm:inline-flex" icon={<Download className="size-4" />}>
                  Download
                </Button>
              </MenuTrigger>
              <MenuContent align="end">
                <MenuItem asChild>
                  <Link to="/$org/settings/retention" params={{ org }}>
                    Everything kept, as a file
                  </Link>
                </MenuItem>
              </MenuContent>
            </Menu>
          ) : undefined}
          </div>
        }
      />
      {/* The filters wrap on a phone: a row that scrolls sideways clipped the workspace filter at 375 ("All worksp"). */}
      <div className="mb-6 flex flex-wrap items-center gap-2">
        <SegmentedControl
          label="Show"
          // Five choices: on a phone they scroll sideways inside the control, never the page.
          className="scrollbar-none max-w-full overflow-x-auto [&>*]:shrink-0"
          value={show}
          options={showOptions.map((value) => ({ value, label: value === 'problems' && problems > 0 ? `Problems ${problems}` : labelOf(value) }))}
          onValueChange={setShow}
        />
        <Select
          size="sm"
          aria-label="Workspace"
          className="w-44 shrink-0"
          value={search.workspace ?? ALL}
          onValueChange={(value) => setFilter('workspace', value)}
          options={[{ value: ALL, label: 'All workspaces' }, ...(workspaces ?? []).map((item) => ({ value: item.id, label: item.name }))]}
        />
        {technical ? (
          <SegmentedControl
            label="View"
            value={view}
            options={[
              { value: 'story', label: 'Stories' },
              { value: 'table', label: 'Raw log' },
            ]}
            onValueChange={setView}
          />
        ) : null}
        {table ? (
          <>
            {filterSelect('lane', 'Lane', laneOptions)}
            {filterSelect('principal', 'Started by', principals)}
            {filterSelect('effect', 'Effect', EFFECTS.map((value) => ({ value, label: effectLabel[value] })))}
            {filterSelect('status', 'Status', STATUSES.map((value) => ({ value, label: runStatus[value].label })))}
          </>
        ) : null}
        {narrowed || (table && technicalFilters) ? (
          <Button size="sm" variant="ghost" className="max-sm:order-first" onClick={() => setSearch(null)}>
            Show everything
          </Button>
        ) : null}
      </div>
      {table ? (
        <>
          <div className="mb-4 flex items-center gap-2">
            <SegmentedControl
              label="Time range"
              value={(search.range ?? '7d') as ActivityRange}
              options={RANGES}
              onValueChange={(value) => setSearch({ range: value, from: undefined, to: undefined }, true)}
            />
          </div>
          <RunsChart
            org={org}
            range={(search.range ?? '7d') as ActivityRange}
            scope={{ workspace: search.workspace, lane: search.lane }}
            from={search.from}
            to={search.to}
            onWindow={(window) => setSearch(window, true)}
          />
          <TechnicalLog org={org} search={search} onOpen={(id) => setSearch({ run: id })} />
        </>
      ) : (
        <div className={cn(wide && stories.size > 0 && 'grid grid-cols-[minmax(0,1fr)_400px] items-start gap-8')}>
          <div className="min-w-0">{feed}</div>
          {wide && stories.size > 0 ? (
            <aside aria-label="What happened" className="sticky top-6 max-h-[calc(100dvh-48px)] overflow-y-auto rounded-xl border border-line bg-card p-5">
              {selected ? <Chosen org={org} id={selected} stories={stories} /> : <p className="text-body text-muted">Pick something on the left to see what it did and the proof.</p>}
            </aside>
          ) : null}
        </div>
      )}
      <RunDrawer org={org} id={wide && !table ? null : (search.run ?? null)} stories={stories} onClose={() => setSearch({ run: undefined })} />
    </>
  );
}
