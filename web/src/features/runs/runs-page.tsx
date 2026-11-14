/**
 * Runs: every `agentos run` job the person may see, newest first, as the Mono table. Kind, title and
 * command, status, branch and commit when the client sent them, who started it and where, the test
 * totals read from the output, how long it took and when. Routine and agent runs live in History.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { Copy, ListFilter, Lock, Square, SquareTerminal, Unplug, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useCancelJob, useJobs } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { Job } from '@/api/types';
import { Chip } from '@/components/ui/badge';
import { BranchPill } from '@/components/ui/branch';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { Input } from '@/components/ui/field';
import { KindTile } from '@/components/ui/kind';
import { CodeBlock, CopyButton, PageHeader } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { SelectionBar } from '@/components/ui/selection-bar';
import { EmptyState, ErrorState, NotLive } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { CellTitle, NoValue, RowActions, Table, TableEmpty, TableScroll, TableSkeleton, Td, Th, Tr } from '@/components/ui/table';
import type { Density, SortDirection } from '@/components/ui/table';
import { SegmentedControl } from '@/components/ui/tabs';
import { Checkbox } from '@/components/ui/toggle';
import { dateTime, plural, timeAgo } from '@/lib/format';
import { PLACES } from '@/lib/vocabulary';
import { createStored } from '@/lib/store';
import { useNow } from '@/lib/use-now';
import { KIND_WORD } from './kind';
import { RUN_KINDS, RUN_STATES } from './search';
import type { RunKind, RunSort, RunStateFilter, RunsSearch } from './search';
import { JOB_STATUS, durationMs, durationOf, logsCommand, matchesState, runKind, summaryOf, titleOf } from './words';

/** The node lists at most this many; the page asks for all of them and filters here. */
const LIMIT = 200;
const density = createStored<'default' | 'compact'>('agentos.runs.density', 'default');

const STATE_WORD: Record<RunStateFilter, string> = { running: 'Running', passed: 'Passed', failed: 'Failed', stopped: 'Stopped early' };
const RANGE_MS = { '1h': 3_600_000, '24h': 86_400_000, '7d': 7 * 86_400_000, '30d': 30 * 86_400_000 } as const;
const RANGES = [
  { value: 'any', label: 'Any time' },
  { value: '1h', label: 'Last hour' },
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
];
/** Running first, then what went wrong, then what passed. */
const STATE_ORDER: Record<Job['state'], number> = { running: 0, failed: 1, lost: 2, timed_out: 3, cancelled: 4, passed: 5 };

const COLUMNS = ['', 'Run', 'Result', 'Branch', 'Who and where', 'Passed', 'Failed', 'Took', 'Started', ''];

export function filterJobs(jobs: Job[], search: RunsSearch, now = Date.now()): Job[] {
  const needle = search.q?.trim().toLowerCase();
  return jobs.filter((job) => {
    if (search.state && !matchesState(job, search.state)) return false;
    if (search.kind && runKind(job) !== search.kind) return false;
    if (search.machine && job.node !== search.machine) return false;
    if (search.lane && job.lane !== search.lane) return false;
    if (search.by && job.started_by !== search.by) return false;
    if (search.range && now - Date.parse(job.started_at) > RANGE_MS[search.range]) return false;
    if (needle) {
      const hay = [job.id, job.command, job.lane, job.node, job.started_by, job.source?.branch, job.source?.commit, titleOf(job), job.note]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();
      if (!hay.includes(needle)) return false;
    }
    return true;
  });
}

export function sortJobs(jobs: Job[], sort: RunSort = 'started', dir: SortDirection = 'desc', now = Date.now()): Job[] {
  const value = (job: Job): number => {
    if (sort === 'duration') return durationMs(job, now);
    if (sort === 'state') return STATE_ORDER[job.state];
    if (sort === 'failed') return job.tests ? job.tests.failed : -1;
    return Date.parse(job.started_at);
  };
  const sign = dir === 'asc' ? 1 : -1;
  return [...jobs].sort((a, b) => (value(a) - value(b)) * sign || b.started_at.localeCompare(a.started_at));
}

