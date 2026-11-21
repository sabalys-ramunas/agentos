/** The runs table: its columns, sorting, choosing several, and one row per run. */
import { Link, useNavigate, useSearch } from '@tanstack/react-router';
import { Check, Copy, Ellipsis, ListFilter, Search, Square, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useCancelJob } from '@/api/hooks';
import type { Job } from '@/api/types';
import { Chip } from '@/components/ui/badge';
import { Button, IconButton } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Menu, MenuContent, MenuItem, MenuLabel, MenuTrigger } from '@/components/ui/menu';
import { ConfirmDialog } from '@/components/ui/dialog';
import { Input } from '@/components/ui/field';
import { KindTile } from '@/components/ui/kind';
import { CopyButton } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { SelectionBar } from '@/components/ui/selection-bar';
import { EmptyState } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { CellTitle, NoValue, RowActions, Table, TableEmpty, TableScroll, Td, Th, Tr } from '@/components/ui/table';
import type { Density, SortDirection } from '@/components/ui/table';
import { SegmentedControl } from '@/components/ui/tabs';
import { Checkbox } from '@/components/ui/toggle';
import { cn } from '@/lib/cn';
import { dateTime, plural, timeAgo } from '@/lib/format';
import { useNow } from '@/lib/use-now';
import { KIND_WORD } from './kind';
import { RUN_KINDS, RUN_STATES } from './search';
import type { RunKind, RunSort, RunStateFilter, RunsSearch } from './search';
import { JOB_STATUS, durationOf, logsCommand, runKind, titleOf } from './words';
import { COLUMNS, LIMIT, RANGES, STATE_WORD, density, filterJobs, sortJobs } from './runs-page';

export function RunsTable({ org, jobs, capped }: { org: string; jobs: Job[]; capped: boolean }) {
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

  const clearFilters = () => setSearch({ state: undefined, kind: undefined, q: undefined, machine: undefined, lane: undefined, by: undefined, range: undefined });

  return (
    <>
      <div className="grid gap-4">
        {/* One row of filters, as drawn: search, time, kind, the states with their counts, and the table's menu. */}
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-0 flex-1 basis-56 md:max-w-90">
            <label htmlFor="runs-search" className="sr-only">
              Search runs
            </label>
            <Search aria-hidden="true" className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-faint" />
            <Input
              id="runs-search"
              type="search"
              value={search.q ?? ''}
              placeholder="Search runs"
              title="Search a command, branch, machine or id"
              className="pl-9"
              onChange={(event) => setSearch({ q: event.target.value || undefined })}
            />
          </div>
          <Select
            aria-label="How far back"
            value={search.range ?? 'any'}
            onValueChange={(value) => setSearch({ range: value === 'any' ? undefined : (value as RunsSearch['range']) })}
            options={RANGES}
            className="w-44"
          />
          <Select
            aria-label="Kind of work"
            value={search.kind ?? 'any'}
            onValueChange={(value) => setSearch({ kind: value === 'any' ? undefined : (value as RunKind) })}
            options={[{ value: 'any', label: 'Any kind' }, ...RUN_KINDS.map((kind) => ({ value: kind, label: `${KIND_WORD[kind]} (${kindCount(kind)})` }))]}
            className="w-40"
          />
          <div className="scrollbar-none overflow-x-auto max-md:-mx-4 max-md:w-[calc(100%+32px)] max-md:px-4">
            <SegmentedControl
              label="Show runs that"
              size="md"
              value={search.state ?? 'all'}
              onValueChange={(value) => setSearch({ state: value === 'all' ? undefined : (value as RunStateFilter) })}
              options={[
                { value: 'all', label: <>All <span className="tabular font-normal text-faint">{filterJobs(jobs, { ...search, state: undefined }, now).length}</span></> },
                ...RUN_STATES.map((state) => ({
                  value: state,
                  label: (
                    <>
                      {STATE_WORD[state]} <span className="tabular font-normal text-faint">{stateCount(state)}</span>
                    </>
                  ),
                })),
              ]}
            />
          </div>
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
            <Button size="sm" variant="ghost" onClick={clearFilters}>
              Clear filters
            </Button>
          ) : null}
          <Menu>
            <MenuTrigger asChild>
              <IconButton label="Table settings" className="ml-auto max-md:hidden">
                <Ellipsis className="size-4" />
              </IconButton>
            </MenuTrigger>
            <MenuContent align="end">
              <MenuLabel>Rows</MenuLabel>
              {(['default', 'compact'] as const).map((value) => (
                <MenuItem key={value} onSelect={() => density.set(value)}>
                  {value === 'default' ? 'Comfortable' : 'Compact'}
                  {rows === value ? <Check aria-hidden="true" className="ml-auto size-4 text-accent-text" /> : null}
                </MenuItem>
              ))}
            </MenuContent>
          </Menu>
        </div>
        <Card className="overflow-hidden">
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
                <Th className="md:max-lg:hidden">Branch, who and where</Th>
                <Th align="end" className="md:max-lg:hidden">
                  Passed
                </Th>
                <Th align="end" {...sortProps('failed')}>
                  Failed
                </Th>
                <Th align="end" {...sortProps('duration')}>
                  Took
                </Th>
                <Th {...sortProps('started')}>Started</Th>
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
                    action={<Button onClick={clearFilters}>Clear filters</Button>}
                  />
                </TableEmpty>
              )}
            </tbody>
          </Table>
        </TableScroll>
        {capped ? (
          <p className="border-t border-line px-5 py-3 text-secondary text-faint">
            Showing the newest {LIMIT}. Older runs are kept on their machines: <span className="font-mono text-code">openajar run --list</span>
          </p>
        ) : null}
        </Card>
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
        <p className="text-secondary text-faint">
          Routines, and the runs agents make on accounts, are in{' '}
          <Link to="/$org/activity" params={{ org }} className="link">
            History
          </Link>
          .
        </p>
      </div>
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
          <KindTile kind={kind} size="lg" />
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
        <Status status={JOB_STATUS[job.state]} chip />
      </Td>
      <Td className="md:max-lg:hidden">
        <div className="grid w-56 min-w-0 max-md:w-auto">
          <span className="flex min-w-0 items-center gap-2">
            {source?.branch ? (
              <span title={source.branch} className={cn('truncate font-mono text-code-caption', source.branch === 'main' || source.branch === 'master' ? 'text-fg' : 'text-muted')}>
                {source.branch}
                {source.commit ? <span className="text-faint"> · {source.commit.slice(0, 7)}{source.dirty ? ' + edits' : ''}</span> : null}
              </span>
            ) : (
              <span className="text-caption text-faint" title="This client did not say which branch it sent">
                No branch recorded
              </span>
            )}
          </span>
          <span className="truncate text-secondary text-fg" title={`${job.started_by ?? 'Who started it was not kept'} on ${job.node}, lane ${job.lane}`}>
            {job.started_by ?? 'Not recorded'}
            <span className="text-faint"> on {job.node}</span>
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
      <Td label="Started">
        <time dateTime={job.started_at} title={dateTime(job.started_at)} className="tabular text-muted">
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
