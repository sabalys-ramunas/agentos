/**
 * Machines (Mono): a sentence naming what matters, then one table of every machine (the nodes, and
 * the environments' VMs the owner rents) with its status, what it is doing now, its load and its lanes.
 * A row opens the machine beside the table, at `/machines/<id>`. Under it, the last hour of runs, but
 * only when there are real runs to draw. One machine is most people's first view, so it gets its own
 * overview and the real ways to add another.
 */
import { Plus, Server } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useAccess, useJobs, useNode, useNodeMetrics, useNodes, useEnvironments, useVms } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import { Sparkline } from '@/components/charts/small';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { SidePanel } from '@/components/ui/dialog';
import { KindTile } from '@/components/ui/kind';
import { Meter, PageHeader } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { CellTitle, NoValue, Table, TableScroll, TableSkeleton, Td, Th, Tr } from '@/components/ui/table';
import type { SortDirection } from '@/components/ui/table';
import { usePageCrumb } from '@/components/shell/crumb';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { AddNodeDialog, AddWays, useRunsAlone } from './add-node';
import { LastHour } from './last-hour';
import type { MachineRow, Part } from './model';
import { BUSY_CPU, hourHasRuns, jobKind, lastHour, machineRows, machinesSentence, naturalDirection, sortRows, timeLeft, totals } from './model';
import { NodeNow, NodePanel, VmPanel } from './machine-aside';
import type { RunsInfo } from './machine-aside';
import type { MachineSort, TableSearch } from './search';

export interface MachinesProps {
  org: string;
  /** The machine open beside the table. */
  selected?: string;
  table: TableSearch;
  addOpen: boolean;
  onAdd: (open: boolean) => void;
  onSelect: (id: string | undefined) => void;
  onSort: (table: TableSearch) => void;
}

/**
 * The time the page reads "now" by, moved on each minute so "the last hour" slides. A hidden tab (or a
 * preview pane) throttles the timer, so it also catches up when the page shows again: a stale "now" drew
 * runs read a moment ago in the future (2026-11-15 test, P2-13).
 */
function useMinute() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const tick = () => setNow(Date.now());
    const timer = setInterval(tick, 60_000);
    const shown = () => {
      if (document.visibilityState === 'visible') tick();
    };
    document.addEventListener('visibilitychange', shown);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', shown);
    };
  }, []);
  return now;
}

function Sentence({ parts }: { parts: Part[] }) {
  return (
    <>
      {parts.map((part, index) =>
        typeof part === 'string' ? (
          <span key={index}>{part}</span>
        ) : 'name' in part ? (
          <span key={index} className="font-medium text-fg">
            {part.name}
          </span>
        ) : (
          <span key={index} className="font-medium text-warn">
            {part.warn}
          </span>
        ),
      )}
    </>
  );
}

/** CPU along the last hour, small, with the number now beside it. */
function LoadCell({ org, row }: { org: string; row: MachineRow }) {
  const metrics = useNodeMetrics(org, row.id, '1h');
  if (row.cpu === null)
    return (
      <div className="text-right">
        <NoValue />
      </div>
    );
  const values = (metrics.data?.points ?? []).map((point) => point.cpu_pct ?? 0);
  const busy = row.cpu >= BUSY_CPU;
  return (
    <div className="flex items-center justify-end gap-3">
      {values.length > 1 ? <Sparkline values={values} className="h-5 w-16" /> : null}
      <span className={cn('w-10 text-right tabular', busy ? 'text-warn' : 'text-fg')}>{row.cpu}%</span>
    </div>
  );
}

function MemoryCell({ row }: { row: MachineRow }) {
  if (row.memory === null)
    return (
      <div className="text-right">
        <NoValue />
      </div>
    );
  return (
    <div className="flex items-center justify-end gap-3" title={row.memoryText ?? undefined}>
      <Meter value={row.node?.mem.used_mb ?? row.memory} max={row.node?.mem.total_mb ?? 100} label={`Memory of ${row.id}`} className="w-14" />
      <span className="w-10 text-right text-fg tabular">{row.memory}%</span>
    </div>
  );
}

function Num({ value }: { value: number | null }) {
  if (value === null) return <NoValue />;
  return <span className={value === 0 ? 'text-faint' : undefined}>{value}</span>;
}