export function RunsPage() {
  const { org } = useParams({ from: '/$org/runs' });
  const query = useJobs(org, { limit: LIMIT });
  const jobs = query.data?.jobs;
  const now = useNow(!!jobs?.some((job) => job.state === 'running'));

  return (
    <div className="grid gap-6">
      <PageHeader
        title={PLACES.runs.word}
        description={
          <>
            Tests, builds and dev servers that you and your agents start on your machines with{' '}
            <code className="rounded-sm border border-line bg-hover px-1 font-mono text-code">agentos run</code>.
            {query.data?.available && jobs?.length ? <span className="text-fg"> {summaryOf(jobs, now)}</span> : null}
            <span className="mt-1 block text-14 text-faint">
              Routines and the runs agents make on accounts are in{' '}
              <Link to="/$org/activity" params={{ org }} className="font-medium text-muted underline underline-offset-2 hover:text-fg">
                History
              </Link>
              .
            </span>
          </>
        }
      />
      <RunsBody org={org} query={query} jobs={jobs} />
    </div>
  );
}

function RunsBody({ org, query, jobs }: { org: string; query: ReturnType<typeof useJobs>; jobs: Job[] | undefined }) {
  if (query.isPending) {
    return (
      <Card role="status" aria-label="Loading runs">
        <TableSkeleton columns={COLUMNS.slice(1, 9)} />
      </Card>
    );
  }
  if (isNotServed(query.error)) {
    return (
      <Card>
        <NotLive />
      </Card>
    );
  }
  if (query.error instanceof ApiError && query.error.status === 403) {
    return (
      <Card>
        <EmptyState
          icon={<Lock />}
          title="Runs are not shown to your role"
          description="You see the runs in the workspaces your role includes, and this role includes none. An owner or admin can change that."
        />
      </Card>
    );
  }
  if (query.isError || !query.data) {
    return (
      <Card>
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Card>
    );
  }
  if (!query.data.available) {
    return (
      <Card>
        <EmptyState
          icon={<Unplug />}
          title="This machine runs no commands"
          description={query.data.reason ?? 'It has no terminal lanes switched on, so nothing can run on it with agentos run.'}
        />
      </Card>
    );
  }
  if (!jobs?.length) {
    return (
      <Card>
        <EmptyState
          icon={<SquareTerminal />}
          title="No runs yet"
          description="When you or an agent runs a command on a machine, it shows here with its live output, what passed and what failed."
          action={<CodeBlock code="agentos run -- pnpm test" label="Copy the command" className="text-left" />}
        />
      </Card>
    );
  }
  return <RunsTable org={org} jobs={jobs} capped={jobs.length >= LIMIT} />;
}