function NowCell({ row }: { row: MachineRow }) {
  if (row.node?.state === 'down') return <span className="text-faint">Accounts paused</span>;
  const [job, ...more] = row.now.jobs;
  if (job) {
    const kind = jobKind(job);
    return (
      <div className="flex min-w-0 items-center gap-2">
        {kind ? <KindTile kind={kind} size="xs" /> : null}
        <span className="min-w-0 truncate" title={`${job.command}${job.started_by ? `, started by ${job.started_by}` : ''}`}>
          <span className="font-mono text-code text-fg">{job.command}</span>
          {job.started_by ? <span className="text-faint"> · {job.started_by}</span> : null}
        </span>
        {more.length ? <Badge title={`${plural(more.length, 'more run')} going`}>+{more.length}</Badge> : null}
      </div>
    );
  }
  if (row.vm) {
    return (
      <span className="block truncate text-muted" title={row.vm.purpose}>
        {row.vm.purpose || 'Nothing running'}
      </span>
    );
  }
  if (row.now.agentLanes) return <span className="text-fg">An agent in {plural(row.now.agentLanes, 'account')}</span>;
  return <span className="text-faint">Nothing running</span>;
}

/** The kind of machine: a server, or a dashed cloud for a VM that deletes itself. */
function MachineIcon({ row }: { row: MachineRow }) {
  return row.type === 'vm' ? <KindTile kind="vm" size="md" waiting /> : <KindTile kind="machine" size="md" />;
}

/** The phone's one line under the name: what the hidden columns would have said. */
function phoneLine(row: MachineRow): string | null {
  if (row.vm) return [timeLeft(row.vm), row.now.jobs.length ? plural(row.now.jobs.length, 'run') : null].filter(Boolean).join(', ') || null;
  if (row.cpu === null) return null;
  const parts = [`${row.cpu}% CPU`];
  if (row.browsers !== null) parts.push(plural(row.browsers, 'browser'));
  if (row.terminals !== null) parts.push(plural(row.terminals, 'terminal'));
  if (row.now.jobs.length) parts.push(`${plural(row.now.jobs.length, 'run')} going`);
  return parts.join(', ');
}

/** The table drops columns by its own width, never by scrolling sideways (app-design.md, Machines). */
const COLUMNS = {
  load: 'hidden @xl:table-cell',
  lanes: 'hidden @3xl:table-cell',
  memory: 'hidden @4xl:table-cell',
  now: 'hidden @5xl:table-cell',
  waiting: 'hidden @5xl:table-cell',
};

function MachinesTable({ rows, org, selected, table, onSelect, onSort }: { rows: MachineRow[]; org: string; selected?: string; table: TableSearch; onSelect: (id: string) => void; onSort: (table: TableSearch) => void }) {
  const sortProps = (column: MachineSort) => ({
    sort: table.sort === column ? (table.dir ?? naturalDirection(column)) : undefined,
    onSort: (next: SortDirection) => onSort({ sort: column, dir: table.sort === column ? next : naturalDirection(column) }),
  });
  return (
    <TableScroll>
      <Table stack aria-label="Every machine">
        <thead>
          <tr>
            <Th {...sortProps('name')}>Machine</Th>
            <Th {...sortProps('state')}>Status</Th>
            <Th className={COLUMNS.now}>Doing now</Th>
            <Th className={COLUMNS.load} align="end" unit="last hour" {...sortProps('load')}>
              CPU
            </Th>
            <Th className={COLUMNS.memory} align="end" {...sortProps('memory')}>
              Memory
            </Th>
            <Th className={COLUMNS.lanes} align="end">
              Browsers
            </Th>
            <Th className={COLUMNS.lanes} align="end">
              Terminals
            </Th>
            <Th className={COLUMNS.waiting} align="end">
              <span title="Requests on it waiting for a person">Waiting</span>
            </Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const phone = phoneLine(row);
            return (
              <Tr key={row.id} selected={row.id === selected} onActivate={() => onSelect(row.id)} aria-label={`${row.vm ? row.name : row.id}, ${row.status.label}`}>
                {/* The name keeps its own width; "Doing now" takes the rest while it shows. */}
                <Td main className="@5xl:w-auto @5xl:min-w-60">
                  <div className="flex min-w-0 items-center gap-3">
                    <MachineIcon row={row} />
                    <div className="grid min-w-0">
                      <CellTitle title={row.vm ? row.name : row.id} second={row.vm ? `Environment${row.where ? ` · ${row.where}` : ''}` : row.where || undefined} />
                      {phone ? <span className="truncate text-secondary text-muted md:hidden">{phone}</span> : null}
                    </div>
                  </div>
                </Td>
                <Td>
                  {/* w-max: a bare grid reports no width of its own, and the column would squeeze the word. */}
                  <div className="grid w-max gap-1">
                    <Status status={row.status} chip />
                    {row.vm && timeLeft(row.vm) ? <span className="text-caption whitespace-nowrap text-faint max-md:hidden">{timeLeft(row.vm)}</span> : null}
                  </div>
                </Td>
                <Td className={cn(COLUMNS.now, 'w-full max-w-0 @5xl:min-w-40')}>
                  <NowCell row={row} />
                </Td>
                <Td className={COLUMNS.load}>{row.type === 'node' ? <LoadCell org={org} row={row} /> : <div className="text-right"><NoValue /></div>}</Td>
                <Td className={COLUMNS.memory}>
                  <MemoryCell row={row} />
                </Td>
                <Td numeric className={COLUMNS.lanes}>
                  <Num value={row.browsers} />
                </Td>
                <Td numeric className={COLUMNS.lanes}>
                  <Num value={row.terminals} />
                </Td>
                <Td numeric className={COLUMNS.waiting}>
                  <Num value={row.waiting} />
                </Td>
              </Tr>
            );
          })}
        </tbody>
      </Table>
    </TableScroll>
  );
}

/** One machine is most people's first view: what is on it now, and how to add another. */
function OneMachine({ org, id, runs, showNow }: { org: string; id: string; runs: RunsInfo; showNow: boolean }) {
  const node = useNode(org, id);
  const alone = useRunsAlone(org);
  return (
    <div className={cn('grid items-start gap-6', showNow && 'xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]')}>
      {showNow ? (
        <Card>
          <CardHeader title={`On ${id} now`} />
          <div className="px-5 py-4">
            {node.data ? (
              <NodeNow org={org} node={node.data} runs={runs} />
            ) : node.isError ? (
              <ErrorState error={node.error} onRetry={() => void node.refetch()} className="py-8" />
            ) : (
              <div role="status" aria-label="Loading" className="grid gap-4">
                <Skeleton className="h-24" />
                <Skeleton className="h-24" />
              </div>
            )}
          </div>
        </Card>
      ) : null}
      {alone ? (
        <Card>
          <CardHeader title="Add a machine" meta="This node is one machine. Each way is one command." />
          <AddWays className="px-5 py-4" />
        </Card>
      ) : null}
    </div>
  );
}