function RunsTable({ org, jobs, capped }: { org: string; jobs: Job[]; capped: boolean }) {
  const search = useSearch({ from: '/$org/runs' });
  const navigate = useNavigate({ from: '/$org/runs' });
  const access = useAccess(org);
  const cancel = useCancelJob(org);
  const rows = density.use();
  const anyRunning = jobs.some((job) => job.state === 'running');
  const now = useNow(anyRunning);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [asking, setAsking] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  const setSearch = (patch: Partial<RunsSearch>) => void navigate({ search: (prev) => ({ ...prev, ...patch }), replace: true });

  const filtered = useMemo(() => filterJobs(jobs, search, now), [jobs, search, now]);
  const shown = useMemo(() => sortJobs(filtered, search.sort, search.dir, now), [filtered, search.sort, search.dir, now]);
  const sort = search.sort ?? 'started';
  const dir = search.dir ?? 'desc';
  const sortProps = (key: RunSort) => ({
    sort: sort === key ? dir : undefined,
    onSort: (next: SortDirection) => setSearch({ sort: key === 'started' ? undefined : key, dir: key === 'started' && next === 'desc' ? undefined : next }),
  });

  // Counts for the chips, with every other filter applied.
  const stateCount = (state: RunStateFilter) => filterJobs(jobs, { ...search, state }, now).length;
  const kindCount = (kind: RunKind) => filterJobs(jobs, { ...search, kind }, now).length;

  const picked = shown.filter((job) => selected.has(job.id));
  const pickedRunning = picked.filter((job) => job.state === 'running');
  const canOperate = access.can('lanes.operate');
  const allPicked = shown.length > 0 && picked.length === shown.length;
  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const filtering = !!(search.state || search.kind || search.q || search.machine || search.lane || search.by || search.range);

  const cancelPicked = async () => {
    setCancelling(true);
    const results = await Promise.allSettled(pickedRunning.map((job) => cancel.mutateAsync(job.id)));
    setCancelling(false);
    setAsking(false);
    const failed = results.filter((result) => result.status === 'rejected').length;
    const done = results.length - failed;
    if (done) toast.success(`${plural(done, 'run')} cancelled. Their output so far is kept.`);
    if (failed) toast.error(`${plural(failed, 'run')} could not be cancelled. Nothing was changed for ${failed === 1 ? 'it' : 'them'}.`);
    setSelected(new Set());
  };

  return (
    <>
      <Card className="overflow-hidden">
        <CardHeader
          title="Every run"
          meta={filtering ? `${shown.length} of ${jobs.length}` : capped ? `the newest ${jobs.length}` : plural(jobs.length, 'run')}
          action={
            <SegmentedControl
              label="Row height"
              value={rows}
              onValueChange={(value) => density.set(value)}
              options={[
                { value: 'default', label: 'Comfortable' },
                { value: 'compact', label: 'Compact' },
              ]}
              className="max-md:hidden"
            />
          }
        />
        <div className="grid gap-3 border-b border-line px-5 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-0 flex-1 basis-56 md:max-w-80">
              <label htmlFor="runs-search" className="sr-only">
                Search runs
              </label>
              <ListFilter aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2 size-4 -translate-y-1/2 text-faint" />
              <Input
                id="runs-search"
                type="search"
                value={search.q ?? ''}
                placeholder="Search a command, branch, machine or id"
                className="pl-8"
                onChange={(event) => setSearch({ q: event.target.value || undefined })}
              />
            </div>
            <Select
              size="sm"
              aria-label="How far back"
              value={search.range ?? 'any'}
              onValueChange={(value) => setSearch({ range: value === 'any' ? undefined : (value as RunsSearch['range']) })}
              options={RANGES}
              className="w-40"
            />
            {(['machine', 'lane', 'by'] as const).map((key) =>
              search[key] ? (
                <Chip
                  key={key}
                  pressed
                  label={
                    <>
                      {key === 'machine' ? 'On ' : key === 'lane' ? 'In ' : 'By '}
                      <span className={key === 'by' ? '' : 'font-mono'}>{search[key]}</span>
                      <X aria-hidden="true" className="size-3.5" />
                    </>
                  }
                  aria-label={`Stop showing only ${key === 'machine' ? 'the machine' : key === 'lane' ? 'the lane' : 'runs by'} ${search[key]}`}
                  onClick={() => setSearch({ [key]: undefined })}
                />
              ) : null,
            )}
            {filtering ? (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setSearch({ state: undefined, kind: undefined, q: undefined, machine: undefined, lane: undefined, by: undefined, range: undefined })}
              >
                Clear filters
              </Button>
            ) : null}
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <div role="group" aria-label="Show runs that" className="flex flex-wrap gap-2">
              <Chip pressed={!search.state} label="Any state" onClick={() => setSearch({ state: undefined })} />
              {RUN_STATES.map((state) => (
                <Chip
                  key={state}
                  pressed={search.state === state}
                  label={STATE_WORD[state]}
                  count={stateCount(state)}
                  onClick={() => setSearch({ state: search.state === state ? undefined : state })}
                />
              ))}
            </div>
            <div role="group" aria-label="Kind of work" className="flex flex-wrap gap-2">
              {RUN_KINDS.map((kind) => (
                <Chip
                  key={kind}
                  pressed={search.kind === kind}
                  kind={kind === 'command' ? undefined : kind}
                  label={KIND_WORD[kind]}
                  count={kindCount(kind)}
                  onClick={() => setSearch({ kind: search.kind === kind ? undefined : kind })}
                />
              ))}
            </div>
          </div>
        </div>
        <SelectionBar
          count={picked.length}
          noun={['run', 'runs']}
          onClear={() => setSelected(new Set())}
          actions={
            <>
              {canOperate && pickedRunning.length ? (
                <Button variant="inverse" size="sm" icon={<Square className="size-4" />} onClick={() => setAsking(true)}>
                  Cancel {pickedRunning.length === picked.length ? '' : `${pickedRunning.length} `}running
                </Button>
              ) : null}
              <Button
                variant="inverse"
                size="sm"
                icon={<Copy className="size-4" />}
                onClick={() => {
                  void navigator.clipboard?.writeText(picked.map((job) => job.id).join(' '));
                  toast.success(`${plural(picked.length, 'run id')} copied`);
                }}
              >
                Copy ids
              </Button>
            </>
          }
        />
        <TableScroll>
          <Table density={rows as Density} stack aria-label="Runs">
            <thead>
              <tr>
                <Th className="w-1 pr-0!">
                  <Checkbox
                    aria-label="Select every run shown"
                    checked={allPicked ? true : picked.length ? 'indeterminate' : false}
                    onCheckedChange={() => setSelected(allPicked ? new Set() : new Set(shown.map((job) => job.id)))}
                  />
                </Th>
                <Th>Run</Th>
                <Th {...sortProps('state')}>Result</Th>
                <Th className="md:max-lg:hidden">Branch</Th>
                <Th className="md:max-lg:hidden">Who and where</Th>
                <Th align="end" className="md:max-lg:hidden">
                  Passed
                </Th>
                <Th align="end" {...sortProps('failed')}>
                  Failed
                </Th>
                <Th align="end" {...sortProps('duration')}>
                  Took
                </Th>
                <Th align="end" {...sortProps('started')}>
                  Started
                </Th>
                <Th className="w-1">
                  <span className="sr-only">Actions</span>
                </Th>
              </tr>
            </thead>
            <tbody>
              {shown.length ? (
                shown.map((job) => (
                  <RunRow
                    key={job.id}
                    org={org}
                    job={job}
                    now={now}
                    selected={selected.has(job.id)}
                    onToggle={() => toggle(job.id)}
                    onOpen={() => void navigate({ to: '/$org/runs/$id', params: { org, id: job.id } })}
                  />
                ))
              ) : (
                <TableEmpty colSpan={COLUMNS.length}>
                  <EmptyState
                    icon={<ListFilter />}
                    title="No run matches"
                    description="Nothing in the runs this machine keeps fits these filters."
                    action={
                      <Button
                        onClick={() => setSearch({ state: undefined, kind: undefined, q: undefined, machine: undefined, lane: undefined, by: undefined, range: undefined })}
                      >
                        Clear filters
                      </Button>
                    }
                  />
                </TableEmpty>
              )}
            </tbody>
          </Table>
        </TableScroll>
        {capped ? (
          <p className="border-t border-line px-5 py-3 text-secondary text-faint">
            Showing the newest {LIMIT}. Older runs are kept on their machines: <span className="font-mono text-code">agentos run --list</span>
          </p>
        ) : null}
      </Card>
      <ConfirmDialog
        open={asking}
        onOpenChange={setAsking}
        title={`Cancel ${plural(pickedRunning.length, 'running run')}?`}
        description="They stop on their machines now. Their output so far is kept, and they show here as cancelled."
        confirmLabel={pickedRunning.length === 1 ? 'Cancel the run' : `Cancel ${pickedRunning.length} runs`}
        danger
        loading={cancelling}
        onConfirm={() => void cancelPicked()}
      />
    </>
  );
}