export function Machines({ org, selected, table, addOpen, onAdd, onSelect, onSort }: MachinesProps) {
  const access = useAccess(org);
  const owner = access.role === 'owner';
  const nodes = useNodes(org);
  // VMs are the owner's, as the node says; anyone else is never asked, so no refusal shows.
  const vms = useVms(org, owner);
  const jobs = useJobs(org, { limit: 100 });
  const now = useMinute();
  const canAdd = access.can('nodes.manage') && !isNotServed(nodes.error);

  const jobList = jobs.data?.jobs;
  const vmList = useMemo(() => (vms.data?.available ? vms.data.vms : []), [vms.data]);
  const envs = useEnvironments(org, 'all', owner).data?.environments;
  const titles = useMemo(() => new Map((envs ?? []).map((env) => [env.lanes.terminal, env.title])), [envs]);
  const rows = useMemo(() => machineRows(nodes.data ?? [], vmList, jobList ?? [], titles), [nodes.data, vmList, jobList, titles]);
  const sorted = useMemo(() => sortRows(rows, table.sort, table.dir), [rows, table.sort, table.dir]);
  // Never behind the runs just read: a fresh read of them moves "now" on too.
  const hourNow = useMemo(() => (jobList ? Math.max(now, Date.now()) : now), [now, jobList]);
  const lanes = useMemo(() => lastHour(rows, jobList ?? [], hourNow), [rows, jobList, hourNow]);
  const open = selected ? rows.find((row) => row.id === selected) : undefined;
  usePageCrumb(selected);

  const add = canAdd ? (
    <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => onAdd(true)}>
      Add a machine
    </Button>
  ) : null;
  const dialog = canAdd ? <AddNodeDialog org={org} open={addOpen} onOpenChange={onAdd} /> : null;
  const lede = 'The machines your agents work on: their browsers, terminals, dev servers and environments.';

  if (nodes.isPending) {
    return (
      <>
        <PageHeader title="Machines" description={lede} actions={add} />
        <Card className="overflow-hidden" role="status" aria-label="Loading">
          <TableSkeleton columns={['Machine', 'Status', 'Doing now', 'CPU', 'Memory']} rows={3} />
        </Card>
        {dialog}
      </>
    );
  }
  if (nodes.isError) {
    return (
      <>
        <PageHeader title="Machines" description={lede} actions={add} />
        <Card>
          <ErrorState error={nodes.error} onRetry={() => void nodes.refetch()} />
        </Card>
        {dialog}
      </>
    );
  }
  if (!rows.length) {
    return (
      <>
        <PageHeader title="Machines" description={lede} actions={add} />
        <Card>
          <EmptyState
            icon={<Server />}
            title="No machines yet"
            description="Your agents need a machine to run tests, dev servers and browsers on. Adding one is one command."
            action={canAdd ? <Button variant="primary" onClick={() => onAdd(true)}>Add your first machine</Button> : undefined}
          />
        </Card>
        {dialog}
      </>
    );
  }

  const nodeRows = rows.filter((row) => row.type === 'node');
  const single = nodeRows.length === 1 && rows.length === 1 ? nodeRows[0] : undefined;
  const runs: RunsInfo = jobs.data
    ? { jobs: jobs.data.jobs, available: jobs.data.available }
    : jobs.isError
      ? { jobs: [], hidden: isNotServed(jobs.error) ? 'Runs are shown on the machine itself, not here.' : 'Runs here are not shown to you.' }
      : {};
  const timeline = hourHasRuns(lanes);
  // Why the last hour is not drawn, in plain words. A refused or failed read says nothing.
  const noTimeline = jobs.data
    ? jobs.data.available
      ? 'The last hour is left out: no run started or ended on these machines in it.'
      : 'The last hour is left out: it shows runs, and no machine here has a terminal lane to run them in.'
    : null;

  return (
    <>
      <PageHeader title="Machines" description={<Sentence parts={machinesSentence(rows, now)} />} actions={add} />
      <div className={cn('grid items-start gap-6', open && 'lg:grid-cols-[minmax(0,1fr)_minmax(20rem,25rem)]')}>
        <div className="grid min-w-0 gap-6">
          <Card className="@container overflow-hidden">
            <MachinesTable rows={sorted} org={org} selected={selected} table={table} onSelect={onSelect} onSort={onSort} />
            <div className="border-t border-line px-5 py-3 text-secondary text-faint">{totals(rows)}</div>
          </Card>
          {selected && !open ? (
            <Card>
              <EmptyState icon={<Server />} title={`No machine called ${selected}`} description="It may have been removed, or a VM may have deleted itself." action={<Button onClick={() => onSelect(undefined)}>Every machine</Button>} />
            </Card>
          ) : null}
          {single ? <OneMachine org={org} id={single.id} runs={runs} showNow={!open} /> : null}
          {timeline ? <LastHour org={org} lanes={lanes} now={hourNow} /> : noTimeline ? <p className="text-secondary text-faint">{noTimeline}</p> : null}
        </div>
        <SidePanel
          open={Boolean(open)}
          onOpenChange={(next) => {
            if (!next) onSelect(undefined);
          }}
          title={open?.vm ? open.name : (open?.id ?? '')}
          description={open?.vm ? `Environment${open.where ? ` · ${open.where}` : ''}` : open?.where || undefined}
          className="lg:max-h-[calc(100dvh-var(--topbar-height)-32px)]"
        >
          {open?.vm ? <VmPanel org={org} vm={open.vm} runs={runs} /> : open ? <NodePanel org={org} id={open.id} runs={runs} /> : null}
        </SidePanel>
      </div>
      {dialog}
    </>
  );
}