function RunRow({ org, job, now, selected, onToggle, onOpen }: { org: string; job: Job; now: number; selected: boolean; onToggle: () => void; onOpen: () => void }) {
  const kind = runKind(job);
  const title = titleOf(job);
  const tests = job.tests;
  const source = job.source;
  const took = durationOf(job, now);
  return (
    <Tr selected={selected} onActivate={onOpen} aria-label={`${title}, ${JOB_STATUS[job.state].label}`}>
      <Td phone="hide" className="pr-0!" onClick={(event) => event.stopPropagation()}>
        <Checkbox aria-label={`Select ${title}, ${job.id}`} checked={selected} onCheckedChange={onToggle} />
      </Td>
      <Td main>
        <div className="flex min-w-0 items-center gap-3">
          <KindTile kind={kind} size="sm" />
          <CellTitle
            title={
              <Link
                to="/$org/runs/$id"
                params={{ org, id: job.id }}
                onClick={(event) => event.stopPropagation()}
                tabIndex={-1}
                title={title}
                className="block truncate text-fg hover:underline"
              >
                {title}
              </Link>
            }
            second={<span className="font-mono text-code-caption">{job.command}</span>}
          />
        </div>
      </Td>
      <Td>
        <Status status={JOB_STATUS[job.state]} />
      </Td>
      <Td className="md:max-lg:hidden" phone={source?.branch ? undefined : 'hide'}>
        {source?.branch ? (
          <div className="grid w-40 min-w-0 justify-items-start max-md:w-auto">
            <BranchPill name={source.branch} main={source.branch === 'main' || source.branch === 'master'} className="max-w-full" />
            {source.commit ? (
              <span data-second-line="" className="max-w-full truncate pl-1 font-mono text-code-caption text-faint">
                {source.commit.slice(0, 7)}
                {source.dirty ? ' + edits' : ''}
              </span>
            ) : null}
          </div>
        ) : (
          <span className="text-secondary text-faint" title="This client did not say which branch it sent">
            Not recorded
          </span>
        )}
      </Td>
      <Td className="md:max-lg:hidden">
        <div className="grid w-48 min-w-0 max-md:w-auto">
          <span className="truncate text-secondary text-fg" title={job.started_by ?? 'Who started it was not kept'}>
            {job.started_by ?? 'Not recorded'}
            <span className="text-faint"> on {job.node}</span>
          </span>
          <span data-second-line="" className="truncate font-mono text-code-caption text-faint" title={`Lane ${job.lane}`}>
            {job.lane}
          </span>
        </div>
      </Td>
      <Td numeric label="Passed" phone={tests ? undefined : 'hide'} className="md:max-lg:hidden">
        {tests ? tests.passed.toLocaleString('en') : <NoValue />}
      </Td>
      <Td numeric label="Failed" phone={tests ? undefined : 'hide'}>
        {tests ? tests.failed ? <span className="text-bad">{tests.failed.toLocaleString('en')}</span> : <span className="text-faint">0</span> : <NoValue />}
      </Td>
      <Td numeric label="Took">
        {took ?? <NoValue />}
      </Td>
      <Td numeric>
        <time dateTime={job.started_at} title={dateTime(job.started_at)} className="text-muted">
          {timeAgo(job.started_at, now)}
        </time>
      </Td>
      <Td className="w-1 text-right" phone="hide" onClick={(event) => event.stopPropagation()}>
        <RowActions>
          <CopyButton text={logsCommand(job)} label={`Copy the logs command for ${title}`} />
        </RowActions>
      </Td>
    </Tr>
  );
}
